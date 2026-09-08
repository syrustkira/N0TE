import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te import (
    CapabilityEvidenceError,
    HeadquartersMemory,
    LineageCorruptionError,
    N0TEableJob,
    ResolutionConstraints,
)
from n0te.hosts import HOST_FAMILIES, HostRuntimeIdentity


class CapabilityEnvironmentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Capability Song")

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    @staticmethod
    def runtime(
        family="ABLETON_LIVE",
        version="12.1",
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    ):
        kwargs = {}
        if family == "GENERIC_OTHER":
            kwargs["generic_host_label"] = "Other DAW"
        return HostRuntimeIdentity.from_runtime_labels(
            host_family=family,
            version=version,
            edition=edition,
            os_name=os_name,
            machine=machine,
            **kwargs,
        )

    def workspace(self, *, family="ABLETON_LIVE", location="file:///song/project"):
        return self.hq.workspaces.create(
            self.song.id,
            runtime=self.runtime(family=family),
            location_ref=location,
        )

    def record(
        self,
        workspace_id,
        *,
        route_id="host-native",
        route_kind="HOST_NATIVE",
        capability="track.read",
        availability="AVAILABLE",
        evidence_ref="evidence:probe-1",
        observed_at=100,
        **overrides,
    ):
        state = self.hq.workspaces.state(workspace_id)
        values = dict(
            expected_workspace_observation_id=state.current_observation.id,
            expected_host_runtime_fingerprint=state.current_observation.host_runtime_fingerprint,
            route_id=route_id,
            route_kind=route_kind,
            capability=capability,
            display_name="Native route",
            availability=availability,
            evidence_kind="RUNTIME_PROBE",
            evidence_ref=evidence_ref,
            observed_at_epoch_seconds=observed_at,
            task_fit=0.9,
            editability=0.8,
            locality=1.0,
            privacy=1.0,
            latency=0.9,
            reversibility=1.0,
            cost_efficiency=1.0,
            portability=0.6,
            paid=False,
        )
        values.update(overrides)
        return self.hq.capability_evidence.record(workspace_id, **values)

    def test_available_evidence_builds_existing_profile_and_resolver(self):
        workspace = self.workspace()
        item = self.record(workspace.id, observed_at=100)
        profile = self.hq.capability_evidence.profile(workspace.id, now_epoch_seconds=112)
        self.assertEqual(profile.host_label, "ABLETON_LIVE")
        candidate = profile.candidates[0]
        self.assertEqual(candidate.candidate_id, item.candidate_id)
        self.assertTrue(candidate.verified)
        self.assertTrue(candidate.compatible)
        self.assertEqual(candidate.evidence_age_seconds, 12)
        result = profile.resolve(
            N0TEableJob("job:track.read", "track.read", "Read the current track")
        )
        self.assertEqual(result.status, "RESOLVED")
        self.assertEqual(result.recommended.candidate.candidate_id, item.candidate_id)

    def test_environment_evidence_cannot_supply_artist_preference(self):
        workspace = self.workspace()
        self.record(workspace.id)
        columns = {
            str(row["name"])
            for row in self.hq.store._conn.execute(
                "PRAGMA table_info(capability_observations)"
            )
        }
        self.assertNotIn("user_preference", columns)
        candidate = self.hq.capability_evidence.profile(
            workspace.id, now_epoch_seconds=101
        ).candidates[0]
        self.assertEqual(candidate.user_preference, 0.5)

    def test_verified_unavailable_and_unknown_are_preserved_truthfully(self):
        workspace = self.workspace()
        unavailable = self.record(
            workspace.id,
            route_id="native-unavailable",
            capability="session.musician",
            availability="UNAVAILABLE",
            evidence_ref="evidence:negative-test",
        )
        unknown = self.record(
            workspace.id,
            route_id="native-unknown",
            capability="audio.transient-edit",
            availability="UNKNOWN",
            evidence_ref=None,
        )
        self.assertEqual(
            {item.id for item in self.hq.capability_evidence.state(workspace.id).current},
            {unavailable.id, unknown.id},
        )
        profile = self.hq.capability_evidence.profile(workspace.id, now_epoch_seconds=120)
        unavailable_result = profile.resolve(
            N0TEableJob("job:session", "session.musician", "Use host session musician")
        )
        self.assertEqual(unavailable_result.status, "UNAVAILABLE")
        self.assertIn("INCOMPATIBLE", unavailable_result.reason_codes)
        self.assertNotIn("UNVERIFIED", unavailable_result.reason_codes)
        unknown_result = profile.resolve(
            N0TEableJob("job:transient", "audio.transient-edit", "Edit transients")
        )
        self.assertEqual(unknown_result.status, "UNAVAILABLE")
        self.assertIn("UNVERIFIED", unknown_result.reason_codes)
        self.assertNotIn("INCOMPATIBLE", unknown_result.reason_codes)

    def test_verified_state_requires_evidence_reference(self):
        workspace = self.workspace()
        for availability in ("AVAILABLE", "UNAVAILABLE"):
            with self.subTest(availability=availability):
                with self.assertRaises(CapabilityEvidenceError):
                    self.record(
                        workspace.id,
                        availability=availability,
                        evidence_ref=None,
                    )
        self.assertEqual(self.hq.capability_evidence.history(workspace.id), ())

    def test_latest_append_only_fact_wins_within_same_environment(self):
        workspace = self.workspace()
        first = self.record(
            workspace.id,
            availability="AVAILABLE",
            evidence_ref="evidence:first",
            observed_at=100,
        )
        second = self.record(
            workspace.id,
            availability="UNAVAILABLE",
            evidence_ref="evidence:second",
            observed_at=110,
        )
        history = self.hq.capability_evidence.history(workspace.id)
        self.assertEqual([item.id for item in history], [first.id, second.id])
        self.assertEqual(self.hq.capability_evidence.state(workspace.id).current, (second,))

    def test_late_older_probe_cannot_overwrite_newer_current_evidence(self):
        workspace = self.workspace()
        current = self.record(workspace.id, observed_at=110)
        with self.assertRaises(CapabilityEvidenceError):
            self.record(
                workspace.id,
                availability="UNAVAILABLE",
                evidence_ref="evidence:late-old-result",
                observed_at=109,
            )
        self.assertEqual(self.hq.capability_evidence.state(workspace.id).current, (current,))
        self.assertEqual(len(self.hq.capability_evidence.history(workspace.id)), 1)

    def test_sql_trigger_also_rejects_regressing_probe_time(self):
        workspace = self.workspace()
        item = self.record(workspace.id, observed_at=110)
        columns = (
            "id,workspace_id,workspace_observation_id,host_runtime_fingerprint,route_id,"
            "route_kind,capability,display_name,brand,availability,evidence_kind,evidence_ref,"
            "observed_at_epoch_seconds,task_fit,editability,locality,privacy,latency,"
            "reversibility,cost_efficiency,portability,paid"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                f"INSERT INTO capability_observations({columns}) "
                f"SELECT ?,workspace_id,workspace_observation_id,host_runtime_fingerprint,"
                f"route_id,route_kind,capability,display_name,brand,availability,evidence_kind,"
                f"evidence_ref,109,task_fit,editability,locality,privacy,latency,reversibility,"
                f"cost_efficiency,portability,paid FROM capability_observations WHERE id=?",
                ("capev_direct_old", item.id),
            )
        self.hq.store._conn.rollback()
        self.assertEqual(len(self.hq.capability_evidence.history(workspace.id)), 1)

    def test_route_kind_is_stable_within_one_exact_environment(self):
        workspace = self.workspace()
        first = self.record(workspace.id, route_id="shared-route")
        with self.assertRaises(CapabilityEvidenceError):
            self.record(
                workspace.id,
                route_id="shared-route",
                route_kind="PROVIDER",
                capability="transport.read",
                evidence_ref="evidence:wrong-kind",
            )
        self.assertEqual(self.hq.capability_evidence.history(workspace.id), (first,))

    def test_workspace_observation_change_stales_prior_evidence_and_probe_race(self):
        workspace = self.workspace()
        original_state = self.hq.workspaces.state(workspace.id)
        old = self.record(workspace.id)
        self.hq.workspaces.reconcile_existing(
            workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime(version="12.2"),
            location_ref="file:///song/project",
        )
        state = self.hq.capability_evidence.state(workspace.id)
        self.assertEqual(state.current, ())
        self.assertEqual(state.stale_count, 1)
        self.assertEqual(self.hq.capability_evidence.history(workspace.id), (old,))
        self.assertEqual(
            self.hq.capability_evidence.profile(workspace.id, now_epoch_seconds=120).candidates,
            (),
        )
        with self.assertRaises(CapabilityEvidenceError):
            self.hq.capability_evidence.record(
                workspace.id,
                expected_workspace_observation_id=original_state.current_observation.id,
                expected_host_runtime_fingerprint=original_state.current_observation.host_runtime_fingerprint,
                route_id="late-probe",
                route_kind="HOST_NATIVE",
                capability="track.read",
                display_name="Late probe",
                availability="AVAILABLE",
                evidence_kind="RUNTIME_PROBE",
                evidence_ref="evidence:late",
                observed_at_epoch_seconds=115,
            )
        self.assertEqual(len(self.hq.capability_evidence.history(workspace.id)), 1)

    def test_same_route_can_supply_multiple_capabilities_without_candidate_collision(self):
        workspace = self.workspace()
        first = self.record(
            workspace.id,
            route_id="native-api",
            capability="track.read",
            evidence_ref="evidence:track",
        )
        second = self.record(
            workspace.id,
            route_id="native-api",
            capability="transport.read",
            evidence_ref="evidence:transport",
        )
        self.assertNotEqual(first.candidate_id, second.candidate_id)
        profile = self.hq.capability_evidence.profile(workspace.id, now_epoch_seconds=110)
        self.assertEqual(len(profile.candidates), 2)
        self.assertEqual(len({item.candidate_id for item in profile.candidates}), 2)

    def test_freshness_constraints_use_observation_time(self):
        workspace = self.workspace()
        self.record(workspace.id, observed_at=100)
        profile = self.hq.capability_evidence.profile(workspace.id, now_epoch_seconds=131)
        result = profile.resolve(
            N0TEableJob("job:track.read", "track.read", "Read track"),
            ResolutionConstraints(max_evidence_age_seconds=30),
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertIn("STALE_OR_UNKNOWN_EVIDENCE", result.reason_codes)
        with self.assertRaises(CapabilityEvidenceError):
            self.hq.capability_evidence.profile(workspace.id, now_epoch_seconds=99)

    def test_all_peer_hosts_use_the_same_evidence_contract(self):
        for index, family in enumerate(HOST_FAMILIES):
            with self.subTest(family=family):
                workspace = self.workspace(family=family, location=f"file:///peer/{index}")
                item = self.record(
                    workspace.id,
                    route_id=f"native-{index}",
                    evidence_ref=f"evidence:{family}",
                )
                state = self.hq.capability_evidence.state(workspace.id)
                self.assertEqual(state.host_family, family)
                profile = self.hq.capability_evidence.profile(
                    workspace.id, now_epoch_seconds=101
                )
                self.assertEqual(profile.host_label, family)
                self.assertEqual(profile.candidates[0].candidate_id, item.candidate_id)

    def test_cross_workspace_binding_is_rejected(self):
        first = self.workspace(location="file:///first")
        second = self.workspace(location="file:///second")
        second_state = self.hq.workspaces.state(second.id)
        with self.assertRaises(CapabilityEvidenceError):
            self.hq.capability_evidence.record(
                first.id,
                expected_workspace_observation_id=second_state.current_observation.id,
                expected_host_runtime_fingerprint=second_state.current_observation.host_runtime_fingerprint,
                route_id="cross",
                route_kind="HOST_NATIVE",
                capability="track.read",
                display_name="Cross workspace",
                availability="AVAILABLE",
                evidence_kind="RUNTIME_PROBE",
                evidence_ref="evidence:cross",
                observed_at_epoch_seconds=100,
            )
        self.assertEqual(self.hq.capability_evidence.history(first.id), ())

    def test_append_only_sql_and_activity_receipt(self):
        workspace = self.workspace()
        checkpoint = self.hq.activity.checkpoint()
        item = self.record(workspace.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "UPDATE capability_observations SET availability='UNKNOWN' WHERE id=?",
                (item.id,),
            )
        self.hq.store._conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "DELETE FROM capability_observations WHERE id=?", (item.id,)
            )
        self.hq.store._conn.rollback()
        events = [
            event
            for event in self.hq.activity.for_song(
                self.song.id, after_sequence=checkpoint
            )
            if event.object_type == "CAPABILITY_EVIDENCE"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].object_id, item.id)
        self.assertEqual(events[0].event_type, "CAPABILITY_EVIDENCE_RECORDED")
        self.assertEqual(events[0].payload["capability"], "track.read")

    def test_record_does_not_mutate_unrelated_product_state(self):
        workspace = self.workspace()
        tracked_tables = [
            "songs",
            "versions",
            "assets",
            "session_items",
            "host_shadow_batches",
            "operation_events",
        ]
        existing = {
            str(row["name"])
            for row in self.hq.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        tracked_tables = [name for name in tracked_tables if name in existing]
        before = {
            name: int(
                self.hq.store._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {name}"
                ).fetchone()["n"]
            )
            for name in tracked_tables
        }
        self.record(workspace.id)
        after = {
            name: int(
                self.hq.store._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {name}"
                ).fetchone()["n"]
            )
            for name in tracked_tables
        }
        self.assertEqual(after, before)
        self.assertEqual(self.hq.shadow.state(workspace.id).status, "EMPTY")

    def test_reopen_rejects_tampered_environment_binding(self):
        workspace = self.workspace()
        item = self.record(workspace.id)
        self.hq.store._conn.execute(
            "DROP TRIGGER capability_observations_immutable_update"
        )
        self.hq.store._conn.execute(
            "UPDATE capability_observations SET host_runtime_fingerprint=? WHERE id=?",
            ("f" * 64, item.id),
        )
        self.hq.store._conn.execute(
            """CREATE TRIGGER capability_observations_immutable_update
            BEFORE UPDATE ON capability_observations
            BEGIN SELECT RAISE(ABORT, 'capability evidence is append-only'); END"""
        )
        self.hq.store._conn.commit()
        self.hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, self.profile_id)


if __name__ == "__main__":
    unittest.main()
