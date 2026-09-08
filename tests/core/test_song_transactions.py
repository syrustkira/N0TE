import tempfile
import unittest
from pathlib import Path

from n0te import ActionIntent, AuthorityService, HeadquartersMemory
from n0te.song_transactions import (
    SongTransactionError,
    SongTransactionService,
    StaleSongTransactionError,
)
from n0te.transactions import (
    CompensationResult,
    PostconditionResult,
    StepExecution,
    TransactionPlan,
    TransactionReceipt,
    TransactionSnapshot,
    TransactionStep,
)


def local_intent(version_id: str) -> ActionIntent:
    return ActionIntent(
        action_id="action:song-transaction:bounded-edit",
        job_id="job:song-transaction:bounded-edit",
        action_class="REVERSIBLE",
        description="Apply one exact bounded Song edit",
        target_ref=f"version:{version_id}",
        revision_fingerprint="sha256:song-transaction-revision-v1",
        payload_fingerprint="sha256:song-transaction-plan-v1",
    )


class RecordingDriver:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def prepare_snapshot(self, plan):
        self.events.append(("snapshot", plan.transaction_id))
        return TransactionSnapshot(
            plan.transaction_id,
            plan.operation_id,
            "snapshot:song:1",
            "sha256:song-snapshot-v1",
            "evidence:song-snapshot:1",
        )

    def execute_step(self, step):
        self.events.append(("execute", step.step_id))
        return StepExecution(
            step.step_id,
            "SUCCEEDED",
            "APPLIED",
            f"evidence:execute:{step.step_id}",
            f"sha256:result:{step.step_id}",
        )

    def verify_postcondition(self, step, execution):
        self.events.append(("verify", step.step_id))
        return PostconditionResult(
            step.step_id,
            step.postcondition_ref,
            "SATISFIED",
            f"evidence:post:{step.step_id}",
        )

    def compensate_step(self, step, snapshot):
        self.events.append(("compensate", step.step_id))
        return CompensationResult(
            step.step_id,
            snapshot.snapshot_ref,
            "RESTORED",
            f"evidence:compensate:{step.step_id}",
        )

    def success_receipt(self, plan, snapshot):
        self.events.append(("receipt", plan.transaction_id))
        return TransactionReceipt(
            plan.transaction_id,
            plan.operation_id,
            snapshot.snapshot_ref,
            f"receipt:{plan.transaction_id}",
            f"evidence:success:{plan.transaction_id}",
            f"sha256:transaction:{plan.transaction_id}",
        )


class SongTransactionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.song_transactions = SongTransactionService(self.hq.transactions)
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Bound Song")
        self.version = self.hq.store.create_version(self.song.id, label="v1")

    def tearDown(self) -> None:
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    def claimed_operation(self, key: str):
        intent = local_intent(self.version.id)
        approval = AuthorityService.bind_approval(intent, f"artist-confirmation:{key}")
        operation = self.hq.operations.prepare(
            idempotency_key=key,
            intent=intent,
            approval=approval,
            song_id=self.song.id,
            version_id=self.version.id,
        )
        return self.hq.operations.claim_execution(
            operation.operation_id,
            intent=intent,
            approval=approval,
            claim_evidence_ref=f"execution-gate:{key}",
        )

    @staticmethod
    def plan(operation_id: str, transaction_id: str = "txn:song:1") -> TransactionPlan:
        return TransactionPlan(
            transaction_id,
            operation_id,
            (
                TransactionStep(
                    "step:song:1",
                    "Apply bounded Song change",
                    "post:song-change-present",
                    True,
                ),
            ),
        )

    def transaction_count(self) -> int:
        row = self.hq.store._conn.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()
        return int(row["count"])

    def test_exact_binding_runs_and_scoped_history_survives_restart(self):
        operation = self.claimed_operation("idem:song:success")
        plan = self.plan(operation.operation_id)
        binding = self.song_transactions.bind(plan)
        self.assertFalse(binding.action_authority_granted)
        self.assertEqual(binding.song_id, self.song.id)
        self.assertEqual(binding.target_version_id, self.version.id)
        self.assertEqual(binding.expected_current_version_id, self.version.id)

        driver = RecordingDriver()
        result = self.song_transactions.run(binding, plan, driver)
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.operation.recorded_state, "SUCCEEDED")
        self.assertEqual(
            driver.events,
            [
                ("snapshot", plan.transaction_id),
                ("execute", "step:song:1"),
                ("verify", "step:song:1"),
                ("receipt", plan.transaction_id),
            ],
        )
        self.assertEqual(
            self.song_transactions.history(binding).operation_id,
            operation.operation_id,
        )
        history = self.song_transactions.history_for_operation(operation.operation_id)
        self.assertIsNotNone(history)
        self.assertEqual(history.transaction_id, plan.transaction_id)

        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        self.song_transactions = SongTransactionService(self.hq.transactions)
        reopened = self.song_transactions.history_for_operation(operation.operation_id)
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.transaction_id, plan.transaction_id)
        self.assertEqual(reopened.operation.recorded_state, "SUCCEEDED")

    def test_switching_active_song_fails_before_driver_or_transaction_registration(self):
        operation = self.claimed_operation("idem:song:switch")
        plan = self.plan(operation.operation_id)
        binding = self.song_transactions.bind(plan)
        other = self.hq.store.create_song("Other Song")
        self.assertEqual(self.hq.store.active_song().id, other.id)
        driver = RecordingDriver()

        with self.assertRaises(StaleSongTransactionError):
            self.song_transactions.run(binding, plan, driver)

        self.assertEqual(driver.events, [])
        self.assertEqual(self.transaction_count(), 0)
        self.assertEqual(
            self.hq.operations.get(operation.operation_id).recorded_state,
            "EXECUTING",
        )

        self.hq.store.select_song(self.song.id)
        result = self.song_transactions.run(binding, plan, driver)
        self.assertEqual(result.status, "COMPLETE")

    def test_advancing_current_version_invalidates_prepared_song_binding(self):
        operation = self.claimed_operation("idem:song:version-moved")
        plan = self.plan(operation.operation_id)
        binding = self.song_transactions.bind(plan)
        newer = self.hq.store.create_version(
            self.song.id,
            label="v2",
            parent_version_id=self.version.id,
        )
        self.assertEqual(self.hq.store.active_song().current_version_id, newer.id)
        driver = RecordingDriver()

        with self.assertRaises(StaleSongTransactionError):
            self.song_transactions.run(binding, plan, driver)

        self.assertEqual(driver.events, [])
        self.assertEqual(self.transaction_count(), 0)
        self.assertEqual(
            self.hq.operations.get(operation.operation_id).recorded_state,
            "EXECUTING",
        )

    def test_plan_substitution_is_rejected_before_any_driver_callback(self):
        operation = self.claimed_operation("idem:song:plan-substitution")
        plan = self.plan(operation.operation_id)
        binding = self.song_transactions.bind(plan)
        substituted = TransactionPlan(
            "txn:song:substituted",
            operation.operation_id,
            (
                TransactionStep(
                    "step:song:alternate",
                    "Different bounded change",
                    "post:different-change-present",
                    True,
                ),
            ),
        )
        driver = RecordingDriver()

        with self.assertRaises(SongTransactionError):
            self.song_transactions.run(binding, substituted, driver)

        self.assertEqual(driver.events, [])
        self.assertEqual(self.transaction_count(), 0)

    def test_recovery_lookup_cannot_cross_active_song_scope(self):
        operation = self.claimed_operation("idem:song:scoped-history")
        plan = self.plan(operation.operation_id)
        binding = self.song_transactions.bind(plan)
        self.song_transactions.run(binding, plan, RecordingDriver())

        other = self.hq.store.create_song("Other Recovery Song")
        self.assertEqual(self.hq.store.active_song().id, other.id)
        with self.assertRaises(StaleSongTransactionError):
            self.song_transactions.history_for_active_song(plan.transaction_id)
        with self.assertRaises(StaleSongTransactionError):
            self.song_transactions.history_for_operation(operation.operation_id)

        self.hq.store.select_song(self.song.id)
        history = self.song_transactions.history_for_operation(operation.operation_id)
        self.assertIsNotNone(history)
        self.assertEqual(history.transaction_id, plan.transaction_id)

    def test_song_surface_rejects_unscoped_operation(self):
        intent = local_intent(self.version.id)
        approval = AuthorityService.bind_approval(intent, "artist-confirmation:unscoped")
        operation = self.hq.operations.prepare(
            idempotency_key="idem:song:unscoped",
            intent=intent,
            approval=approval,
        )
        operation = self.hq.operations.claim_execution(
            operation.operation_id,
            intent=intent,
            approval=approval,
            claim_evidence_ref="execution-gate:unscoped",
        )
        plan = self.plan(operation.operation_id)

        with self.assertRaises(SongTransactionError):
            self.song_transactions.bind(plan)

        self.assertEqual(self.transaction_count(), 0)


if __name__ == "__main__":
    unittest.main()
