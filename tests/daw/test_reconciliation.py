import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te.hosts import HostRuntimeIdentity
from n0te.lineage import LineageCorruptionError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.reconcile import (
    ReconciliationError,
    ReconciliationTarget,
)
from n0te.shadow import HostShadowError, ShadowEventInput


class ReconciliationTests(unittest.TestCase):
    KEY = "workspace.track.track:1.mute"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Reconciliation Song")
        self.other_song = self.hq.store.create_song("Other Song")
        self.runtime = self.make_runtime()
        self.workspace = self.hq.workspaces.create(
            self.song.id,
            runtime=self.runtime,
            location_ref="file:///song/project",
        )
        self.target = ReconciliationTarget(
            self.KEY,
            "TRACK",
            "track:1",
            "mute",
        )

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    @staticmethod
    def make_runtime(version="12.1"):
        return HostRuntimeIdentity.from_runtime_labels(
            host_family="ABLETON_LIVE",
            version=version,
            edition="Suite",
            os_name="Darwin",
            machine="arm64",
        )

    def shadow_binding(self):
        observation = self.hq.workspaces.state(self.workspace.id).current_observation
        return observation.id, observation.host_runtime_fingerprint

    def full_shadow(self, *, value_marker=object()):
        observation_id, fingerprint = self.shadow_binding()
        events = ()
        if value_marker.__class__ is not object:
            events = (
                ShadowEventInput(
                    "TRACK",
                    "track:1",
                    "mute",
                    "SET",
                    value_marker,
                    "host:track-1-mute",
                ),
            )
        return self.hq.shadow.record_batch(
            self.workspace.id,
            workspace_observation_id=observation_id,
            host_runtime_fingerprint=fingerprint,
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref="host:full-scan",
            verified=True,
            events=events,
        )

    def incremental_shadow(self, value):
        observation_id, fingerprint = self.shadow_binding()
        return self.hq.shadow.record_batch(
            self.workspace.id,
            workspace_observation_id=observation_id,
            host_runtime_fingerprint=fingerprint,
            coverage="INCREMENTAL",
            actor="HUMAN",
            evidence_ref="host:user-change",
            verified=True,
            events=(
                ShadowEventInput(
                    "TRACK",
                    "track:1",
                    "mute",
                    "SET",
                    value,
                    "host:user-mute-change",
                ),
            ),
        )

    def technical_claim(self, value, *, supersedes=()):
        return self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song.id,
            key=self.KEY,
            value=value,
            source_kind="USER_DECLARED",
            source_ref="song:technical-intent",
            confidence=1.0,
            twin_domain="TECHNICAL",
            supersedes=supersedes,
        )

    def test_match_host_only_song_only_conflict_and_unresolved_are_distinct(self):
        self.full_shadow(value_marker=False)
        canonical = self.technical_claim(False)
        match = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        self.assertEqual(match.status, "MATCH")
        self.assertEqual(match.canonical_claim_ids, (canonical.id,))
        self.assertFalse(match.needs_decision)

        host_only_target = ReconciliationTarget(
            "workspace.track.track:1.solo", "TRACK", "track:1", "mute"
        )
        host_only = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=host_only_target,
        )
        self.assertEqual(host_only.status, "HOST_ONLY")

        song_only_target = ReconciliationTarget(
            "workspace.track.track:2.mute", "TRACK", "track:2", "mute"
        )
        self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song.id,
            key=song_only_target.canonical_key,
            value=False,
            source_kind="USER_DECLARED",
            source_ref="song:track-2",
            confidence=1.0,
            twin_domain="TECHNICAL",
        )
        song_only = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=song_only_target,
        )
        self.assertEqual(song_only.status, "SONG_ONLY")

        conflict_target = ReconciliationTarget(
            "workspace.track.track:3.mute", "TRACK", "track:1", "mute"
        )
        self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song.id,
            key=conflict_target.canonical_key,
            value=True,
            source_kind="USER_DECLARED",
            source_ref="song:track-3",
            confidence=1.0,
            twin_domain="TECHNICAL",
        )
        conflict = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=conflict_target,
        )
        self.assertEqual(conflict.status, "CONFLICT")

        unresolved_target = ReconciliationTarget(
            "workspace.track.track:4.mute", "TRACK", "track:4", "mute"
        )
        for value in (False, True):
            self.hq.evidence.record_claim(
                scope_kind="SONG",
                scope_id=self.song.id,
                key=unresolved_target.canonical_key,
                value=value,
                source_kind="OBSERVED",
                source_ref=f"song:track-4:{value}",
                confidence=1.0,
                twin_domain="TECHNICAL",
            )
        unresolved = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=unresolved_target,
        )
        self.assertEqual(unresolved.status, "UNRESOLVED")
        self.assertEqual(len(unresolved.canonical_values), 2)

    def test_both_absent_match_cannot_open_case(self):
        self.full_shadow()
        empty_target = ReconciliationTarget(
            "workspace.track.track:9.mute", "TRACK", "track:9", "mute"
        )
        comparison = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=empty_target,
        )
        self.assertEqual(comparison.status, "MATCH")
        with self.assertRaises(ReconciliationError):
            self.hq.reconciliation.open_case(
                song_id=self.song.id,
                workspace_id=self.workspace.id,
                target=empty_target,
            )

    def test_conflict_case_snapshots_exact_provenance_and_decisions_do_not_execute(self):
        baseline = self.full_shadow(value_marker=True)
        claim = self.technical_claim(False)
        comparison = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        self.assertEqual(comparison.status, "CONFLICT")
        case = self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        self.assertEqual(case.canonical_claim_ids, (claim.id,))
        self.assertEqual(case.canonical_values, (False,))
        self.assertEqual(case.host_fact.value, True)
        self.assertEqual(case.host_fact.evidence_ref, "host:track-1-mute")
        self.assertEqual(case.host_baseline_batch_id, baseline.id)
        self.assertEqual(case.host_latest_batch_id, baseline.id)

        evidence_before = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM evidence_claims"
        ).fetchone()[0]
        shadow_before = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM host_shadow_batches"
        ).fetchone()[0]
        choices = (
            "UPDATE_SONG",
            "RESTORE_HOST",
            "KEEP_WORKSPACE_SPECIFIC",
            "DO_NOTHING",
        )
        for choice in choices:
            self.hq.reconciliation.record_decision(
                case.id,
                choice=choice,
                evidence_ref=f"user:choice:{choice.lower()}",
                rationale=f"exercise {choice}",
            )
        self.assertEqual(
            [item.choice for item in self.hq.reconciliation.decisions(case.id)],
            list(choices),
        )
        self.assertEqual(
            self.hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0],
            evidence_before,
        )
        self.assertEqual(
            self.hq.store._conn.execute("SELECT COUNT(*) FROM host_shadow_batches").fetchone()[0],
            shadow_before,
        )
        state = self.hq.reconciliation.state(case.id)
        self.assertEqual(state.status, "DECIDED")
        pending_ids = {
            item.case.id
            for item in self.hq.reconciliation.unresolved_for_workspace(self.workspace.id)
        }
        self.assertIn(case.id, pending_ids)

    def test_host_shadow_change_stales_case_and_blocks_later_decision(self):
        self.full_shadow(value_marker=True)
        self.technical_claim(False)
        case = self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        self.incremental_shadow(False)
        state = self.hq.reconciliation.state(case.id)
        self.assertEqual(state.status, "STALE")
        with self.assertRaises(ReconciliationError):
            self.hq.reconciliation.record_decision(
                case.id,
                choice="DO_NOTHING",
                evidence_ref="user:stale-choice",
            )

    def test_canonical_supersession_stales_case(self):
        self.full_shadow(value_marker=True)
        old = self.technical_claim(False)
        case = self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        new = self.technical_claim(True, supersedes=(old.id,))
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(self.hq.reconciliation.state(case.id).status, "STALE")

    def test_workspace_observation_change_makes_host_shadow_stale_and_compare_refuses(self):
        self.full_shadow(value_marker=True)
        self.technical_claim(False)
        self.hq.workspaces.reconcile_existing(
            self.workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.make_runtime("12.2"),
            location_ref="file:///song/project-moved",
        )
        with self.assertRaises(HostShadowError):
            self.hq.reconciliation.compare(
                song_id=self.song.id,
                workspace_id=self.workspace.id,
                target=self.target,
            )

    def test_choice_compatibility_prevents_execution_intent_without_source_truth(self):
        self.full_shadow(value_marker=True)
        host_only_target = ReconciliationTarget(
            "workspace.track.track:8.mute", "TRACK", "track:1", "mute"
        )
        host_only = self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=host_only_target,
        )
        with self.assertRaises(ReconciliationError):
            self.hq.reconciliation.record_decision(
                host_only.id,
                choice="RESTORE_HOST",
                evidence_ref="user:no-canonical-source",
            )

        song_only_target = ReconciliationTarget(
            "workspace.track.track:7.mute", "TRACK", "track:7", "mute"
        )
        self.hq.evidence.record_claim(
            scope_kind="SONG",
            scope_id=self.song.id,
            key=song_only_target.canonical_key,
            value=False,
            source_kind="USER_DECLARED",
            source_ref="song:track-7",
            confidence=1.0,
            twin_domain="TECHNICAL",
        )
        song_only = self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=song_only_target,
        )
        with self.assertRaises(ReconciliationError):
            self.hq.reconciliation.record_decision(
                song_only.id,
                choice="UPDATE_SONG",
                evidence_ref="user:no-host-source",
            )

    def test_cross_song_workspace_mix_is_rejected(self):
        self.full_shadow(value_marker=True)
        with self.assertRaises(ValidationError):
            self.hq.reconciliation.compare(
                song_id=self.other_song.id,
                workspace_id=self.workspace.id,
                target=self.target,
            )

    def test_comparison_reads_are_pure_activity_is_recorded_and_restart_preserves_case(self):
        self.full_shadow(value_marker=True)
        self.technical_claim(False)
        before = self.hq.store._conn.total_changes
        comparison = self.hq.reconciliation.compare(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        self.assertEqual(comparison.status, "CONFLICT")
        self.assertEqual(self.hq.store._conn.total_changes, before)

        checkpoint = self.hq.activity.checkpoint()
        case = self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        self.hq.reconciliation.record_decision(
            case.id,
            choice="KEEP_WORKSPACE_SPECIFIC",
            evidence_ref="user:keep-workspace-specific",
        )
        events = [
            event
            for event in self.hq.activity.for_song(
                self.song.id, after_sequence=checkpoint
            )
            if event.object_type == "RECONCILIATION_CASE"
        ]
        self.assertEqual(
            [event.event_type for event in events],
            ["RECONCILIATION_CASE_OPENED", "RECONCILIATION_DECISION_RECORDED"],
        )
        self.assertEqual(events[0].object_id, case.id)
        self.assertEqual(events[1].object_id, case.id)

        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        reopened = self.hq.reconciliation.state(case.id)
        self.assertEqual(reopened.status, "DECIDED")
        self.assertEqual(reopened.decisions[0].choice, "KEEP_WORKSPACE_SPECIFIC")

    def test_case_and_decision_rows_are_sql_immutable(self):
        self.full_shadow(value_marker=True)
        self.technical_claim(False)
        case = self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        decision = self.hq.reconciliation.record_decision(
            case.id,
            choice="DO_NOTHING",
            evidence_ref="user:do-nothing",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "UPDATE reconciliation_cases SET comparison_status='HOST_ONLY' WHERE id=?",
                (case.id,),
            )
        self.hq.store._conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "DELETE FROM reconciliation_decisions WHERE id=?", (decision.id,)
            )
        self.hq.store._conn.rollback()

    def test_reopen_rejects_missing_reconciliation_integrity_hook(self):
        self.full_shadow(value_marker=True)
        self.technical_claim(False)
        self.hq.reconciliation.open_case(
            song_id=self.song.id,
            workspace_id=self.workspace.id,
            target=self.target,
        )
        self.hq.store._conn.execute("DROP TRIGGER reconciliation_cases_immutable_delete")
        self.hq.store._conn.commit()
        self.hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, self.profile_id)


if __name__ == "__main__":
    unittest.main()
