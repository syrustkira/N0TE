from __future__ import annotations

from dataclasses import dataclass

from .transactions import (
    TransactionCoordinator,
    TransactionDriver,
    TransactionError,
    TransactionHistory,
    TransactionPlan,
    TransactionResult,
)


class SongTransactionError(TransactionError):
    """A transaction cannot be owned or recovered through the Song-scoped path."""


class StaleSongTransactionError(SongTransactionError):
    """The Song state moved after the exact transaction execution binding was prepared."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise SongTransactionError(f"{field} must not be empty")
    return text


@dataclass(frozen=True)
class SongTransactionBinding:
    """Read-only execution witness for one exact Song transaction plan.

    The binding grants no authority. Authority and execute-once ownership remain in
    OperationJournal. This witness only proves which Song state the caller saw when
    the already-authorized operation was connected to one immutable transaction plan.
    """

    profile_id: str
    song_id: str
    target_version_id: str | None
    expected_current_version_id: str | None
    operation_id: str
    transaction_id: str
    plan_fingerprint: str
    intent_fingerprint: str
    approval_id: str
    approval_source_ref: str
    action_authority_granted: bool = False

    def __post_init__(self) -> None:
        for field in (
            "profile_id",
            "song_id",
            "operation_id",
            "transaction_id",
            "plan_fingerprint",
            "intent_fingerprint",
            "approval_id",
            "approval_source_ref",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if self.target_version_id is not None:
            object.__setattr__(
                self,
                "target_version_id",
                _text(self.target_version_id, "target_version_id"),
            )
        if self.expected_current_version_id is not None:
            object.__setattr__(
                self,
                "expected_current_version_id",
                _text(self.expected_current_version_id, "expected_current_version_id"),
            )
        if self.action_authority_granted is not False:
            raise SongTransactionError("Song transaction bindings never grant action authority")


class SongTransactionService:
    """Exact Song ownership and stale-state guard over TransactionCoordinator.

    TransactionCoordinator remains the host-neutral choreography primitive. This
    service is the Artist/Song product boundary: a Song-bound operation must be
    connected to the active Song and the current Version state the caller actually
    saw before any transaction driver is allowed to mutate a host or provider.
    """

    def __init__(self, transactions: TransactionCoordinator):
        if not isinstance(transactions, TransactionCoordinator):
            raise TypeError("SongTransactionService requires TransactionCoordinator")
        self.transactions = transactions
        self.journal = transactions.journal
        self.store = transactions.store

    def _active_song_for(self, song_id: str):
        active = self.store.active_song()
        if active is None:
            raise StaleSongTransactionError(
                "No Song is active; select the bound Song before using this transaction."
            )
        if active.id != song_id:
            raise StaleSongTransactionError(
                "The active Song changed after transaction ownership was prepared."
            )
        return active

    def bind(self, plan: TransactionPlan) -> SongTransactionBinding:
        """Bind one already-claimed operation to the exact active Song/plan state."""

        if not isinstance(plan, TransactionPlan):
            raise TypeError("plan must be TransactionPlan")
        operation = self.journal.get(plan.operation_id)
        if operation.recorded_state != "EXECUTING":
            raise SongTransactionError(
                "Song transaction binding requires an already-claimed EXECUTING operation."
            )
        if operation.song_id is None:
            raise SongTransactionError(
                "SongTransactionService requires an operation explicitly bound to a Song."
            )
        active = self._active_song_for(operation.song_id)
        if operation.version_id is not None:
            version = self.store.get_version(operation.version_id)
            if version is None or version.song_id != operation.song_id:
                raise SongTransactionError(
                    "The operation target Version is no longer valid for its bound Song."
                )
        return SongTransactionBinding(
            profile_id=self.store.profile_id,
            song_id=operation.song_id,
            target_version_id=operation.version_id,
            expected_current_version_id=active.current_version_id,
            operation_id=operation.operation_id,
            transaction_id=plan.transaction_id,
            plan_fingerprint=plan.plan_fingerprint,
            intent_fingerprint=operation.intent_fingerprint,
            approval_id=operation.approval_id,
            approval_source_ref=operation.approval_source_ref,
        )

    def _validate_execution_binding(
        self,
        binding: SongTransactionBinding,
        plan: TransactionPlan,
    ) -> None:
        if not isinstance(binding, SongTransactionBinding):
            raise TypeError("binding must be SongTransactionBinding")
        if not isinstance(plan, TransactionPlan):
            raise TypeError("plan must be TransactionPlan")
        if binding.profile_id != self.store.profile_id:
            raise SongTransactionError("Song transaction binding belongs to a different Artist profile.")
        if (
            binding.transaction_id != plan.transaction_id
            or binding.operation_id != plan.operation_id
            or binding.plan_fingerprint != plan.plan_fingerprint
        ):
            raise SongTransactionError("Song transaction binding does not match this exact plan.")

        operation = self.journal.get(binding.operation_id)
        if operation.recorded_state != "EXECUTING":
            raise StaleSongTransactionError(
                f"The bound operation is no longer executable from state {operation.recorded_state}."
            )
        if (
            operation.song_id != binding.song_id
            or operation.version_id != binding.target_version_id
            or operation.intent_fingerprint != binding.intent_fingerprint
            or operation.approval_id != binding.approval_id
            or operation.approval_source_ref != binding.approval_source_ref
        ):
            raise SongTransactionError(
                "Song transaction ownership no longer matches the immutable operation identity."
            )

        active = self._active_song_for(binding.song_id)
        if active.current_version_id != binding.expected_current_version_id:
            raise StaleSongTransactionError(
                "The Song current Version changed after transaction ownership was prepared."
            )

    def run(
        self,
        binding: SongTransactionBinding,
        plan: TransactionPlan,
        driver: TransactionDriver,
    ) -> TransactionResult:
        """Execute through the canonical coordinator only after exact Song validation."""

        self._validate_execution_binding(binding, plan)
        return self.transactions.run(plan, driver)

    def history_for_active_song(self, transaction_id: str) -> TransactionHistory:
        """Read transaction recovery evidence only through its owning active Song."""

        history = self.transactions.history(_text(transaction_id, "transaction_id"))
        song_id = history.operation.song_id
        if song_id is None:
            raise SongTransactionError(
                "That transaction is not Song-bound and cannot use the Song recovery surface."
            )
        self._active_song_for(song_id)
        return history

    def history_for_operation(self, operation_id: str) -> TransactionHistory | None:
        """Resolve the durable transaction for one operation without crossing Song scope."""

        operation_id = _text(operation_id, "operation_id")
        operation = self.journal.get(operation_id)
        if operation.song_id is None:
            raise SongTransactionError(
                "That operation is not Song-bound and has no Song-scoped transaction history."
            )
        self._active_song_for(operation.song_id)
        row = self.transactions._conn.execute(
            "SELECT id FROM transactions WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        return self.history_for_active_song(str(row["id"]))

    def history(self, binding: SongTransactionBinding) -> TransactionHistory:
        """Read back one completed/recovering plan through its immutable ownership witness."""

        if not isinstance(binding, SongTransactionBinding):
            raise TypeError("binding must be SongTransactionBinding")
        if binding.profile_id != self.store.profile_id:
            raise SongTransactionError("Song transaction binding belongs to a different Artist profile.")
        history = self.history_for_active_song(binding.transaction_id)
        operation = history.operation
        if (
            history.operation_id != binding.operation_id
            or history.plan_fingerprint != binding.plan_fingerprint
            or operation.song_id != binding.song_id
            or operation.version_id != binding.target_version_id
            or operation.intent_fingerprint != binding.intent_fingerprint
            or operation.approval_id != binding.approval_id
            or operation.approval_source_ref != binding.approval_source_ref
        ):
            raise SongTransactionError(
                "Stored transaction history does not match the supplied Song ownership binding."
            )
        return history
