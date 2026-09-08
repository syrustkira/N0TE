from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from governance.execution_permit import ExecutionPermitAuthority
from governance.trusted_context import FileTrustedContextProvider

from .authority import ActionIntent

T = TypeVar("T")


class CoordinatorGatewayError(RuntimeError):
    """A coordinator attempted to execute outside the permit-controlled gateway."""


@dataclass(frozen=True)
class GatedExecutionResult(Generic[T]):
    permit_id: str
    action_id: str
    action_intent_fingerprint: str
    result: T


class CoordinatorMutationGateway:
    """The only supported bridge from coordinator intent to registered mutation.

    The gateway consumes a one-time permit before calling the executor. A failed
    executor does not restore the permit; callers must reconstruct current state
    and obtain a fresh permit before retrying. That prevents stale replay after a
    partial or ambiguous mutation.
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
        self._executors: dict[str, Callable[[ActionIntent], object]] = {}
        self._action_classes: dict[str, str] = {}

    def register(
        self,
        name: str,
        *,
        action_class: str,
        executor: Callable[[ActionIntent], T],
    ) -> None:
        name = str(name).strip()
        action_class = str(action_class).strip().upper()
        if not name:
            raise CoordinatorGatewayError("mutation name must not be empty")
        if name in self._executors:
            raise CoordinatorGatewayError(f"mutation already registered: {name}")
        if action_class == "READ_ONLY":
            raise CoordinatorGatewayError("read-only operations do not belong in mutation gateway")
        if action_class not in {"REVERSIBLE", "COMPENSATABLE", "IRREVERSIBLE"}:
            raise CoordinatorGatewayError(f"unsupported mutation action class: {action_class}")
        if not callable(executor):
            raise TypeError("executor must be callable")
        self._executors[name] = executor
        self._action_classes[name] = action_class

    @property
    def registered_mutations(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))

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
        executor = self._executors.get(name)
        if executor is None:
            raise CoordinatorGatewayError(f"unregistered mutation: {name}")
        if not isinstance(action, ActionIntent):
            raise TypeError("action must be ActionIntent")
        expected_class = self._action_classes[name]
        if action.action_class != expected_class:
            raise CoordinatorGatewayError(
                f"registered mutation class mismatch expected={expected_class} action={action.action_class}"
            )

        snapshot = self._contexts.get(context_snapshot_id)
        consumed = self._permits.consume(
            token=permit_token,
            envelope=envelope,
            action=action,
            snapshot=snapshot,
        )
        result = executor(action)
        return GatedExecutionResult(
            permit_id=consumed.permit_id,
            action_id=action.action_id,
            action_intent_fingerprint=action.intent_fingerprint,
            result=result,
        )
