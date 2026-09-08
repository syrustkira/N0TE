from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from .authority import ActionIntent, ApprovalBinding, AuthorityService
from .focus import FocusContext
from .musical_plan import (
    CompiledHostAction,
    HostCompilation,
    MusicalPlan,
    MusicalPlanService,
)
from .song_transactions import SongTransactionBinding, SongTransactionService
from .transactions import (
    CompensationResult,
    PostconditionResult,
    StepExecution,
    TransactionHistory,
    TransactionPlan,
    TransactionReceipt,
    TransactionResult,
    TransactionSnapshot,
    TransactionStep,
)


class MusicalTransactionError(RuntimeError):
    """The Musical Plan cannot safely enter the transactional execution path."""


def _text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise MusicalTransactionError(f"{field} must be text")
    text = " ".join(value.split())
    if not text:
        raise MusicalTransactionError(f"{field} must not be empty")
    return text


def compilation_fingerprint(compilation: HostCompilation) -> str:
    """Fingerprint the material host execution envelope bound by user approval."""

    if not isinstance(compilation, HostCompilation):
        raise TypeError("compilation must be HostCompilation")
    payload = {
        "schema": "n0te.host-compilation-authority/v1",
        "plan_id": compilation.plan_id,
        "plan_fingerprint": compilation.plan_fingerprint,
        "compiler_id": compilation.compiler_id,
        "compiler_version": compilation.compiler_version,
        "host_family": compilation.host_family,
        "host_runtime_fingerprint": compilation.host_runtime_fingerprint,
        "actions": [
            {
                "action_id": action.action_id,
                "route_kind": action.route_kind,
                "capability": action.capability,
                "payload_ref": action.payload_ref,
                "postcondition_ref": action.postcondition_ref,
                "compensatable": action.compensatable,
                "depends_on_action_ids": list(action.depends_on_action_ids),
            }
            for action in compilation.actions
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class CompiledActionDriver(Protocol):
    """Driver that receives the exact compiled action, never only a step label."""

    def prepare_snapshot(
        self,
        transaction_plan: TransactionPlan,
        musical_plan: MusicalPlan,
        compilation: HostCompilation,
    ) -> TransactionSnapshot: ...

    def execute_action(self, action: CompiledHostAction) -> StepExecution: ...

    def verify_action(
        self,
        action: CompiledHostAction,
        execution: StepExecution,
    ) -> PostconditionResult: ...

    def compensate_action(
        self,
        action: CompiledHostAction,
        snapshot: TransactionSnapshot,
    ) -> CompensationResult: ...

    def success_receipt(
        self,
        transaction_plan: TransactionPlan,
        musical_plan: MusicalPlan,
        compilation: HostCompilation,
        snapshot: TransactionSnapshot,
    ) -> TransactionReceipt: ...


@dataclass(frozen=True)
class PreparedMusicalTransaction:
    """Exact prior-authority and Song-binding witness for one execution.

    The witness itself grants no authority. Approval remains owned by the
    immutable OperationJournal identity, while execution/recovery remain owned by
    SongTransactionService and TransactionCoordinator.
    """

    plan_id: str
    plan_fingerprint: str
    compilation_fingerprint: str
    intent_fingerprint: str
    transaction_plan: TransactionPlan
    song_binding: SongTransactionBinding
    action_authority_granted: bool = False

    def __post_init__(self) -> None:
        for field in (
            "plan_id",
            "plan_fingerprint",
            "compilation_fingerprint",
            "intent_fingerprint",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if not isinstance(self.transaction_plan, TransactionPlan):
            raise TypeError("transaction_plan must be TransactionPlan")
        if not isinstance(self.song_binding, SongTransactionBinding):
            raise TypeError("song_binding must be SongTransactionBinding")
        if self.action_authority_granted is not False:
            raise MusicalTransactionError(
                "PreparedMusicalTransaction records prior authority; it never grants authority"
            )
        if (
            self.song_binding.operation_id != self.transaction_plan.operation_id
            or self.song_binding.transaction_id != self.transaction_plan.transaction_id
            or self.song_binding.plan_fingerprint != self.transaction_plan.plan_fingerprint
        ):
            raise MusicalTransactionError(
                "Song transaction binding must match the exact transaction plan"
            )
        if self.song_binding.intent_fingerprint != self.intent_fingerprint:
            raise MusicalTransactionError(
                "Song transaction binding must match the exact approved intent"
            )


class _CompiledDriverAdapter:
    _METHODS = (
        "prepare_snapshot",
        "execute_action",
        "verify_action",
        "compensate_action",
        "success_receipt",
    )

    def __init__(
        self,
        driver: CompiledActionDriver,
        *,
        musical_plan: MusicalPlan,
        compilation: HostCompilation,
    ) -> None:
        missing = [
            name for name in self._METHODS if not callable(getattr(driver, name, None))
        ]
        if missing:
            raise MusicalTransactionError(
                f"compiled action driver is missing methods: {missing}"
            )
        self.driver = driver
        self.musical_plan = musical_plan
        self.compilation = compilation
        self.actions = {action.action_id: action for action in compilation.actions}

    def _action(self, step: TransactionStep) -> CompiledHostAction:
        try:
            action = self.actions[step.step_id]
        except KeyError as exc:
            raise MusicalTransactionError(
                f"transaction step has no exact compiled action: {step.step_id}"
            ) from exc
        expected = TransactionStep(
            action.action_id,
            MusicalTransactionService._step_description(action),
            action.postcondition_ref,
            action.compensatable,
            action.depends_on_action_ids,
        )
        if expected != step:
            raise MusicalTransactionError(
                f"transaction step no longer matches compiled action: {step.step_id}"
            )
        return action

    def prepare_snapshot(self, plan: TransactionPlan) -> TransactionSnapshot:
        return self.driver.prepare_snapshot(
            plan,
            self.musical_plan,
            self.compilation,
        )

    def execute_step(self, step: TransactionStep) -> StepExecution:
        return self.driver.execute_action(self._action(step))

    def verify_postcondition(
        self,
        step: TransactionStep,
        execution: StepExecution,
    ) -> PostconditionResult:
        return self.driver.verify_action(self._action(step), execution)

    def compensate_step(
        self,
        step: TransactionStep,
        snapshot: TransactionSnapshot,
    ) -> CompensationResult:
        return self.driver.compensate_action(self._action(step), snapshot)

    def success_receipt(
        self,
        plan: TransactionPlan,
        snapshot: TransactionSnapshot,
    ) -> TransactionReceipt:
        return self.driver.success_receipt(
            plan,
            self.musical_plan,
            self.compilation,
            snapshot,
        )


class MusicalTransactionService:
    """Bridge exact Musical Plan meaning into existing transaction owners.

    No second permission system, operation journal, transaction journal, or
    recovery store is created here. The service revalidates current focus,
    derives one exact REVERSIBLE ActionIntent from MusicalPlan+HostCompilation,
    consumes an explicit ApprovalBinding, claims one Song-bound execute-once
    operation, then delegates mutation, postconditions, compensation, UNKNOWN and
    receipts to the existing SongTransactionService/TransactionCoordinator path.
    """

    def __init__(
        self,
        plans: MusicalPlanService,
        song_transactions: SongTransactionService,
    ) -> None:
        if not isinstance(plans, MusicalPlanService):
            raise TypeError("plans must be MusicalPlanService")
        if not isinstance(song_transactions, SongTransactionService):
            raise TypeError("song_transactions must be SongTransactionService")
        self.plans = plans
        self.song_transactions = song_transactions
        self.journal = song_transactions.journal
        self.store = song_transactions.store

    @staticmethod
    def _validate_pair(plan: MusicalPlan, compilation: HostCompilation) -> str:
        if not isinstance(plan, MusicalPlan):
            raise TypeError("plan must be MusicalPlan")
        if not isinstance(compilation, HostCompilation):
            raise TypeError("compilation must be HostCompilation")
        expected = {
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "host_runtime_fingerprint": plan.host_runtime_fingerprint,
        }
        for field, value in expected.items():
            if getattr(compilation, field) != value:
                raise MusicalTransactionError(
                    f"compilation does not match Musical Plan {field}"
                )
        noncompensatable = [
            action.action_id for action in compilation.actions if not action.compensatable
        ]
        if noncompensatable:
            raise MusicalTransactionError(
                "WP-090 reversible-write bridge refuses noncompensatable compiled "
                f"actions: {noncompensatable}"
            )
        return compilation_fingerprint(compilation)

    @staticmethod
    def _step_description(action: CompiledHostAction) -> str:
        return f"{action.capability} via {action.route_kind} using {action.payload_ref}"

    @classmethod
    def _steps(cls, compilation: HostCompilation) -> tuple[TransactionStep, ...]:
        return tuple(
            TransactionStep(
                action.action_id,
                cls._step_description(action),
                action.postcondition_ref,
                action.compensatable,
                action.depends_on_action_ids,
            )
            for action in compilation.actions
        )

    @staticmethod
    def _transaction_id(
        operation_id: str,
        plan_fingerprint: str,
        compiled_fingerprint: str,
    ) -> str:
        raw = "\0".join(
            (operation_id, plan_fingerprint, compiled_fingerprint)
        ).encode("utf-8")
        return f"txn_musical_{hashlib.sha256(raw).hexdigest()[:32]}"

    def preview(
        self,
        plan: MusicalPlan,
        context: FocusContext,
        compilation: HostCompilation,
    ) -> ActionIntent:
        if not isinstance(context, FocusContext):
            raise TypeError("context must be FocusContext")
        self.plans.validate_current(plan, context)
        compiled_fingerprint = self._validate_pair(plan, compilation)
        return ActionIntent(
            action_id=f"musical-plan:{plan.plan_id}",
            job_id=f"produce:{plan.song_id}",
            action_class="REVERSIBLE",
            description=plan.desired_change,
            target_ref=f"song:{plan.song_id}/musical-plan:{plan.plan_id}",
            revision_fingerprint=plan.fingerprint,
            payload_fingerprint=compiled_fingerprint,
        )

    def _active_target_version(
        self,
        plan: MusicalPlan,
        target_version_id: str | None,
    ) -> str | None:
        active = self.store.active_song()
        if active is None or active.id != plan.song_id:
            raise MusicalTransactionError(
                "the Musical Plan Song must be active before operation preparation"
            )
        current = active.current_version_id
        if target_version_id is None:
            return current
        target = _text(target_version_id, "target_version_id")
        if current != target:
            raise MusicalTransactionError(
                "target_version_id must equal the active Song current Version"
            )
        return target

    def prepare(
        self,
        plan: MusicalPlan,
        context: FocusContext,
        compilation: HostCompilation,
        approval: ApprovalBinding,
        *,
        idempotency_key: str,
        claim_evidence_ref: str,
        target_version_id: str | None = None,
    ) -> PreparedMusicalTransaction:
        if not isinstance(approval, ApprovalBinding):
            raise TypeError("approval must be ApprovalBinding")
        intent = self.preview(plan, context, compilation)
        if AuthorityService.validate(intent, approval).status != "VALID":
            raise MusicalTransactionError(
                "approval is stale for this exact Musical Plan compilation"
            )
        steps = self._steps(compilation)
        version_id = self._active_target_version(plan, target_version_id)
        operation = self.journal.prepare(
            idempotency_key=_text(idempotency_key, "idempotency_key"),
            intent=intent,
            approval=approval,
            song_id=plan.song_id,
            version_id=version_id,
        )
        operation = self.journal.claim_execution(
            operation.operation_id,
            intent=intent,
            approval=approval,
            claim_evidence_ref=_text(claim_evidence_ref, "claim_evidence_ref"),
        )
        compiled_fingerprint = compilation_fingerprint(compilation)
        transaction_plan = TransactionPlan(
            self._transaction_id(
                operation.operation_id,
                plan.fingerprint,
                compiled_fingerprint,
            ),
            operation.operation_id,
            steps,
        )
        song_binding = self.song_transactions.bind(transaction_plan)
        return PreparedMusicalTransaction(
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            compilation_fingerprint=compiled_fingerprint,
            intent_fingerprint=intent.intent_fingerprint,
            transaction_plan=transaction_plan,
            song_binding=song_binding,
        )

    def _validate_prepared(
        self,
        prepared: PreparedMusicalTransaction,
        plan: MusicalPlan,
        context: FocusContext,
        compilation: HostCompilation,
    ) -> None:
        if not isinstance(prepared, PreparedMusicalTransaction):
            raise TypeError("prepared must be PreparedMusicalTransaction")
        intent = self.preview(plan, context, compilation)
        compiled_fingerprint = compilation_fingerprint(compilation)
        if (
            prepared.plan_id != plan.plan_id
            or prepared.plan_fingerprint != plan.fingerprint
            or prepared.compilation_fingerprint != compiled_fingerprint
            or prepared.intent_fingerprint != intent.intent_fingerprint
        ):
            raise MusicalTransactionError(
                "prepared transaction does not match the exact current plan/compilation"
            )
        expected_plan = TransactionPlan(
            prepared.transaction_plan.transaction_id,
            prepared.transaction_plan.operation_id,
            self._steps(compilation),
        )
        if expected_plan.plan_fingerprint != prepared.transaction_plan.plan_fingerprint:
            raise MusicalTransactionError(
                "transaction plan no longer matches the exact host compilation"
            )

    def run(
        self,
        prepared: PreparedMusicalTransaction,
        plan: MusicalPlan,
        context: FocusContext,
        compilation: HostCompilation,
        driver: CompiledActionDriver,
    ) -> TransactionResult:
        self._validate_prepared(prepared, plan, context, compilation)
        adapter = _CompiledDriverAdapter(
            driver,
            musical_plan=plan,
            compilation=compilation,
        )
        return self.song_transactions.run(
            prepared.song_binding,
            prepared.transaction_plan,
            adapter,
        )

    def history(self, prepared: PreparedMusicalTransaction) -> TransactionHistory:
        if not isinstance(prepared, PreparedMusicalTransaction):
            raise TypeError("prepared must be PreparedMusicalTransaction")
        return self.song_transactions.history(prepared.song_binding)
