import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from n0te.focus import FocusDimension, FocusUncertainError
from n0te.hosts import HostRuntimeIdentity
from n0te.memory import HeadquartersMemory
from n0te.musical_plan import (
    CompiledHostAction,
    HostCompilation,
    MusicalPlanError,
    MusicalPlanService,
)


class FakeCompiler:
    compiler_id = "ableton-plan-compiler"
    compiler_version = "1"
    host_family = "ABLETON_LIVE"

    def __init__(self, *, mismatch=None):
        self.calls = 0
        self.mismatch = mismatch

    def compile(self, plan, runtime):
        self.calls += 1
        values = {
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "compiler_id": self.compiler_id,
            "compiler_version": self.compiler_version,
            "host_family": runtime.family,
            "host_runtime_fingerprint": runtime.fingerprint,
        }
        if self.mismatch is not None:
            field, value = self.mismatch
            values[field] = value
        return HostCompilation(
            **values,
            actions=(
                CompiledHostAction(
                    action_id="set-bass-level",
                    route_kind="HOST_NATIVE",
                    capability="track.level.set",
                    payload_ref="compiler-payload:ableton:set-bass-level",
                    postcondition_ref="verify:track-level:track:bass:-6db",
                    compensatable=True,
                ),
                CompiledHostAction(
                    action_id="verify-bass-balance",
                    route_kind="N0TE_NATIVE",
                    capability="audio.compare",
                    payload_ref="analysis:compare:bass-vocal-balance",
                    postcondition_ref="verify:balance:bass-vocal",
                    compensatable=False,
                    depends_on_action_ids=("set-bass-level",),
                ),
            ),
            evidence_ref="compiler-evidence:ableton:1",
        )


class WrongHostCompiler(FakeCompiler):
    compiler_id = "reaper-plan-compiler"
    host_family = "REAPER"


class MusicalPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hq = HeadquartersMemory.create(self.root, "Artist")
        self.song = self.hq.store.create_song("Plan Song")
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
            location_ref="file:///plan/project",
        )
        self.service = MusicalPlanService(self.hq.focus)

    def tearDown(self):
        try:
            self.hq.close()
        except Exception:
            pass
        self.temp.cleanup()

    def capture(self, dimensions):
        return self.hq.focus.capture(
            self.workspace.id,
            song_id=self.song.id,
            runtime=self.runtime,
            observation_evidence_ref="host-focus:plan:1",
            dimensions=tuple(dimensions),
        )

    def exact_context(self):
        return self.capture(
            (
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
            )
        )

    def prepare(self, context=None):
        return self.service.prepare(
            context or self.exact_context(),
            plan_id="plan:chorus-bass-balance",
            artist_intent="Make the chorus bass feel more confident without masking the vocal.",
            required_dimensions=("TRACK", "SONG_SECTION"),
            desired_change="Raise the exact chorus bass target while preserving vocal clarity.",
            constraints=("Preserve vocal level", "Preserve chorus timing"),
            locked_element_refs=("track:vocal",),
            editable_element_refs=("track:bass",),
            verification_refs=("verify:bass-level", "verify:vocal-mask"),
            provenance_refs=("reasoning:chorus-bass:1",),
        )

    def test_plan_preserves_host_neutral_musical_meaning_and_exact_focus(self):
        context = self.exact_context()
        plan = self.prepare(context)

        self.assertEqual(plan.song_id, self.song.id)
        self.assertEqual(plan.workspace_id, self.workspace.id)
        self.assertEqual(
            plan.workspace_observation_id,
            context.workspace_observation_id,
        )
        self.assertEqual(plan.host_runtime_fingerprint, self.runtime.fingerprint)
        self.assertEqual(plan.target("TRACK").refs, ("track:bass",))
        self.assertEqual(plan.target("SONG_SECTION").refs, ("section:chorus",))
        self.assertEqual(plan.section_ref, "section:chorus")
        self.assertEqual(plan.constraints, ("Preserve vocal level", "Preserve chorus timing"))
        self.assertEqual(plan.locked_element_refs, ("track:vocal",))
        self.assertEqual(plan.editable_element_refs, ("track:bass",))
        self.assertEqual(plan.verification_refs, ("verify:bass-level", "verify:vocal-mask"))
        self.assertIn("host-focus:plan:1", plan.provenance_refs)
        self.assertIn("host-focus:track:bass", plan.provenance_refs)
        self.assertIn("host-focus:section:chorus", plan.provenance_refs)
        self.assertIn("reasoning:chorus-bass:1", plan.provenance_refs)

        durable_fields = set(type(plan).__dataclass_fields__)
        for forbidden in ("command", "payload", "route_kind", "provider", "ableton", "reaper"):
            self.assertFalse(any(forbidden in name.casefold() for name in durable_fields))

    def test_plan_fingerprint_is_deterministic_and_semantic(self):
        plan = self.prepare()
        self.assertEqual(plan.fingerprint, plan.fingerprint)
        self.assertEqual(len(plan.fingerprint), 64)
        changed = replace(plan, desired_change="Lower the bass instead.")
        self.assertNotEqual(changed.fingerprint, plan.fingerprint)

    def test_scalar_text_is_rejected_for_every_sequence_input(self):
        context = self.exact_context()
        baseline = {
            "plan_id": "plan:shape-check",
            "artist_intent": "Make one exact bounded change.",
            "required_dimensions": ("TRACK", "SONG_SECTION"),
            "desired_change": "Change the exact target.",
            "constraints": (),
            "locked_element_refs": (),
            "editable_element_refs": (),
            "verification_refs": ("verify:shape",),
            "provenance_refs": (),
        }
        for field in (
            "constraints",
            "locked_element_refs",
            "editable_element_refs",
            "verification_refs",
            "provenance_refs",
        ):
            with self.subTest(field=field):
                kwargs = dict(baseline)
                kwargs[field] = "not-a-sequence"
                with self.assertRaises(MusicalPlanError):
                    self.service.prepare(context, **kwargs)

    def test_missing_ambiguous_inferred_or_unknown_target_refuses_plan_creation(self):
        cases = (
            (),
            (FocusDimension("TRACK", "OBSERVED_AMBIGUOUS", ("a", "b"), "focus:amb"),),
            (FocusDimension("TRACK", "INFERRED", ("a",), "focus:inferred"),),
            (FocusDimension("TRACK", "UNKNOWN", (), "focus:unknown"),),
        )
        for dimensions in cases:
            with self.subTest(dimensions=dimensions):
                context = self.capture(dimensions)
                with self.assertRaises(FocusUncertainError):
                    self.service.prepare(
                        context,
                        plan_id="plan:unsafe",
                        artist_intent="Change the selected track.",
                        required_dimensions=("TRACK",),
                        desired_change="Change it.",
                        verification_refs=("verify:track",),
                    )

    def test_stale_workspace_refuses_compilation_before_compiler_is_called(self):
        context = self.exact_context()
        plan = self.prepare(context)
        compiler = FakeCompiler()
        self.hq.workspaces.reconcile_existing(
            self.workspace.id,
            song_id=self.song.id,
            relation="SAME_OR_MOVED",
            runtime=self.runtime,
            location_ref="file:///plan/project-moved",
        )

        with self.assertRaises(FocusUncertainError) as captured:
            self.service.compile(plan, context, self.runtime, compiler)
        self.assertEqual(captured.exception.reason, "STALE_WORKSPACE")
        self.assertEqual(compiler.calls, 0)

    def test_matching_host_compiler_receives_portable_plan_and_returns_bound_actions(self):
        context = self.exact_context()
        plan = self.prepare(context)
        compiler = FakeCompiler()
        before = self.hq.store._conn.total_changes

        compilation = self.service.compile(plan, context, self.runtime, compiler)

        self.assertEqual(compiler.calls, 1)
        self.assertEqual(compilation.plan_id, plan.plan_id)
        self.assertEqual(compilation.plan_fingerprint, plan.fingerprint)
        self.assertEqual(compilation.host_family, "ABLETON_LIVE")
        self.assertEqual(compilation.host_runtime_fingerprint, self.runtime.fingerprint)
        self.assertEqual([action.action_id for action in compilation.actions], [
            "set-bass-level",
            "verify-bass-balance",
        ])
        self.assertEqual(compilation.actions[0].route_kind, "HOST_NATIVE")
        self.assertEqual(compilation.actions[1].depends_on_action_ids, ("set-bass-level",))
        self.assertEqual(self.hq.store._conn.total_changes, before)

    def test_wrong_host_or_mismatched_compilation_envelope_fails_closed(self):
        context = self.exact_context()
        plan = self.prepare(context)
        wrong_host = WrongHostCompiler()
        with self.assertRaises(MusicalPlanError):
            self.service.compile(plan, context, self.runtime, wrong_host)
        self.assertEqual(wrong_host.calls, 0)

        mismatched = FakeCompiler(mismatch=("plan_fingerprint", "wrong-fingerprint"))
        with self.assertRaises(MusicalPlanError):
            self.service.compile(plan, context, self.runtime, mismatched)
        self.assertEqual(mismatched.calls, 1)

    def test_compiled_action_dependencies_must_reference_prior_actions(self):
        with self.assertRaises(MusicalPlanError):
            HostCompilation(
                plan_id="plan:bad",
                plan_fingerprint="fingerprint",
                compiler_id="compiler",
                compiler_version="1",
                host_family="ABLETON_LIVE",
                host_runtime_fingerprint=self.runtime.fingerprint,
                actions=(
                    CompiledHostAction(
                        action_id="later",
                        route_kind="HOST_NATIVE",
                        capability="track.level.set",
                        payload_ref="payload:later",
                        postcondition_ref="verify:later",
                        compensatable=True,
                        depends_on_action_ids=("missing",),
                    ),
                ),
                evidence_ref="compiler:test",
            )

    def test_service_surface_contains_no_execution_or_mutation_verb(self):
        public = {
            name
            for name in dir(MusicalPlanService)
            if not name.startswith("_") and callable(getattr(MusicalPlanService, name))
        }
        self.assertEqual(public, {"prepare", "validate_current", "compile"})
        for forbidden in ("execute", "mutate", "write", "apply", "commit", "send"):
            self.assertFalse(any(forbidden in name.casefold() for name in public))


if __name__ == "__main__":
    unittest.main()
