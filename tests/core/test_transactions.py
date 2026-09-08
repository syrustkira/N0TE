import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import ActionIntent, AuthorityService, HeadquartersMemory, LineageCorruptionError
from n0te.transactions import (
    CompensationResult,
    PostconditionResult,
    StepExecution,
    TransactionError,
    TransactionPlan,
    TransactionReceipt,
    TransactionSnapshot,
    TransactionStep,
)


def local_intent(version_id: str) -> ActionIntent:
    return ActionIntent(
        action_id="action:local:bounded-edit",
        job_id="job:bounded-edit",
        action_class="REVERSIBLE",
        description="Apply one exact bounded local multi-step edit",
        target_ref=f"version:{version_id}",
        revision_fingerprint="sha256:txn-revision-v1",
        payload_fingerprint="sha256:txn-plan-v1",
    )


class FakeDriver:
    def __init__(self):
        self.events = []
        self.execute_overrides = {}
        self.post_overrides = {}
        self.comp_overrides = {}
        self.raise_phase = None
        self.wrong_identity = None

    def prepare_snapshot(self, plan):
        self.events.append(("snapshot", plan.transaction_id))
        if self.raise_phase == "snapshot":
            raise RuntimeError("snapshot failed")
        return TransactionSnapshot(
            "txn:wrong" if self.wrong_identity == "snapshot" else plan.transaction_id,
            plan.operation_id,
            "snapshot:1",
            "sha256:snapshot-v1",
            "evidence:snapshot:1",
        )

    def execute_step(self, step):
        self.events.append(("execute", step.step_id))
        if self.raise_phase == f"execute:{step.step_id}":
            raise RuntimeError("execution callback failed")
        if step.step_id in self.execute_overrides:
            return self.execute_overrides[step.step_id]
        return StepExecution(
            "step:wrong" if self.wrong_identity == f"execute:{step.step_id}" else step.step_id,
            "SUCCEEDED",
            "APPLIED",
            f"evidence:execute:{step.step_id}",
            f"sha256:result:{step.step_id}",
        )

    def verify_postcondition(self, step, execution):
        self.events.append(("verify", step.step_id))
        if self.raise_phase == f"verify:{step.step_id}":
            raise RuntimeError("postcondition callback failed")
        if step.step_id in self.post_overrides:
            return self.post_overrides[step.step_id]
        return PostconditionResult(
            step.step_id,
            step.postcondition_ref,
            "SATISFIED",
            f"evidence:post:{step.step_id}",
        )

    def compensate_step(self, step, snapshot):
        self.events.append(("compensate", step.step_id))
        if self.raise_phase == f"compensate:{step.step_id}":
            raise RuntimeError("compensation callback failed")
        if step.step_id in self.comp_overrides:
            return self.comp_overrides[step.step_id]
        return CompensationResult(
            step.step_id,
            snapshot.snapshot_ref,
            "RESTORED",
            f"evidence:compensate:{step.step_id}",
        )

    def success_receipt(self, plan, snapshot):
        self.events.append(("receipt", plan.transaction_id))
        if self.raise_phase == "receipt":
            raise RuntimeError("receipt callback failed")
        return TransactionReceipt(
            "txn:wrong" if self.wrong_identity == "receipt" else plan.transaction_id,
            plan.operation_id,
            snapshot.snapshot_ref,
            "receipt:transaction:1",
            "evidence:transaction:success",
            "sha256:transaction-result-v1",
        )


class Core04FTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Transaction Song")
        self.version = self.hq.store.create_version(self.song.id, label="v1")
        self.intent = local_intent(self.version.id)
        self.approval = AuthorityService.bind_approval(
            self.intent, "artist-confirmation:transaction:1"
        )

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    def claimed_operation(self, key="idem:transaction:1"):
        operation = self.hq.operations.prepare(
            idempotency_key=key,
            intent=self.intent,
            approval=self.approval,
            song_id=self.song.id,
            version_id=self.version.id,
        )
        return self.hq.operations.claim_execution(
            operation.operation_id,
            intent=self.intent,
            approval=self.approval,
            claim_evidence_ref="execution-gate:transaction:local",
        )

    @staticmethod
    def plan(operation_id, *, noncompensatable_first=False):
        return TransactionPlan(
            "txn:1",
            operation_id,
            (
                TransactionStep(
                    "step:1",
                    "Create bounded object",
                    "post:create-exists",
                    not noncompensatable_first,
                ),
                TransactionStep(
                    "step:2",
                    "Route bounded object",
                    "post:route-matches",
                    True,
                    ("step:1",),
                ),
                TransactionStep(
                    "step:3",
                    "Apply bounded value",
                    "post:value-matches",
                    True,
                    ("step:2",),
                ),
            ),
        )

    def test_full_success_is_ordered_verified_durable_and_activity_visible(self):
        operation = self.claimed_operation()
        plan = self.plan(operation.operation_id)
        driver = FakeDriver()
        result = self.hq.transactions.run(plan, driver)

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.executed_step_ids, ("step:1", "step:2", "step:3"))
        self.assertEqual(result.operation.recorded_state, "SUCCEEDED")
        self.assertEqual(
            driver.events,
            [
                ("snapshot", "txn:1"),
                ("execute", "step:1"),
                ("verify", "step:1"),
                ("execute", "step:2"),
                ("verify", "step:2"),
                ("execute", "step:3"),
                ("verify", "step:3"),
                ("receipt", "txn:1"),
            ],
        )

        history = self.hq.transactions.history("txn:1")
        self.assertFalse(history.requires_recovery_review)
        self.assertEqual(history.plan_fingerprint, plan.plan_fingerprint)
        self.assertEqual(history.unresolved_execution_step_ids, ())
        self.assertEqual(history.unresolved_compensation_step_ids, ())
        activity = [
            event.event_type
            for event in self.hq.activity.for_song(self.song.id)
            if event.object_type == "TRANSACTION" and event.object_id == "txn:1"
        ]
        self.assertEqual(activity[0], "TRANSACTION_PLAN_REGISTERED")
        self.assertIn("TRANSACTION_STEP_EXECUTION_RECORDED", activity)

        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        reopened = self.hq.transactions.history("txn:1")
        self.assertEqual(reopened.operation.recorded_state, "SUCCEEDED")
        self.assertEqual(reopened.steps, plan.steps)

    def test_middle_failure_compensates_current_and_prior_in_reverse_and_stops_forward(self):
        operation = self.claimed_operation("idem:transaction:middle-fail")
        plan = self.plan(operation.operation_id)
        driver = FakeDriver()
        driver.execute_overrides["step:2"] = StepExecution(
            "step:2", "FAILED", "APPLIED", "evidence:step2:failed"
        )

        result = self.hq.transactions.run(plan, driver)
        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(result.compensated_step_ids, ("step:2", "step:1"))
        self.assertEqual(result.operation.recorded_state, "FAILED")
        self.assertNotIn(("execute", "step:3"), driver.events)
        self.assertLess(
            driver.events.index(("compensate", "step:2")),
            driver.events.index(("compensate", "step:1")),
        )

    def test_unknown_change_state_never_retries_and_requires_recovery(self):
        operation = self.claimed_operation("idem:transaction:unknown")
        plan = self.plan(operation.operation_id)
        driver = FakeDriver()
        driver.execute_overrides["step:2"] = StepExecution(
            "step:2", "UNKNOWN", "UNKNOWN", "evidence:step2:ambiguous"
        )

        result = self.hq.transactions.run(plan, driver)
        self.assertEqual(result.status, "RECOVERY_REQUIRED")
        self.assertEqual(result.operation.recorded_state, "UNKNOWN")
        self.assertNotIn(("execute", "step:3"), driver.events)
        self.assertTrue(self.hq.transactions.history("txn:1").requires_recovery_review)

        reconciled = self.hq.operations.reconcile_unknown(
            operation.operation_id,
            observed_outcome="FAILED",
            evidence_ref="host-inspection:recovery-confirmed",
        )
        self.assertTrue(reconciled.reconciled)
        self.assertFalse(self.hq.transactions.history("txn:1").requires_recovery_review)

    def test_compensation_failure_or_noncompensatable_completed_step_requires_recovery(self):
        for noncompensatable, compensation_failure in ((False, True), (True, False)):
            with self.subTest(
                noncompensatable=noncompensatable,
                compensation_failure=compensation_failure,
            ):
                self.hq.close()
                self.hq = HeadquartersMemory.create(
                    self.root / f"case-{noncompensatable}-{compensation_failure}",
                    "Artist",
                )
                song = self.hq.store.create_song("Case Song")
                version = self.hq.store.create_version(song.id, label="v1")
                intent = local_intent(version.id)
                approval = AuthorityService.bind_approval(intent, "artist:case")
                operation = self.hq.operations.prepare(
                    idempotency_key="idem:case",
                    intent=intent,
                    approval=approval,
                    song_id=song.id,
                    version_id=version.id,
                )
                operation = self.hq.operations.claim_execution(
                    operation.operation_id,
                    intent=intent,
                    approval=approval,
                    claim_evidence_ref="gate:case",
                )
                plan = self.plan(
                    operation.operation_id,
                    noncompensatable_first=noncompensatable,
                )
                driver = FakeDriver()
                driver.execute_overrides["step:2"] = StepExecution(
                    "step:2", "FAILED", "NOT_APPLIED", "evidence:step2:known-failure"
                )
                if compensation_failure:
                    driver.comp_overrides["step:1"] = CompensationResult(
                        "step:1",
                        "snapshot:1",
                        "FAILED",
                        "evidence:compensation:failed",
                    )
                result = self.hq.transactions.run(plan, driver)
                self.assertEqual(result.status, "RECOVERY_REQUIRED")
                self.assertEqual(result.operation.recorded_state, "UNKNOWN")

    def test_missing_snapshot_or_bad_snapshot_identity_refuses_all_mutation(self):
        for mode in ("exception", "wrong-identity"):
            with self.subTest(mode=mode):
                self.hq.close()
                self.hq = HeadquartersMemory.create(self.root / mode, "Artist")
                song = self.hq.store.create_song("Snapshot Song")
                version = self.hq.store.create_version(song.id, label="v1")
                intent = local_intent(version.id)
                approval = AuthorityService.bind_approval(intent, "artist:snapshot")
                operation = self.hq.operations.prepare(
                    idempotency_key="idem:snapshot",
                    intent=intent,
                    approval=approval,
                )
                operation = self.hq.operations.claim_execution(
                    operation.operation_id,
                    intent=intent,
                    approval=approval,
                    claim_evidence_ref="gate:snapshot",
                )
                driver = FakeDriver()
                if mode == "exception":
                    driver.raise_phase = "snapshot"
                else:
                    driver.wrong_identity = "snapshot"
                result = self.hq.transactions.run(self.plan(operation.operation_id), driver)
                self.assertEqual(result.status, "COMPENSATED")
                self.assertEqual(result.executed_step_ids, ())
                self.assertFalse(any(event[0] == "execute" for event in driver.events))
                self.assertEqual(result.operation.recorded_state, "FAILED")
                self.assertEqual(
                    self.hq.transactions.history("txn:1").events[-1].event_type,
                    "SAFE_FAILURE",
                )

    def test_callback_exception_or_wrong_result_identity_never_becomes_success(self):
        for mode in ("execute-exception", "wrong-execution", "receipt-exception", "wrong-receipt"):
            with self.subTest(mode=mode):
                self.hq.close()
                self.hq = HeadquartersMemory.create(self.root / mode, "Artist")
                song = self.hq.store.create_song("Identity Song")
                version = self.hq.store.create_version(song.id, label="v1")
                intent = local_intent(version.id)
                approval = AuthorityService.bind_approval(intent, "artist:identity")
                operation = self.hq.operations.prepare(
                    idempotency_key="idem:identity",
                    intent=intent,
                    approval=approval,
                )
                operation = self.hq.operations.claim_execution(
                    operation.operation_id,
                    intent=intent,
                    approval=approval,
                    claim_evidence_ref="gate:identity",
                )
                driver = FakeDriver()
                if mode == "execute-exception":
                    driver.raise_phase = "execute:step:2"
                elif mode == "wrong-execution":
                    driver.wrong_identity = "execute:step:2"
                elif mode == "receipt-exception":
                    driver.raise_phase = "receipt"
                else:
                    driver.wrong_identity = "receipt"
                result = self.hq.transactions.run(self.plan(operation.operation_id), driver)
                self.assertNotEqual(result.status, "COMPLETE")
                self.assertNotEqual(result.operation.recorded_state, "SUCCEEDED")

    def test_plan_rejects_duplicate_or_forward_dependency(self):
        with self.assertRaises(TransactionError):
            TransactionPlan(
                "txn:bad",
                "op:bad",
                (
                    TransactionStep("step:x", "A", "post:a", True),
                    TransactionStep("step:x", "B", "post:b", True),
                ),
            )
        with self.assertRaises(TransactionError):
            TransactionPlan(
                "txn:bad",
                "op:bad",
                (
                    TransactionStep("step:a", "A", "post:a", True, ("step:b",)),
                    TransactionStep("step:b", "B", "post:b", True),
                ),
            )

    def test_crash_breadcrumb_survives_restart_and_blocks_blind_rerun(self):
        operation = self.claimed_operation("idem:transaction:crash")
        plan = self.plan(operation.operation_id)
        self.hq.transactions._register_plan(plan)
        with self.hq.store._tx():
            self.hq.transactions._append_event(
                "txn:1",
                "SNAPSHOT_CAPTURED",
                snapshot_ref="snapshot:1",
                snapshot_fingerprint="sha256:snapshot-v1",
                evidence_ref="evidence:snapshot:1",
            )
            self.hq.transactions._append_event(
                "txn:1",
                "STEP_EXECUTION_STARTED",
                step_id="step:1",
                snapshot_ref="snapshot:1",
                evidence_ref="evidence:step1:start",
            )

        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        history = self.hq.transactions.history("txn:1")
        self.assertEqual(history.unresolved_execution_step_ids, ("step:1",))
        self.assertTrue(history.requires_recovery_review)
        with self.assertRaises(TransactionError):
            self.hq.transactions.run(plan, FakeDriver())

    def test_transaction_plan_and_history_are_sql_immutable_and_corruption_fails_reopen(self):
        operation = self.claimed_operation("idem:transaction:immutable")
        self.hq.transactions.run(self.plan(operation.operation_id), FakeDriver())
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "UPDATE transaction_steps SET description='tampered' WHERE transaction_id='txn:1' AND ordinal=0"
            )
        self.hq.store._conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "DELETE FROM transaction_events WHERE transaction_id='txn:1'"
            )
        self.hq.store._conn.rollback()

        self.hq.store._conn.execute("DROP TRIGGER transactions_immutable_update")
        self.hq.store._conn.execute(
            "UPDATE transactions SET plan_fingerprint='tampered' WHERE id='txn:1'"
        )
        self.hq.store._conn.commit()
        self.hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, self.profile_id)


if __name__ == "__main__":
    unittest.main()
