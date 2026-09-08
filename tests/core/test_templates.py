import unittest

from n0te import (
    CapabilityCandidate,
    ResolutionConstraints,
    StudioCapabilityProfile,
    TemplateDefinition,
    TemplatePlanner,
    TemplateRole,
    TemplateValidationError,
)


def candidate(candidate_id, route_kind, capability, **overrides):
    values = dict(
        candidate_id=candidate_id,
        route_kind=route_kind,
        capability=capability,
        display_name=candidate_id,
        brand=None,
        verified=True,
        compatible=True,
        evidence_ref=f"evidence:{candidate_id}",
        evidence_age_seconds=10,
        task_fit=0.80,
        editability=0.80,
        locality=0.80,
        privacy=0.80,
        latency=0.80,
        reversibility=1.0,
        cost_efficiency=0.80,
        portability=0.80,
        user_preference=0.50,
        paid=False,
    )
    values.update(overrides)
    return CapabilityCandidate(**values)


def vocal_template(optional_harmony=True):
    return TemplateDefinition(
        template_id="template:vocal:start",
        family="vocal",
        name="Vocal Production Start",
        intent="Start a vocal production session with editing and optional harmony support",
        roles=(
            TemplateRole(
                role_id="lead-edit",
                capability="vocal.tighten",
                description="Tighten the lead vocal while preserving performance intent",
                required=True,
                tags=("lead", "editing"),
            ),
            TemplateRole(
                role_id="harmony-build",
                capability="vocal.harmony.build",
                description="Build supporting vocal harmonies",
                required=not optional_harmony,
                tags=("harmony", "support"),
            ),
        ),
    )


class Core03CHostNeutralTemplateTests(unittest.TestCase):
    def setUp(self):
        self.planner = TemplatePlanner()

    def test_template_normalizes_family_tags_and_role_order(self):
        template = vocal_template()
        self.assertEqual(template.family, "VOCAL")
        self.assertEqual(
            tuple(role.role_id for role in template.roles),
            ("harmony-build", "lead-edit"),
        )
        lead = next(role for role in template.roles if role.role_id == "lead-edit")
        self.assertEqual(lead.tags, ("editing", "lead"))

    def test_template_schema_contains_no_host_provider_candidate_or_track_identity(self):
        self.assertEqual(
            set(TemplateDefinition.__dataclass_fields__),
            {"template_id", "family", "name", "intent", "roles"},
        )
        self.assertEqual(
            set(TemplateRole.__dataclass_fields__),
            {"role_id", "capability", "description", "required", "tags"},
        )
        forbidden = {"host", "provider", "candidate", "track", "daw", "plugin"}
        self.assertFalse(
            forbidden & {name.lower() for name in TemplateDefinition.__dataclass_fields__}
        )
        self.assertFalse(forbidden & {name.lower() for name in TemplateRole.__dataclass_fields__})

    def test_duplicate_or_empty_roles_and_unknown_family_are_rejected(self):
        role = TemplateRole("lead", "vocal.tighten", "Tighten lead")
        with self.assertRaises(TemplateValidationError):
            TemplateDefinition("t", "VOCAL", "Name", "Intent", ())
        with self.assertRaises(TemplateValidationError):
            TemplateDefinition("t", "NOT_A_FAMILY", "Name", "Intent", (role,))
        with self.assertRaises(TemplateValidationError):
            TemplateDefinition("t", "VOCAL", "Name", "Intent", (role, role))

    def test_required_flag_must_be_a_real_boolean(self):
        with self.assertRaises(TypeError):
            TemplateRole(
                role_id="lead",
                capability="vocal.tighten",
                description="Tighten lead",
                required="false",
            )

    def test_full_plan_when_every_role_has_a_legitimate_route(self):
        template = vocal_template()
        studio = StudioCapabilityProfile.build(
            environment_id="studio:full",
            candidates=[
                candidate("lead-native", "HOST_NATIVE", "vocal.tighten", task_fit=0.95),
                candidate("harmony-n0te", "N0TE_NATIVE", "vocal.harmony.build", task_fit=0.90),
            ],
        )
        plan = self.planner.plan(template, studio)
        self.assertEqual(plan.status, "FULL")
        self.assertEqual(plan.unavailable_required_role_ids, ())
        self.assertEqual(plan.unavailable_optional_role_ids, ())
        self.assertTrue(all(item.available for item in plan.role_plans))

    def test_optional_gap_is_partial(self):
        template = vocal_template(optional_harmony=True)
        studio = StudioCapabilityProfile.build(
            environment_id="studio:partial",
            candidates=[candidate("lead", "GUIDED", "vocal.tighten")],
        )
        plan = self.planner.plan(template, studio)
        self.assertEqual(plan.status, "PARTIAL")
        self.assertEqual(plan.unavailable_required_role_ids, ())
        self.assertEqual(plan.unavailable_optional_role_ids, ("harmony-build",))
        harmony = next(item for item in plan.role_plans if item.role.role_id == "harmony-build")
        self.assertEqual(harmony.resolution.status, "UNAVAILABLE")
        self.assertIn("NO_CANDIDATES", harmony.resolution.reason_codes)

    def test_required_gap_makes_entire_plan_unavailable(self):
        template = vocal_template(optional_harmony=False)
        studio = StudioCapabilityProfile.build(
            environment_id="studio:missing-required",
            candidates=[candidate("lead", "GUIDED", "vocal.tighten")],
        )
        plan = self.planner.plan(template, studio)
        self.assertEqual(plan.status, "UNAVAILABLE")
        self.assertEqual(plan.unavailable_required_role_ids, ("harmony-build",))

    def test_same_template_identity_plans_differently_across_studios(self):
        template = vocal_template()
        studio_a = StudioCapabilityProfile.build(
            environment_id="studio:a",
            host_label="Ableton Live",
            candidates=[
                candidate("a-lead", "HOST_NATIVE", "vocal.tighten", task_fit=0.98),
                candidate("a-harmony", "GUIDED", "vocal.harmony.build"),
            ],
        )
        studio_b = StudioCapabilityProfile.build(
            environment_id="studio:b",
            host_label="Logic Pro",
            candidates=[
                candidate("b-lead", "N0TE_NATIVE", "vocal.tighten", task_fit=0.96),
                candidate("b-harmony", "OWNED_TOOL", "vocal.harmony.build"),
            ],
        )
        plan_a = self.planner.plan(template, studio_a)
        plan_b = self.planner.plan(template, studio_b)
        self.assertEqual(plan_a.template_id, plan_b.template_id, template.template_id)
        self.assertEqual(template, vocal_template())
        lead_a = next(item for item in plan_a.role_plans if item.role.role_id == "lead-edit")
        lead_b = next(item for item in plan_b.role_plans if item.role.role_id == "lead-edit")
        self.assertEqual(lead_a.resolution.recommended.candidate.route_kind, "HOST_NATIVE")
        self.assertEqual(lead_b.resolution.recommended.candidate.route_kind, "N0TE_NATIVE")

    def test_host_label_and_brand_do_not_change_plan_results(self):
        template = vocal_template()
        facts = [
            candidate("lead", "GUIDED", "vocal.tighten", brand="Brand A"),
            candidate("harmony", "GUIDED", "vocal.harmony.build", brand="Brand B"),
        ]
        left = StudioCapabilityProfile.build(
            environment_id="same", host_label="Ableton Live", candidates=facts
        )
        right = StudioCapabilityProfile.build(
            environment_id="same", host_label="Generic Other", candidates=facts
        )
        self.assertEqual(self.planner.plan(template, left), self.planner.plan(template, right))

    def test_unverified_candidate_remains_gap_inside_template_plan(self):
        template = TemplateDefinition(
            template_id="template:mix:start",
            family="MIX",
            name="Mix Start",
            intent="Prepare a reversible mix starting point",
            roles=(
                TemplateRole("repair", "audio.repair", "Repair obvious technical defects", True),
            ),
        )
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate(
                    "famous-installed-repair",
                    "OWNED_TOOL",
                    "audio.repair",
                    brand="Famous Brand",
                    verified=False,
                    evidence_ref=None,
                    task_fit=1.0,
                    user_preference=1.0,
                )
            ],
        )
        plan = self.planner.plan(template, studio)
        self.assertEqual(plan.status, "UNAVAILABLE")
        resolution = plan.role_plans[0].resolution
        self.assertIn("UNVERIFIED", resolution.reason_codes)

    def test_constraints_are_delegated_to_studio_resolver(self):
        template = TemplateDefinition(
            "template:content:start",
            "CONTENT",
            "Content Start",
            "Prepare one private local content draft",
            (TemplateRole("draft", "content.generate", "Generate a content draft", True),),
        )
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate(
                    "cloud",
                    "PROVIDER",
                    "content.generate",
                    locality=0.1,
                    privacy=0.2,
                    paid=True,
                )
            ],
        )
        plan = self.planner.plan(
            template,
            studio,
            ResolutionConstraints(min_locality=0.9, min_privacy=0.9, allow_paid=False),
        )
        self.assertEqual(plan.status, "UNAVAILABLE")
        reasons = set(plan.role_plans[0].resolution.reason_codes)
        self.assertTrue(
            {"LOCALITY_BELOW_MINIMUM", "PRIVACY_BELOW_MINIMUM", "PAID_ROUTE_NOT_ALLOWED"}
            <= reasons
        )

    def test_role_input_order_cannot_change_plan_order_or_result(self):
        a = TemplateRole("a", "cap.a", "Capability A", True)
        b = TemplateRole("b", "cap.b", "Capability B", False)
        first = TemplateDefinition("t", "SONG", "T", "I", (b, a))
        second = TemplateDefinition("t", "SONG", "T", "I", (a, b))
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[candidate("ca", "GUIDED", "cap.a")],
        )
        self.assertEqual(first, second)
        self.assertEqual(self.planner.plan(first, studio), self.planner.plan(second, studio))
        self.assertEqual(
            tuple(item.role.role_id for item in self.planner.plan(first, studio).role_plans),
            ("a", "b"),
        )

    def test_planning_is_pure_and_deterministic(self):
        template = vocal_template()
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate("lead", "GUIDED", "vocal.tighten"),
                candidate("harmony", "GUIDED", "vocal.harmony.build"),
            ],
        )
        before_template = template
        before_candidates = studio.candidates
        first = self.planner.plan(template, studio)
        second = self.planner.plan(template, studio)
        self.assertEqual(first, second)
        self.assertEqual(template, before_template)
        self.assertEqual(studio.candidates, before_candidates)


if __name__ == "__main__":
    unittest.main()
