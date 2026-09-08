import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from n0te.hosts import HostRuntimeIdentity
from n0te.lineage import LineageCorruptionError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.workspace import WorkspaceError, WorkspaceMemory


class WorkspaceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Workspace Song")
        self.other_song = self.hq.store.create_song("Other Song")

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
        edition="Suite",
        os_name="Darwin",
        machine="arm64",
    ):
        return HostRuntimeIdentity.from_runtime_labels(
            host_family=family,
            version=version,
            edition=edition,
            os_name=os_name,
            machine=machine,
        )

    def test_move_rename_and_recovery_preserve_workspace_identity(self):
        workspace = self.hq.workspaces.create(
            self.song.id,
            runtime=self.runtime(),
            location_ref="file:///studio/song-a/project-v1",
            display_name="Project V1",
        )
        moved = self.hq.workspaces.reconcile_existing(
            workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime(version="12.2"),
            location_ref="file:///archive/song-a/project-renamed",
            display_name="Project Renamed",
        )
        recovered = self.hq.workspaces.reconcile_existing(
            workspace.id,
            song_id=self.song.id,
            relation="RECOVERED",
            runtime=self.runtime(version="12.3"),
            location_ref="recovery://song-a/project-restored",
        )
        self.assertEqual(workspace.id, moved.id)
        self.assertEqual(workspace.id, recovered.id)
        state = self.hq.workspaces.state(workspace.id)
        self.assertEqual(state.current_observation.location_ref, "recovery://song-a/project-restored")
        self.assertEqual(
            [item.observation_kind for item in state.history],
            ["CREATED", "SAME_OR_MOVED", "RECOVERED"],
        )

    def test_duplicate_and_fork_create_distinct_identity_with_source_lineage(self):
        source = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/source"
        )
        duplicate = self.hq.workspaces.derive(
            source.id,
            song_id=self.song.id,
            relation="DUPLICATE",
            runtime=self.runtime(),
            location_ref="file:///song/copy",
        )
        fork = self.hq.workspaces.derive(
            source.id,
            song_id=self.song.id,
            relation="FORK",
            runtime=self.runtime("REAPER", "7.2", "Standard"),
            location_ref="file:///song/fork.rpp",
        )
        self.assertEqual({source.id, duplicate.id, fork.id}, {source.id, duplicate.id, fork.id})
        self.assertEqual(len({source.id, duplicate.id, fork.id}), 3)
        self.assertEqual((duplicate.source_workspace_id, duplicate.source_relation), (source.id, "DUPLICATE"))
        self.assertEqual((fork.source_workspace_id, fork.source_relation), (source.id, "FORK"))
        self.assertEqual(fork.host_family, "REAPER")

    def test_cross_song_lineage_and_existing_reconciliation_are_rejected(self):
        source = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/source"
        )
        with self.assertRaises(ValidationError):
            self.hq.workspaces.derive(
                source.id,
                song_id=self.other_song.id,
                relation="FORK",
                runtime=self.runtime(),
                location_ref="file:///other/cross-song",
            )
        with self.assertRaises(ValidationError):
            self.hq.workspaces.reconcile_existing(
                source.id,
                song_id=self.other_song.id,
                relation="SAME_OR_MOVED",
                runtime=self.runtime(),
                location_ref="file:///other/move",
            )

    def test_current_location_collision_refuses_merge_but_old_location_can_be_reused(self):
        first = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/original"
        )
        with self.assertRaises(WorkspaceError):
            self.hq.workspaces.create(
                self.song.id, runtime=self.runtime(), location_ref="file:///song/original"
            )
        self.hq.workspaces.reconcile_existing(
            first.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime(),
            location_ref="file:///song/moved",
        )
        second = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/original"
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            self.hq.workspaces.current_candidates_at_location("file:///song/original"),
            (second,),
        )

    def test_ambiguous_save_as_and_silent_host_family_change_are_rejected(self):
        workspace = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/source"
        )
        with self.assertRaises(WorkspaceError):
            self.hq.workspaces.reconcile_existing(
                workspace.id,
                song_id=self.song.id,
                relation="SAVE_AS",
                runtime=self.runtime(),
                location_ref="file:///song/save-as",
            )
        with self.assertRaises(WorkspaceError):
            self.hq.workspaces.reconcile_existing(
                workspace.id,
                song_id=self.song.id,
                relation="SAME_OR_MOVED",
                runtime=self.runtime("REAPER", "7.2", "Standard"),
                location_ref="file:///song/reaper",
            )

    def test_runtime_version_changes_do_not_regenerate_workspace_identity_across_restart(self):
        workspace = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(version="12.0"), location_ref="file:///song/project"
        )
        self.hq.workspaces.reconcile_existing(
            workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime(version="12.4"),
            location_ref="file:///song/project",
        )
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        reopened = self.hq.workspaces.state(workspace.id)
        self.assertEqual(reopened.workspace.id, workspace.id)
        self.assertEqual(reopened.current_observation.runtime_identity["version"], "12.4")

    def test_workspace_identity_and_observation_history_are_sql_immutable(self):
        workspace = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/project"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "UPDATE workspaces SET song_id=? WHERE id=?",
                (self.other_song.id, workspace.id),
            )
        self.hq.store._conn.rollback()
        observation_id = self.hq.workspaces.history(workspace.id)[0].id
        with self.assertRaises(sqlite3.IntegrityError):
            self.hq.store._conn.execute(
                "DELETE FROM workspace_observations WHERE id=?", (observation_id,)
            )
        self.hq.store._conn.rollback()

    def test_reads_are_pure_and_activity_records_explicit_workspace_events(self):
        checkpoint = self.hq.activity.checkpoint()
        workspace = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/project"
        )
        self.hq.workspaces.reconcile_existing(
            workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime(),
            location_ref="file:///song/project-moved",
        )
        before = self.hq.store._conn.total_changes
        self.hq.workspaces.get(workspace.id)
        self.hq.workspaces.state(workspace.id)
        self.hq.workspaces.history(workspace.id)
        self.hq.workspaces.current_candidates_at_location("file:///song/project-moved")
        self.assertEqual(self.hq.store._conn.total_changes, before)
        events = [
            event.event_type
            for event in self.hq.activity.for_song(self.song.id, after_sequence=checkpoint)
            if event.object_type == "WORKSPACE" and event.object_id == workspace.id
        ]
        self.assertEqual(events, ["WORKSPACE_CREATED", "WORKSPACE_SAME_OR_MOVED"])

    def test_reopen_rejects_tampered_runtime_identity_even_if_hook_is_restored(self):
        workspace = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/project"
        )
        row = self.hq.store._conn.execute(
            "SELECT id,runtime_identity_json FROM workspace_observations "
            "WHERE workspace_id=? ORDER BY seq LIMIT 1",
            (workspace.id,),
        ).fetchone()
        payload = json.loads(str(row["runtime_identity_json"]))
        payload["version"] = "999.0"
        self.hq.store._conn.execute("DROP TRIGGER workspace_observations_immutable_update")
        self.hq.store._conn.execute(
            "UPDATE workspace_observations SET runtime_identity_json=? WHERE id=?",
            (json.dumps(payload), row["id"]),
        )
        self.hq.store._conn.execute(
            """CREATE TRIGGER workspace_observations_immutable_update
            BEFORE UPDATE ON workspace_observations
            BEGIN SELECT RAISE(ABORT, 'workspace history is append-only'); END"""
        )
        self.hq.store._conn.commit()
        self.hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, self.profile_id)

    def test_reopen_rejects_cross_song_source_lineage_after_tampering(self):
        source = self.hq.workspaces.create(
            self.song.id, runtime=self.runtime(), location_ref="file:///song/source"
        )
        other = self.hq.workspaces.create(
            self.other_song.id, runtime=self.runtime(), location_ref="file:///other/source"
        )
        fork = self.hq.workspaces.derive(
            source.id,
            song_id=self.song.id,
            relation="FORK",
            runtime=self.runtime(),
            location_ref="file:///song/fork",
        )
        self.hq.store._conn.execute("DROP TRIGGER workspaces_immutable_update")
        self.hq.store._conn.execute(
            "UPDATE workspaces SET source_workspace_id=? WHERE id=?",
            (other.id, fork.id),
        )
        self.hq.store._conn.execute(
            """CREATE TRIGGER workspaces_immutable_update
            BEFORE UPDATE ON workspaces
            BEGIN SELECT RAISE(ABORT, 'workspace identity is immutable'); END"""
        )
        self.hq.store._conn.commit()
        self.hq.close()
        with self.assertRaises(LineageCorruptionError):
            HeadquartersMemory.open(self.root, self.profile_id)

    def test_public_api_exposes_location_candidates_not_path_identity_resolution(self):
        public = {
            name
            for name in dir(WorkspaceMemory)
            if not name.startswith("_") and callable(getattr(WorkspaceMemory, name))
        }
        self.assertIn("current_candidates_at_location", public)
        self.assertNotIn("get_by_path", public)
        self.assertNotIn("workspace_for_path", public)
        self.assertNotIn("resolve_path_identity", public)


if __name__ == "__main__":
    unittest.main()
