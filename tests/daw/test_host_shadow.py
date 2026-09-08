import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te.hosts import HostRuntimeIdentity
from n0te.lineage import LineageCorruptionError
from n0te.memory import HeadquartersMemory
from n0te.shadow import HostShadowError, ShadowEventInput


class HostShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Shadow Song")
        self.other_song = self.hq.store.create_song("Other Song")
        self.runtime = self.make_runtime()
        self.workspace = self.hq.workspaces.create(
            self.song.id,
            runtime=self.runtime,
            location_ref="file:///song/project",
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

    def binding(self):
        observation = self.hq.workspaces.state(self.workspace.id).current_observation
        return observation.id, observation.host_runtime_fingerprint

    @staticmethod
    def event(
        kind="TRACK",
        ref="track:1",
        field="name",
        action="SET",
        value="Kick",
        evidence_ref=None,
    ):
        return ShadowEventInput(kind, ref, field, action, value, evidence_ref)

    def record(self, coverage, actor, evidence_ref, events=(), verified=True):
        observation_id, fingerprint = self.binding()
        return self.hq.shadow.record_batch(
            self.workspace.id,
            workspace_observation_id=observation_id,
            host_runtime_fingerprint=fingerprint,
            coverage=coverage,
            actor=actor,
            evidence_ref=evidence_ref,
            verified=verified,
            events=events,
        )

    def test_full_then_incremental_projects_verified_current_state_with_actor_and_evidence(self):
        self.assertEqual(self.hq.shadow.state(self.workspace.id).status, "EMPTY")
        full = self.record(
            "FULL",
            "EXTERNAL",
            "scan:full",
            events=(
                self.event(ref="track:1", field="name", value="Kick"),
                self.event(ref="track:1", field="mute", value=False),
                self.event(kind="TEMPO", ref="song", field="bpm", value=120.0),
            ),
        )
        incremental = self.record(
            "INCREMENTAL",
            "HUMAN",
            "host:event:22",
            events=(
                self.event(
                    ref="track:1",
                    field="mute",
                    value=True,
                    evidence_ref="host:mute",
                ),
                self.event(
                    kind="TEMPO",
                    ref="song",
                    field="bpm",
                    action="REMOVE",
                    value=None,
                ),
            ),
        )
        state = self.hq.shadow.require_current(self.workspace.id)
        self.assertEqual(state.baseline_batch_id, full.id)
        self.assertEqual(state.latest_batch_id, incremental.id)
        facts = {
            (fact.object_kind, fact.object_ref, fact.field): fact
            for fact in state.facts
        }
        self.assertEqual(facts[("TRACK", "track:1", "name")].value, "Kick")
        self.assertEqual(facts[("TRACK", "track:1", "name")].actor, "EXTERNAL")
        self.assertTrue(facts[("TRACK", "track:1", "mute")].value)
        self.assertEqual(facts[("TRACK", "track:1", "mute")].actor, "HUMAN")
        self.assertEqual(
            facts[("TRACK", "track:1", "mute")].evidence_ref, "host:mute"
        )
        self.assertNotIn(("TEMPO", "song", "bpm"), facts)

    def test_incremental_requires_current_full_and_unverified_observations_are_refused(self):
        with self.assertRaises(HostShadowError):
            self.record(
                "INCREMENTAL",
                "HUMAN",
                "host:delta",
                events=(self.event(),),
            )
        with self.assertRaises(HostShadowError):
            self.record("FULL", "EXTERNAL", "scan", verified=False)

    def test_later_full_resets_projection_instead_of_laundering_omitted_old_facts(self):
        self.record(
            "FULL",
            "EXTERNAL",
            "scan:1",
            events=(
                self.event(ref="track:1", field="name", value="Kick"),
                self.event(ref="track:1", field="mute", value=True),
            ),
        )
        latest_full = self.record(
            "FULL",
            "N0TE",
            "scan:2",
            events=(self.event(ref="track:1", field="mute", value=False),),
        )
        state = self.hq.shadow.state(self.workspace.id)
        self.assertEqual(state.baseline_batch_id, latest_full.id)
        self.assertEqual(
            [(fact.object_ref, fact.field, fact.value) for fact in state.facts],
            [("track:1", "mute", False)],
        )

    def test_workspace_move_or_runtime_observation_change_stales_shadow_until_fresh_full(self):
        self.record("FULL", "EXTERNAL", "scan:1", events=(self.event(),))
        self.hq.workspaces.reconcile_existing(
            self.workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime,
            location_ref="file:///song/project-moved",
        )
        self.assertEqual(self.hq.shadow.state(self.workspace.id).status, "STALE")
        with self.assertRaises(HostShadowError):
            self.record(
                "INCREMENTAL",
                "HUMAN",
                "delta:stale",
                events=(self.event(field="mute", value=True),),
            )
        self.record("FULL", "HUMAN", "scan:after-move", events=())
        self.assertEqual(self.hq.shadow.state(self.workspace.id).status, "CURRENT")

        newer_runtime = self.make_runtime("12.2")
        self.hq.workspaces.reconcile_existing(
            self.workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=newer_runtime,
            location_ref="file:///song/project-moved",
        )
        self.assertEqual(self.hq.shadow.state(self.workspace.id).status, "STALE")
        self.record(
            "FULL",
            "EXTERNAL",
            "scan:runtime-12.2",
            events=(
                self.event(
                    kind="ROUTING",
                    ref="bus:A",
                    field="destination",
                    value={"bus": "master", "gain": 0.0},
                ),
            ),
        )
        self.assertEqual(self.hq.shadow.state(self.workspace.id).status, "CURRENT")

    def test_workspace_isolation_restart_and_activity_history(self):
        checkpoint = self.hq.activity.checkpoint()
        first = self.record("FULL", "EXTERNAL", "scan:1", events=(self.event(),))
        other_workspace = self.hq.workspaces.create(
            self.other_song.id,
            runtime=self.runtime,
            location_ref="file:///other/project",
        )
        other_observation = self.hq.workspaces.state(other_workspace.id).current_observation
        self.hq.shadow.record_batch(
            other_workspace.id,
            workspace_observation_id=other_observation.id,
            host_runtime_fingerprint=other_observation.host_runtime_fingerprint,
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref="other:scan",
            verified=True,
            events=(self.event(ref="track:other", value="Other"),),
        )
        self.assertNotIn(
            "track:other", {fact.object_ref for fact in self.hq.shadow.state(self.workspace.id).facts}
        )
        events = [
            event
            for event in self.hq.activity.for_song(
                self.song.id, after_sequence=checkpoint
            )
            if event.object_type == "HOST_SHADOW_BATCH"
        ]
        self.assertEqual([event.object_id for event in events], [first.id])
        self.assertEqual(events[0].event_type, "HOST_SHADOW_FULL")
        self.assertEqual(events[0].payload, {"actor": "EXTERNAL", "coverage": "FULL"})

        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        state = self.hq.shadow.state(self.workspace.id)
        self.assertEqual(state.status, "CURRENT")
        self.assertEqual(state.facts[0].value, "Kick")

    def test_shadow_rows_are_immutable_and_reads_do_not_write_product_memory(self):
        batch = self.record("FULL", "EXTERNAL", "scan", events=(self.event(),))
        evidence_before = self.hq.store._conn.execute(
            "SELECT COUNT(*) FROM evidence_claims"
        ).fetchone()[0]
        songs_before = self.hq.store._conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        before = self.hq.store._conn.total_changes
        self.hq.shadow.state(self.workspace.id)
        self.hq.shadow.history(self.workspace.id)
        self.hq.shadow.require_current(self.workspace.id)
        self.assertEqual(self.hq.store._conn.total_changes, before)
        self.assertEqual(
            self.hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0],
            evidence_before,
        )
        self.assertEqual(
            self.hq.store._conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0],
            songs_before,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "UPDATE host_shadow_batches SET actor='HUMAN' WHERE id=?", (batch.id,)
            )
        self.hq.store._conn.rollback()
        event_id = self.hq.store._conn.execute(
            "SELECT id FROM host_shadow_events WHERE batch_id=? LIMIT 1", (batch.id,)
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "DELETE FROM host_shadow_events WHERE id=?", (event_id,)
            )
        self.hq.store._conn.rollback()

    def test_bounded_event_shape_and_duplicate_field_updates_are_refused(self):
        with self.assertRaises(HostShadowError):
            ShadowEventInput("SECRET", "x", "name", "SET", "value")
        with self.assertRaises(HostShadowError):
            ShadowEventInput("TRACK", "x", "name", "REMOVE", "value")
        with self.assertRaises(HostShadowError):
            ShadowEventInput("TRACK", "x", "meter", "SET", float("nan"))
        with self.assertRaises(HostShadowError):
            self.record(
                "FULL",
                "EXTERNAL",
                "scan",
                events=(self.event(), self.event(value="Snare")),
            )

    def test_reopen_rejects_missing_shadow_integrity_hook(self):
        self.record("FULL", "EXTERNAL", "scan", events=(self.event(),))
        self.hq.store._conn.execute("DROP TRIGGER host_shadow_events_immutable_delete")
        self.hq.store._conn.commit()
        self.hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, self.profile_id)


if __name__ == "__main__":
    unittest.main()
