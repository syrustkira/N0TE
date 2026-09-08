import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from n0te.authority import AuthorityService
from n0te.focus import FocusDimension, FocusUncertainError
from n0te.hosts import HostRuntimeIdentity
from n0te.memory import HeadquartersMemory
from n0te.musical_plan import (
    CompiledHostAction,
    HostCompilation,
    MusicalPlanService,
)
from n0te.musical_transactions import (
    MusicalTransactionError,
    MusicalTransactionService,
    compilation_fingerprint,
)
from n0te.song_transactions import SongTransactionService, StaleSongTransactionError
from n0te.transactions import (
    CompensationResult,
    PostconditionResult,
    StepExecution,
    TransactionReceipt,
    TransactionSnapshot,
)


class RecordingCompiledDriver:
    def __init__(self):
        self.events = []
        self.execute_overrides = {}
        self.post_overrides = {}
        self.comp_overrides = {}

    def prepare_snapshot(self, transaction_plan, musical_plan, compilation):
        self.events.append(
            (
                "snapshot",
                transaction_plan.transaction_id,
                musical_plan.plan_id,
                compilation.plan_id,
            )
        )
        return TransactionSnapshot(
            transaction_plan.transaction_id,
            transaction_plan.operation_id,
            "snapshot:musical:1",
            "sha256:musical-snapshot-v1",
            "evidence:musical-snapshot:1",
        )

    def execute_action(self, action):
        self.events.append(("execute", action.action_id, action.payload_ref))
        if action.action_id in self.execute_overrides:
            return self.execute_overrides[action.action_id]
        return StepExecution(
            action.action_id,
            "SUCCEEDED",
            "APPLIED",
            f"evidence:execute:{action.action_id}",
            f"sha256:result:{action.action_id}",
        )

    def verify_action(self, action, execution):
        self.events.append(("verify", action.action_id, action.postcondition_ref))
        if action.action_id in self.post_overrides:
            return self.post_overrides[action.action_id]
        return PostconditionResult(
            action.action_id,
            action.postcondition_ref,
            "SATISFIED",
            f"evidence:verify:{action.action_id}",
        )

    def compensate_action(self, action, snapshot):
        self.events.append(("compensate", action.action_id, action.payload_ref))
        if action.action_id in self.comp_overrides:
            return self.comp_overrides[action.action_id]
        return CompensationResult(
            action.action_id,
            snapshot.snapshot_ref,
            "RESTORED",
            f"evidence:compensate:{action.action_id}",
        )

    def success_receipt(
        self,
        transaction_plan,
        musical_plan,
        compilation,
        snapshot,
    ):
        self.events.append(
            (
                "receipt",
                transaction_plan.transaction_id,
                musical_plan.plan_id,
                compilation.plan_id,
            )
        )
        return TransactionReceipt(
            transaction_plan.transaction_id,
            transaction_plan.operation_id,
            snapshot.snapshot_ref,
            f"receipt:{transaction_plan.transaction_id}",
            f"evidence:success:{transaction_plan.transaction_id}",
            f"sha256:transaction:{transaction_plan.transaction_id}",
        )


class MusicalTransactionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.song = self.hq.store.create_song("Musical Transaction Song")
        self.version = self.hq.store.create_version(self.song.id, label="v1")
        self.runtime = HostRuntimeIdentity.from_runtime_labels(
            host_family="ABLETON_LIVE",
            version="12.1",
            edition="Suite",
            os_name="Darwin",
            machine="arm64",
        )
        self.workspace = self.hq.workspaces.create(
            self.song.id,
            runtime=self.runtime,
            location_ref="file:///musical-transaction/project",
        )
        self.plans = MusicalPlanService(self.hq.focus)
        self.song_transactions = SongTransactionService(self.hq.transactions)
        self.bridge = MusicalTransactionService(self.plans, self.song_transactions)

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    def context(self):
        return self.hq.focus.capture(
            self.workspace.id,
            song_id=self.song.id,
            runtime=self.runtime,
            observation_evidence_ref="host-focus:musical-transaction:1",
            dimensions=(
                FocusDimension(
                    "TRACK",
                    "OBSERVED_EXACT",
                    ("track:bass",),
                    "host-focus:track:bass",
                ),
                FocusDimension(
                    "SONG_SECTION",
                    "OBSERVED_EXACT",
                    ("section:chorus",),
                    "host-focus:section:chorus",
                ),
            ),
        )

    def plan(self, context=None):
        return self.plans.prepare(
            context or self.context(),
            plan_id="plan:chorus-bass-transaction",
            artist_intent="Make the chorus bass more confident without masking the vocal.",
            required_dimensions=("TRACK", "SONG_SECTION"),
            desired_change="Raise and shape the exact chorus bass target.",
            constraints=("Preserve vocal level", "Preserve chorus timing"),
            locked_element_refs=("track:vocal",),
            editable_element_refs=("track:bass",),
            verification_refs=("verify:bass-level", "verify:vocal-mask"),
            provenance_refs=("reasoning:chorus-bass:transaction",),
        )

    def compilation(self, plan, *, suffix="a", compensatable=True):
        return HostCompilation(
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            compiler_id="ableton-plan-compiler",
            compiler_version="1",
            host_family="ABLETON_LIVE",
            host_runtime_fingerprint=self.runtime.fingerprint,
            actions=(
                CompiledHostAction(
                    action_id="set-bass-level",
                    route_kind="HOST_NATIVE",
                    capability="track.level.set",
                    payload_ref=f"payload:{suffix}:bass-level",
                    postcondition_ref="verify:track-level:track:bass",
                    compensatable=compensatable,
                ),
                CompiledHostAction(
                    action_id="shape-bass-mask",
                    route_kind="HOST_NATIVE",
                    capability="eq.band.adjust",
                    payload_ref=f"payload:{suffix}:bass-mask",
                    postcondition_ref="verify:vocal-mask:clear",
                    compensatable=compensatable,
                    depends_on_action_ids=("set-bass-level",),
                ),
            ),
            evidence_ref=f"compiler-evidence:{suffix}",
        )

    def prepared(self, context=None, plan=None, compilation=None, *, key="idem:musical:1"):
        context = context or self.context()
        plan = plan or self.plan(context)
        compilation = compilation or self.compilation(plan)
        intent = self.bridge.preview(plan, context, compilation)
        approval = AuthorityService.bind_approval(
            intent,
            f"artist-confirmation:{key}",
        )
        prepared = self.bridge.prepare(
            plan,
            context,
            compilation,
            approval,
            idempotency_key=key,
            claim_evidence_ref=f"execution-gate:{key}",
        )
        return context, plan, compilation, intent, prepared

    def operation_count(self):
        row = self.hq.store._conn.execute(
            "SELECT COUNT(*) AS count FROM operations"
        ).fetchone()
        return int(row["count"])

    def transaction_count(self):
        row = self.hq.store._conn.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()
        return int(row["count"])

    def test_preview_is_write_free_and_binds_exact_plan_compilation_and_reversible_authority(self):
        context = self.context()
        plan = self.plan(context)
        compilation = self.compilation(plan)
        before = self.hq.store._conn.total_changes

        intent = self.bridge.preview(plan, context, compilation)

        self.assertEqual(self.hq.store._conn.total_changes, before)
        self.assertEqual(intent.action_class, "REVERSIBLE")
        self.assertEqual(intent.revision_fingerprint, plan.fingerprint)
        self.assertEqual(
            intent.payload_fingerprint,
            compilation_fingerprint(compilation),
        )
        self.assertEqual(intent.target_ref, f"song:{self.song.id}/musical-plan:{plan.plan_id}")
        self.assertEqual(self.operation_count(), 0)
        self.assertEqual(self.transaction_count(), 0)

    def test_stale_approval_after_compilation_change_refuses_before_operation_write(self):
        context = self.context()
        plan = self.plan(context)
        first = self.compilation(plan, suffix="first")
        approval = AuthorityService.bind_approval(
            self.bridge.preview(plan, context, first),
            "artist-confirmation:stale-compilation",
        )
        changed = self.compilation(plan, suffix="changed")

        with self.assertRaises(MusicalTransactionError):
            self.bridge.prepare(
                plan,
                context,
                changed,
                approval,
                idempotency_key="idem:stale-compilation",
                claim_evidence_ref="gate:stale-compilation",
            )

        self.assertEqual(self.operation_count(), 0)
        self.assertEqual(self.transaction_count(), 0)

    def test_prepare_claims_one_song_version_bound_operation_and_preserves_action_order(self):
        context, plan, compilation, intent, prepared = self.prepared()
        operation = self.hq.operations.get(prepared.transaction_plan.operation_id)

        self.assertEqual(operation.recorded_state, "EXECUTING")
        self.assertEqual(operation.song_id, self.song.id)
        self.assertEqual(operation.version_id, self.version.id)
        self.assertEqual(operation.intent_fingerprint, intent.intent_fingerprint)
        self.assertFalse(prepared.action_authority_granted)
        self.assertEqual(prepared.plan_fingerprint, plan.fingerprint)
        self.assertEqual(
            prepared.compilation_fingerprint,
            compilation_fingerprint(compilation),
        )
        self.assertEqual(
            [step.step_id for step in prepared.transaction_plan.steps],
            ["set-bass-level", "shape-bass-mask"],
        )
        self.assertEqual(
            prepared.transaction_plan.steps[1].depends_on_step_ids,
            ("set-bass-level",),
        )
        self.assertIn(
            compilation.actions[0].payload_ref,
            prepared.transaction_plan.steps[0].description,
        )
        self.assertEqual(self.transaction_count(), 0)

    def test_success_executes_exact_compiled_actions_through_existing_transaction_owner(self):
        context, plan, compilation, _, prepared = self.prepared(key="idem:success")
        driver = RecordingCompiledDriver()

        result = self.bridge.run(prepared, plan, context, compilation, driver)

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.operation.recorded_state, "SUCCEEDED")
        self.assertEqual(
            [event for event in driver.events if event[0] == "execute"],
            [
                ("execute", "set-bass-level", "payload:a:bass-level"),
                ("execute", "shape-bass-mask", "payload:a:bass-mask"),
            ],
        )
        self.assertEqual(
            [event[0] for event in driver.events],
            ["snapshot", "execute", "verify", "execute", "verify", "receipt"],
        )
        history = self.bridge.history(prepared)
        self.assertEqual(history.plan_fingerprint, prepared.transaction_plan.plan_fingerprint)
        self.assertFalse(history.requires_recovery_review)
        self.assertEqual(self.transaction_count(), 1)

    def test_known_failure_compensates_prior_change_and_records_failed_operation(self):
        context, plan, compilation, _, prepared = self.prepared(key="idem:compensate")
        driver = RecordingCompiledDriver()
        driver.execute_overrides["shape-bass-mask"] = StepExecution(
            "shape-bass-mask",
            "FAILED",
            "NOT_APPLIED",
            "evidence:mask:not-applied",
        )

        result = self.bridge.run(prepared, plan, context, compilation, driver)

        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(result.operation.recorded_state, "FAILED")
        self.assertEqual(result.compensated_step_ids, ("set-bass-level",))
        self.assertIn(
            ("compensate", "set-bass-level", "payload:a:bass-level"),
            driver.events,
        )
        self.assertFalse(self.bridge.history(prepared).requires_recovery_review)

    def test_unknown_execution_enters_recovery_and_existing_reconciliation_clears_review(self):
        context, plan, compilation, _, prepared = self.prepared(key="idem:unknown")
        driver = RecordingCompiledDriver()
        driver.execute_overrides["shape-bass-mask"] = StepExecution(
            "shape-bass-mask",
            "UNKNOWN",
            "UNKNOWN",
            "evidence:mask:unknown",
        )

        result = self.bridge.run(prepared, plan, context, compilation, driver)

        self.assertEqual(result.status, "RECOVERY_REQUIRED")
        self.assertEqual(result.operation.recorded_state, "UNKNOWN")
        history = self.bridge.history(prepared)
        self.assertTrue(history.requires_recovery_review)
        self.assertEqual(history.unresolved_execution_step_ids, ())

        reconciled = self.hq.operations.reconcile_unknown(
            result.operation_id,
            observed_outcome="FAILED",
            evidence_ref="host-inspection:musical-transaction:failed",
        )
        self.assertTrue(reconciled.reconciled)
        self.assertFalse(self.bridge.history(prepared).requires_recovery_review)

    def test_stale_focus_after_authority_preparation_refuses_before_driver_or_transaction_registration(self):
        context, plan, compilation, _, prepared = self.prepared(key="idem:stale-focus")
        self.hq.workspaces.reconcile_existing(
            self.workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime,
            location_ref="file:///musical-transaction/project-moved",
        )
        driver = RecordingCompiledDriver()

        with self.assertRaises(FocusUncertainError):
            self.bridge.run(prepared, plan, context, compilation, driver)

        self.assertEqual(driver.events, [])
        self.assertEqual(self.transaction_count(), 0)
        self.assertEqual(
            self.hq.operations.get(prepared.transaction_plan.operation_id).recorded_state,
            "EXECUTING",
        )

    def test_current_version_move_after_prepare_refuses_before_driver_or_transaction_registration(self):
        context, plan, compilation, _, prepared = self.prepared(key="idem:version-move")
        newer = self.hq.store.create_version(
            self.song.id,
            label="v2",
            parent_version_id=self.version.id,
        )
        self.assertEqual(self.hq.store.active_song().current_version_id, newer.id)
        driver = RecordingCompiledDriver()

        with self.assertRaises(StaleSongTransactionError):
            self.bridge.run(prepared, plan, context, compilation, driver)

        self.assertEqual(driver.events, [])
        self.assertEqual(self.transaction_count(), 0)

    def test_noncompensatable_compilation_is_outside_reversible_write_authority(self):
        context = self.context()
        plan = self.plan(context)
        compilation = self.compilation(plan, compensatable=False)

        with self.assertRaises(MusicalTransactionError):
            self.bridge.preview(plan, context, compilation)

        self.assertEqual(self.operation_count(), 0)
        self.assertEqual(self.transaction_count(), 0)

    def test_active_song_must_match_before_operation_preparation(self):
        context = self.context()
        plan = self.plan(context)
        compilation = self.compilation(plan)
        intent = self.bridge.preview(plan, context, compilation)
        approval = AuthorityService.bind_approval(intent, "artist-confirmation:wrong-song")
        other = self.hq.store.create_song("Other Active Song")
        self.assertEqual(self.hq.store.active_song().id, other.id)

        with self.assertRaises(MusicalTransactionError):
            self.bridge.prepare(
                plan,
                context,
                compilation,
                approval,
                idempotency_key="idem:wrong-song",
                claim_evidence_ref="gate:wrong-song",
            )

        self.assertEqual(self.operation_count(), 0)
        self.assertEqual(self.transaction_count(), 0)

    def test_prepared_identity_rejects_compilation_substitution_before_driver(self):
        context, plan, compilation, _, prepared = self.prepared(key="idem:substitution")
        changed = self.compilation(plan, suffix="substituted")
        driver = RecordingCompiledDriver()

        with self.assertRaises(MusicalTransactionError):
            self.bridge.run(prepared, plan, context, changed, driver)

        self.assertEqual(driver.events, [])
        self.assertEqual(self.transaction_count(), 0)
        self.assertEqual(
            self.hq.operations.get(prepared.transaction_plan.operation_id).recorded_state,
            "EXECUTING",
        )

    def test_public_surface_reuses_existing_authority_and_recovery_owners(self):
        public = {
            name
            for name in dir(MusicalTransactionService)
            if not name.startswith("_") and callable(getattr(MusicalTransactionService, name))
        }
        self.assertEqual(public, {"preview", "prepare", "run", "history"})
        for forbidden in (
            "approve",
            "grant_authority",
            "reconcile",
            "rollback",
            "write_receipt",
            "create_journal",
        ):
            self.assertFalse(any(forbidden in name.casefold() for name in public))


if __name__ == "__main__":
    unittest.main()
