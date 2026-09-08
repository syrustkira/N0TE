import tempfile
import unittest
from pathlib import Path

from n0te.focus import FocusContext, FocusDimension, FocusError, FocusUncertainError
from n0te.hosts import HostRuntimeIdentity
from n0te.lineage import ValidationError
from n0te.memory import HeadquartersMemory


class FocusContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.song = self.hq.store.create_song("Focus Song")
        self.other_song = self.hq.store.create_song("Other Song")
        self.runtime = self.make_runtime()
        self.workspace = self.hq.workspaces.create(
            self.song.id,
            runtime=self.runtime,
            location_ref="file:///focus/project",
        )

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    @staticmethod
    def make_runtime(family="ABLETON_LIVE", version="12.1", edition="Suite"):
        return HostRuntimeIdentity.from_runtime_labels(
            host_family=family,
            version=version,
            edition=edition,
            os_name="Darwin",
            machine="arm64",
        )

    def capture(self, dimensions=()):
        return self.hq.focus.capture(
            self.workspace.id,
            song_id=self.song.id,
            runtime=self.runtime,
            observation_evidence_ref="host-focus-read:1",
            dimensions=tuple(dimensions),
        )

    def test_exact_focus_returns_only_observed_exact_targets(self):
        context = self.capture(
            (
                FocusDimension("TRACK", "OBSERVED_EXACT", ("track:2",), "host:track"),
                FocusDimension("CLIP_REGION", "OBSERVED_EXACT", ("region:5",), "host:region"),
                FocusDimension("MIDI_NOTES", "OBSERVED_EXACT", ("note:1", "note:2"), "host:notes"),
                FocusDimension("DEVICE_PLUGIN", "OBSERVED_EXACT", ("device:7",), "host:device"),
            )
        )
        self.assertEqual(self.hq.focus.exact_refs(context, "TRACK"), ("track:2",))
        resolved = self.hq.focus.require_exact(context, "TRACK", "CLIP_REGION", "DEVICE_PLUGIN")
        self.assertEqual([item.dimension for item in resolved], ["TRACK", "CLIP_REGION", "DEVICE_PLUGIN"])

    def test_missing_ambiguous_inferred_and_unknown_focus_all_refuse_exact_targeting(self):
        cases = (
            ((), "AUTOMATION"),
            ((FocusDimension("TRACK", "OBSERVED_AMBIGUOUS", ("t1", "t2"), "host:amb"),), "TRACK"),
            ((FocusDimension("TRACK", "INFERRED", ("t1",), "inference:1"),), "TRACK"),
            ((FocusDimension("TRACK", "UNKNOWN", (), "host:none"),), "TRACK"),
        )
        for dimensions, required in cases:
            with self.subTest(dimensions=dimensions):
                context = self.capture(dimensions)
                with self.assertRaises(FocusUncertainError):
                    self.hq.focus.require_exact(context, required)

    def test_workspace_move_or_recovery_makes_prior_context_stale(self):
        context = self.capture((FocusDimension("TRACK", "OBSERVED_EXACT", ("t1",), "host:t1"),))
        self.hq.workspaces.reconcile_existing(
            self.workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime,
            location_ref="file:///focus/project-moved",
        )
        with self.assertRaises(FocusUncertainError) as captured:
            self.hq.focus.validate_current(context)
        self.assertEqual(captured.exception.reason, "STALE_WORKSPACE")

    def test_runtime_version_change_requires_workspace_observation_then_new_focus_capture(self):
        old_context = self.capture((FocusDimension("TRACK", "OBSERVED_EXACT", ("t1",), "host:t1"),))
        new_runtime = self.make_runtime(version="12.2")
        with self.assertRaises(FocusUncertainError):
            self.hq.focus.capture(
                self.workspace.id,
                song_id=self.song.id,
                runtime=new_runtime,
                observation_evidence_ref="host:new-runtime-before-workspace-observation",
            )
        self.hq.workspaces.reconcile_existing(
            self.workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=new_runtime,
            location_ref="file:///focus/project",
        )
        with self.assertRaises(FocusUncertainError):
            self.hq.focus.validate_current(old_context)
        fresh = self.hq.focus.capture(
            self.workspace.id,
            song_id=self.song.id,
            runtime=new_runtime,
            observation_evidence_ref="host:new-runtime",
            dimensions=(FocusDimension("TRACK", "OBSERVED_EXACT", ("t1",), "host:t1-new"),),
        )
        self.assertIs(self.hq.focus.validate_current(fresh), fresh)

    def test_cross_song_and_wrong_host_capture_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.hq.focus.capture(
                self.workspace.id,
                song_id=self.other_song.id,
                runtime=self.runtime,
                observation_evidence_ref="bad-song",
            )
        with self.assertRaises(FocusError):
            self.hq.focus.capture(
                self.workspace.id,
                song_id=self.song.id,
                runtime=self.make_runtime("REAPER", "7.2", "Standard"),
                observation_evidence_ref="bad-host",
            )

    def test_dimension_shape_and_duplicate_dimension_validation(self):
        invalid = (
            lambda: FocusDimension("TRACK", "UNKNOWN", ("t1",), "bad"),
            lambda: FocusDimension("TRACK", "OBSERVED_AMBIGUOUS", ("t1",), "bad"),
            lambda: FocusDimension("TRACK", "OBSERVED_EXACT", ("t1", "t2"), "bad"),
        )
        for factory in invalid:
            with self.assertRaises(FocusError):
                factory()
        observation = self.hq.workspaces.state(self.workspace.id).current_observation
        with self.assertRaises(FocusError):
            FocusContext(
                workspace_id=self.workspace.id,
                song_id=self.song.id,
                workspace_observation_id=observation.id,
                host_runtime_fingerprint=self.runtime.fingerprint,
                observation_evidence_ref="duplicate-dimension",
                dimensions=(
                    FocusDimension("TRACK", "OBSERVED_EXACT", ("t1",), "a"),
                    FocusDimension("TRACK", "OBSERVED_EXACT", ("t2",), "b"),
                ),
            )

    def test_focus_capture_validation_and_exact_reads_are_write_free(self):
        before = self.hq.store._conn.total_changes
        context = self.capture((FocusDimension("TRACK", "OBSERVED_EXACT", ("t1",), "host:t1"),))
        self.hq.focus.validate_current(context)
        self.hq.focus.require_exact(context, "TRACK")
        self.hq.focus.exact_refs(context, "TRACK")
        self.assertEqual(self.hq.store._conn.total_changes, before)

    def test_service_has_no_mutation_or_host_specific_selection_verbs(self):
        public = {
            name
            for name in dir(type(self.hq.focus))
            if not name.startswith("_") and callable(getattr(type(self.hq.focus), name))
        }
        self.assertEqual(public, {"capture", "validate_current", "require_exact", "exact_refs"})
        for forbidden in ("ableton", "logic", "fl_", "pro_tools", "reaper", "mutate", "write", "select_track"):
            self.assertFalse(any(forbidden in name.casefold() for name in public))


if __name__ == "__main__":
    unittest.main()
