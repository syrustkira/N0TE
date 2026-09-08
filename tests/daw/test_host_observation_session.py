import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from n0te import HeadquartersMemory, N0TEableJob, ValidationError
from n0te.focus import FocusDimension
from n0te.host_observation import (
    CapabilityFactInput,
    HostObservationError,
    ShadowObservationInput,
)
from n0te.hosts import HOST_FAMILIES, HostRuntimeIdentity
from n0te.shadow import ShadowEventInput


class HostObservationSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.profile_id = self.hq.store.profile_id
        self.song = self.hq.store.create_song("Observation Song")
        self.other_song = self.hq.store.create_song("Other Song")
        self.hq.store.select_song(self.song.id)

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    @staticmethod
    def runtime(family="ABLETON_LIVE", version="12.1"):
        kwargs = {}
        if family == "GENERIC_OTHER":
            kwargs["generic_host_label"] = "Other DAW"
        return HostRuntimeIdentity.from_runtime_labels(
            host_family=family,
            version=version,
            edition="Standard",
            os_name="Darwin",
            machine="arm64",
            **kwargs,
        )

    def workspace(self, *, family="ABLETON_LIVE", location="file:///song/project"):
        runtime = self.runtime(family)
        workspace = self.hq.workspaces.create(
            self.song.id,
            runtime=runtime,
            location_ref=location,
            state_fingerprint=f"state:{family}",
        )
        return workspace, runtime

    @staticmethod
    def capability(
        *,
        route_id="native-api",
        route_kind="HOST_NATIVE",
        capability="track.read",
        observed_at=100,
        evidence_ref="evidence:capability",
    ):
        return CapabilityFactInput(
            route_id=route_id,
            route_kind=route_kind,
            capability=capability,
            display_name="Native API",
            availability="AVAILABLE",
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
        )

    @staticmethod
    def focus_dimensions():
        return (
            FocusDimension(
                dimension="TRACK",
                state="OBSERVED_EXACT",
                refs=("track:1",),
                evidence_ref="evidence:focus-track",
            ),
            FocusDimension(
                dimension="PLAYHEAD",
                state="OBSERVED_EXACT",
                refs=("beat:33.0",),
                evidence_ref="evidence:focus-playhead",
            ),
        )

    @staticmethod
    def full_shadow():
        return ShadowObservationInput(
            coverage="FULL",
            actor="HUMAN",
            evidence_ref="evidence:shadow-full",
            events=(
                ShadowEventInput(
                    object_kind="TRACK",
                    object_ref="track:1",
                    field="name",
                    action="SET",
                    value="Lead Vocal",
                ),
                ShadowEventInput(
                    object_kind="TRANSPORT",
                    object_ref="transport:main",
                    field="playhead",
                    action="SET",
                    value=33.0,
                ),
            ),
        )

    def complete_session(self, workspace_id, runtime, *, now=101):
        binding = self.hq.host_observation.begin(
            workspace_id, song_id=self.song.id, runtime=runtime
        )
        return self.hq.host_observation.observe(
            binding,
            capabilities=(self.capability(),),
            focus_dimensions=self.focus_dimensions(),
            focus_evidence_ref="evidence:focus-snapshot",
            shadow=self.full_shadow(),
            now_epoch_seconds=now,
        )

    def test_complete_session_binds_all_observation_layers(self):
        workspace, runtime = self.workspace()
        result = self.complete_session(workspace.id, runtime)
        self.assertEqual(result.status, "COMPLETE")
        binding = result.binding
        self.assertEqual(result.workspace.workspace.id, workspace.id)
        self.assertEqual(
            result.workspace.current_observation.id,
            binding.workspace_observation_id,
        )
        self.assertEqual(
            result.capability_environment.workspace_observation_id,
            binding.workspace_observation_id,
        )
        self.assertEqual(
            result.focus.workspace_observation_id,
            binding.workspace_observation_id,
        )
        self.assertEqual(
            result.shadow.current_workspace_observation_id,
            binding.workspace_observation_id,
        )
        self.assertEqual(
            result.studio.environment_id,
            result.capability_environment.environment_id,
        )
        self.assertEqual(result.shadow.status, "CURRENT")
        self.assertEqual(len(result.recorded_capability_ids), 1)
        self.assertIsNotNone(result.recorded_shadow_batch_id)
        self.assertEqual(
            self.hq.focus.exact_refs(result.focus, "TRACK"),
            ("track:1",),
        )
        resolution = result.studio.resolve(
            N0TEableJob("job:track.read", "track.read", "Read track")
        )
        self.assertEqual(resolution.status, "RESOLVED")

    def test_no_current_shadow_is_truthfully_partial(self):
        workspace, runtime = self.workspace()
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        result = self.hq.host_observation.observe(
            binding,
            capabilities=(self.capability(),),
            focus_dimensions=self.focus_dimensions(),
            focus_evidence_ref="evidence:focus",
            shadow=None,
            now_epoch_seconds=101,
        )
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.shadow.status, "EMPTY")
        self.assertIsNone(result.recorded_shadow_batch_id)
        self.assertEqual(len(result.studio.candidates), 1)

    def test_current_shadow_without_capability_truth_is_truthfully_partial(self):
        workspace, runtime = self.workspace()
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        result = self.hq.host_observation.observe(
            binding,
            capabilities=(),
            focus_dimensions=self.focus_dimensions(),
            focus_evidence_ref="evidence:focus",
            shadow=self.full_shadow(),
            now_epoch_seconds=101,
        )
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.shadow.status, "CURRENT")
        self.assertEqual(result.capability_environment.current, ())
        self.assertEqual(result.studio.candidates, ())

    def test_stale_binding_rejects_before_new_observation_writes(self):
        workspace, runtime = self.workspace()
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        self.hq.workspaces.reconcile_existing(
            workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime(version="12.2"),
            location_ref="file:///song/project",
            state_fingerprint="state:new",
        )
        with self.assertRaises(HostObservationError):
            self.hq.host_observation.observe(
                binding,
                capabilities=(self.capability(),),
                focus_dimensions=self.focus_dimensions(),
                focus_evidence_ref="evidence:focus",
                shadow=self.full_shadow(),
                now_epoch_seconds=101,
            )
        self.assertEqual(self.hq.capability_evidence.history(workspace.id), ())
        self.assertEqual(self.hq.shadow.history(workspace.id), ())

    def test_begin_rejects_wrong_song_and_stale_runtime(self):
        workspace, runtime = self.workspace()
        with self.assertRaises(ValidationError):
            self.hq.host_observation.begin(
                workspace.id,
                song_id=self.other_song.id,
                runtime=runtime,
            )
        with self.assertRaises(HostObservationError):
            self.hq.host_observation.begin(
                workspace.id,
                song_id=self.song.id,
                runtime=self.runtime(version="12.2"),
            )

    def test_conflicting_route_kinds_fail_preflight_before_shadow_or_capability_write(self):
        workspace, runtime = self.workspace()
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        facts = (
            self.capability(route_id="route:1", route_kind="HOST_NATIVE"),
            self.capability(
                route_id="route:1",
                route_kind="PROVIDER",
                capability="transport.read",
                evidence_ref="evidence:provider",
            ),
        )
        with self.assertRaises(HostObservationError):
            self.hq.host_observation.observe(
                binding,
                capabilities=facts,
                focus_dimensions=self.focus_dimensions(),
                focus_evidence_ref="evidence:focus",
                shadow=self.full_shadow(),
                now_epoch_seconds=101,
            )
        self.assertEqual(self.hq.capability_evidence.history(workspace.id), ())
        self.assertEqual(self.hq.shadow.history(workspace.id), ())

    def test_incremental_shadow_without_baseline_fails_before_capability_write(self):
        workspace, runtime = self.workspace()
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        incremental = ShadowObservationInput(
            coverage="INCREMENTAL",
            actor="HUMAN",
            evidence_ref="evidence:incremental",
            events=(
                ShadowEventInput(
                    object_kind="TRACK",
                    object_ref="track:1",
                    field="name",
                    action="SET",
                    value="Changed",
                ),
            ),
        )
        with self.assertRaises(HostObservationError):
            self.hq.host_observation.observe(
                binding,
                capabilities=(self.capability(),),
                focus_dimensions=self.focus_dimensions(),
                focus_evidence_ref="evidence:focus",
                shadow=incremental,
                now_epoch_seconds=101,
            )
        self.assertEqual(self.hq.capability_evidence.history(workspace.id), ())

    def test_now_before_submitted_evidence_must_fail_before_writes(self):
        workspace, runtime = self.workspace()
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        with self.assertRaises(HostObservationError):
            self.hq.host_observation.observe(
                binding,
                capabilities=(self.capability(observed_at=110),),
                focus_dimensions=self.focus_dimensions(),
                focus_evidence_ref="evidence:focus",
                shadow=self.full_shadow(),
                now_epoch_seconds=109,
            )
        self.assertEqual(self.hq.capability_evidence.history(workspace.id), ())
        self.assertEqual(self.hq.shadow.history(workspace.id), ())

    def test_now_before_existing_current_evidence_fails_before_shadow_write(self):
        workspace, runtime = self.workspace()
        workspace_state = self.hq.workspaces.state(workspace.id)
        self.hq.capability_evidence.record(
            workspace.id,
            expected_workspace_observation_id=workspace_state.current_observation.id,
            expected_host_runtime_fingerprint=(
                workspace_state.current_observation.host_runtime_fingerprint
            ),
            route_id="native-api",
            route_kind="HOST_NATIVE",
            capability="track.read",
            display_name="Native API",
            availability="AVAILABLE",
            evidence_kind="RUNTIME_PROBE",
            evidence_ref="evidence:future-current",
            observed_at_epoch_seconds=110,
        )
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        with self.assertRaises(HostObservationError):
            self.hq.host_observation.observe(
                binding,
                capabilities=(),
                focus_dimensions=self.focus_dimensions(),
                focus_evidence_ref="evidence:focus",
                shadow=self.full_shadow(),
                now_epoch_seconds=109,
            )
        self.assertEqual(len(self.hq.capability_evidence.history(workspace.id)), 1)
        self.assertEqual(self.hq.shadow.history(workspace.id), ())

    def test_later_layer_failure_returns_no_session_but_preserves_truthful_prior_fact(self):
        workspace, runtime = self.workspace()
        binding = self.hq.host_observation.begin(
            workspace.id, song_id=self.song.id, runtime=runtime
        )
        with patch.object(
            self.hq.shadow,
            "record_batch",
            side_effect=RuntimeError("simulated shadow storage failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.hq.host_observation.observe(
                    binding,
                    capabilities=(self.capability(),),
                    focus_dimensions=self.focus_dimensions(),
                    focus_evidence_ref="evidence:focus",
                    shadow=self.full_shadow(),
                    now_epoch_seconds=101,
                )
        history = self.hq.capability_evidence.history(workspace.id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].capability, "track.read")
        self.assertEqual(self.hq.shadow.history(workspace.id), ())

    def test_all_peer_hosts_use_identical_session_contract(self):
        for index, family in enumerate(HOST_FAMILIES):
            with self.subTest(family=family):
                workspace, runtime = self.workspace(
                    family=family,
                    location=f"file:///peer/{index}",
                )
                result = self.complete_session(workspace.id, runtime)
                self.assertEqual(result.status, "COMPLETE")
                self.assertEqual(result.workspace.workspace.host_family, family)
                self.assertEqual(result.capability_environment.host_family, family)
                self.assertEqual(result.studio.host_label, family)

    def test_observation_session_does_not_mutate_song_or_operational_state(self):
        workspace, runtime = self.workspace()
        tracked = [
            "songs",
            "versions",
            "assets",
            "session_items",
            "operation_events",
            "evidence_claims",
        ]
        existing = {
            str(row["name"])
            for row in self.hq.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        tracked = [name for name in tracked if name in existing]
        before = {
            name: int(
                self.hq.store._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {name}"
                ).fetchone()["n"]
            )
            for name in tracked
        }
        self.complete_session(workspace.id, runtime)
        after = {
            name: int(
                self.hq.store._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {name}"
                ).fetchone()["n"]
            )
            for name in tracked
        }
        self.assertEqual(after, before)

    def test_reopen_preserves_observation_layers_and_allows_fresh_focus_capture(self):
        workspace, runtime = self.workspace()
        first = self.complete_session(workspace.id, runtime)
        workspace_id = workspace.id
        observation_id = first.binding.workspace_observation_id
        self.hq.close()
        self.hq = HeadquartersMemory.open(self.root, self.profile_id)
        binding = self.hq.host_observation.begin(
            workspace_id,
            song_id=self.song.id,
            runtime=runtime,
        )
        self.assertEqual(binding.workspace_observation_id, observation_id)
        self.assertEqual(
            self.hq.capability_evidence.state(workspace_id).workspace_observation_id,
            observation_id,
        )
        self.assertEqual(self.hq.shadow.state(workspace_id).status, "CURRENT")
        second = self.hq.host_observation.observe(
            binding,
            capabilities=(),
            focus_dimensions=self.focus_dimensions(),
            focus_evidence_ref="evidence:focus-after-reopen",
            shadow=None,
            now_epoch_seconds=102,
        )
        self.assertEqual(second.status, "COMPLETE")
        self.assertEqual(second.focus.workspace_observation_id, observation_id)


if __name__ == "__main__":
    unittest.main()
