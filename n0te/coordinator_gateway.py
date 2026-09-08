from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from governance.execution_permit import ExecutionPermitAuthority
from governance.trusted_context import FileTrustedContextProvider

from .authority import ActionIntent

T = TypeVar("T")


class CoordinatorGatewayError(RuntimeError):
    """A coordinator attempted to execute outside the permit-controlled gateway."""


class MutationVerificationError(CoordinatorGatewayError):
    """The mutation may have happened, but fresh acceptance evidence did not verify it."""


class MutationReconciliationError(CoordinatorGatewayError):
    """The mutation verified externally, but canonical owning state was not reconciled."""


@dataclass(frozen=True)
class MutationVerification:
    verified: bool
    evidence_refs: tuple[str, ...]
    observation: str

    def __post_init__(self) -> None:
        if type(self.verified) is not bool:
            raise TypeError("verification.verified must be bool")
        refs = tuple(str(item).strip() for item in self.evidence_refs)
        if any(not item for item in refs) or len(refs) != len(set(refs)):
            raise CoordinatorGatewayError("verification evidence_refs must be unique non-empty refs")
        observation = str(self.observation).strip()
        if not observation:
            raise CoordinatorGatewayError("verification observation must not be empty")
        if self.verified and not refs:
            raise CoordinatorGatewayError("verified mutation requires fresh evidence refs")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "observation", observation)


@dataclass(frozen=True)
class GatedExecutionResult(Generic[T]):
    permit_id: str
    action_id: str
    action_intent_fingerprint: str
    result: T
    verification: MutationVerification
    reconciliation_ref: str


@dataclass(frozen=True)
class _MutationRegistration(Generic[T]):
    action_class: str
    executor: Callable[[ActionIntent], T]
    verifier: Callable[[ActionIntent, T], MutationVerification]
    reconciler: Callable[[ActionIntent, T, MutationVerification], str]


class CoordinatorMutationGateway:
    """Permit -> mutation -> fresh verification -> canonical reconciliation.

    The gateway consumes a one-time permit before calling the executor. A failed
    executor, ambiguous verification, or failed reconciliation never restores the
    permit. Callers must reconstruct current state and obtain a fresh permit before
    retrying, preventing stale replay after a partial or uncertain side effect.
    """

    def __init__(
        self,
        *,
        permits: ExecutionPermitAuthority,
        contexts: FileTrustedContextProvider,
    ):
        if not isinstance(permits, ExecutionPermitAuthority):
            raise TypeError("permits must be ExecutionPermitAuthority")
        if not isinstance(contexts, FileTrustedContextProvider):
            raise TypeError("contexts must be FileTrustedContextProvider")
        self._permits = permits
        self._contexts = contexts
        self._mutations: dict[str, _MutationRegistration] = {}

    def register(
        self,
        name: str,
        *,
        action_class: str,
        executor: Callable[[ActionIntent], T],
        verifier: Callable[[ActionIntent, T], MutationVerification],
        reconciler: Callable[[ActionIntent, T, MutationVerification], str],
    ) -> None:
        name = str(name).strip()
        action_class = str(action_class).strip().upper()
        if not name:
            raise CoordinatorGatewayError("mutation name must not be empty")
        if name in self._mutations:
            raise CoordinatorGatewayError(f"mutation already registered: {name}")
        if action_class == "READ_ONLY":
            raise CoordinatorGatewayError("read-only operations do not belong in mutation gateway")
        if action_class not in {"REVERSIBLE", "COMPENSATABLE", "IRREVERSIBLE"}:
            raise CoordinatorGatewayError(f"unsupported mutation action class: {action_class}")
        for field, fn in (("executor", executor), ("verifier", verifier), ("reconciler", reconciler)):
            if not callable(fn):
                raise TypeError(f"{field} must be callable")
        self._mutations[name] = _MutationRegistration(
            action_class=action_class,
            executor=executor,
            verifier=verifier,
            reconciler=reconciler,
        )

    @property
    def registered_mutations(self) -> tuple[str, ...]:
        return tuple(sorted(self._mutations))

    def execute(
        self,
        name: str,
        *,
        permit_token: str,
        context_snapshot_id: str,
        envelope,
        action: ActionIntent,
    ) -> GatedExecutionResult[T]:
        name = str(name).strip()
        registration = self._mutations.get(name)
        if registration is None:
            raise CoordinatorGatewayError(f"unregistered mutation: {name}")
        if not isinstance(action, ActionIntent):
            raise TypeError("action must be ActionIntent")
        if action.action_class != registration.action_class:
            raise CoordinatorGatewayError(
                f"registered mutation class mismatch expected={registration.action_class} action={action.action_class}"
            )

        snapshot = self._contexts.get(context_snapshot_id)
        consumed = self._permits.consume(
            token=permit_token,
            envelope=envelope,
            action=action,
            snapshot=snapshot,
        )

        result = registration.executor(action)
        verification = registration.verifier(action, result)
        if not isinstance(verification, MutationVerification):
            raise MutationVerificationError(
                "mutation verifier returned no typed verification; completion remains unproven"
            )
        if not verification.verified:
            raise MutationVerificationError(
                "mutation completion is unverified after execution; reconstruct current state before any retry"
            )

        reconciliation_ref = registration.reconciler(action, result, verification)
        reconciliation_ref = str(reconciliation_ref).strip()
        if not reconciliation_ref:
            raise MutationReconciliationError(
                "verified mutation has no canonical reconciliation receipt"
            )

        return GatedExecutionResult(
            permit_id=consumed.permit_id,
            action_id=action.action_id,
            action_intent_fingerprint=action.intent_fingerprint,
            result=result,
            verification=verification,
            reconciliation_ref=reconciliation_ref,
        )
