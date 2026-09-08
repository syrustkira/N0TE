import unittest

from n0te import (
    CapabilityCandidate,
    RecipeDefinition,
    RecipePlanner,
    RecipeStep,
    RecipeValidationError,
    ResolutionConstraints,
    StudioCapabilityProfile,
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


def vocal_recipe():
    return RecipeDefinition(
        recipe_id="recipe:vocal:tighten-chorus",
        name="Tighten Chorus Vocals",
        intent="Inspect and prepare a bounded vocal-tightening job without granting execution authority",
        steps=(
            RecipeStep(
                step_id="inspect",
                capability="vocal.timing.inspect",
                description="Inspect chorus vocal timing relationships",
                authority_class="read_only",
                postconditions=("timing relationships are inspectable",),
                recovery_policy="none",
            ),
            RecipeStep(
                step_id="tighten",
                capability="vocal.tighten",
                description="Prepare a reversible timing correction candidate",
                authority_class="reversible_mutation",
                postconditions=("original remains recoverable", "candidate timing is inspectable"),
                recovery_policy="rollback_required",
                depends_on=("inspect",),
            ),
            RecipeStep(
                step_id="compare",
                capability="audio.compare",
                description="Compare original and tightened candidates",
                authority_class="read_only",
                postconditions=("comparison result is inspectable",),
                recovery_policy="retry_safe",
                depends_on=("tighten",),
            ),
        ),
    )


class Core03DHostNeutralRecipeTests(unittest.TestCase):
    def setUp(self):
        self.planner = RecipePlanner()

    def test_recipe_preserves_declared_step_order(self):
        recipe = vocal_recipe()
        self.assertEqual(
            tuple(step.step_id for step in recipe.steps),
            ("inspect", "tighten", "compare"),
        )
        self.assertEqual(recipe.steps[1].depends_on, ("inspect",))
        self.assertEqual(recipe.steps[1].authority_class, "REVERSIBLE_MUTATION")
        self.assertEqual(recipe.steps[1].recovery_policy, "ROLLBACK_REQUIRED")

    def test_recipe_schema_contains_no_host_provider_candidate_track_or_approval_fields(self):
        self.assertEqual(
            set(RecipeDefinition.__dataclass_fields__),
            {"recipe_id", "name", "intent", "steps"},
        )
        self.assertEqual(
            set(RecipeStep.__dataclass_fields__),
            {
                "step_id",
                "capability",
                "description",
                "authority_class",
                "postconditions",
                "recovery_policy",
                "depends_on",
            },
        )
        forbidden = {"host", "provider", "candidate", "track", "plugin", "approved", "authorization"}
        fields = {
            name.lower()
            for name in (*RecipeDefinition.__dataclass_fields__, *RecipeStep.__dataclass_fields__)
        }
        self.assertFalse(fields & forbidden)

    def test_duplicate_step_ids_are_rejected(self):
        step = RecipeStep(
            "x", "cap.x", "X", "READ_ONLY", ("x inspected",), "NONE"
        )
        with self.assertRaises(RecipeValidationError):
            RecipeDefinition("r", "R", "Intent", (step, step))

    def test_unknown_or_forward_dependency_is_rejected(self):
        with self.assertRaises(RecipeValidationError):
            RecipeDefinition(
                "r",
                "R",
                "Intent",
                (
                    RecipeStep(
                        "first",
                        "cap.first",
                        "First",
                        "READ_ONLY",
                        ("first inspected",),
                        "NONE",
                        depends_on=("second",),
                    ),
                    RecipeStep(
                        "second",
                        "cap.second",
                        "Second",
                        "READ_ONLY",
                        ("second inspected",),
                        "NONE",
                    ),
                ),
            )

        with self.assertRaises(RecipeValidationError):
            RecipeDefinition(
                "r",
                "R",
                "Intent",
                (
                    RecipeStep(
                        "first",
                        "cap.first",
                        "First",
                        "READ_ONLY",
                        ("first inspected",),
                        "NONE",
                        depends_on=("missing",),
                    ),
                ),
            )

    def test_missing_postconditions_and_invalid_authority_or_recovery_are_rejected(self):
        with self.assertRaises(RecipeValidationError):
            RecipeStep("x", "cap.x", "X", "READ_ONLY", (), "NONE")
        with self.assertRaises(RecipeValidationError):
            RecipeStep("x", "cap.x", "X", "APPROVED", ("done",), "NONE")
        with self.assertRaises(RecipeValidationError):
            RecipeStep("x", "cap.x", "X", "READ_ONLY", ("done",), "MAGIC_RECOVERY")

    def test_all_resolved_steps_make_recipe_ready(self):
        recipe = vocal_recipe()
        studio = StudioCapabilityProfile.build(
            environment_id="studio:ready",
            candidates=[
                candidate("inspect", "GUIDED", "vocal.timing.inspect"),
                candidate("tighten", "HOST_NATIVE", "vocal.tighten", task_fit=0.95),
                candidate("compare", "N0TE_NATIVE", "audio.compare"),
            ],
        )
        plan = self.planner.plan(recipe, studio)
        self.assertEqual(plan.status, "READY")
        self.assertEqual(plan.unavailable_step_ids, ())
        self.assertEqual(
            tuple(item.step.step_id for item in plan.step_plans),
            ("inspect", "tighten", "compare"),
        )
        self.assertEqual(
            plan.declared_authority_classes,
            ("READ_ONLY", "REVERSIBLE_MUTATION"),
        )

    def test_one_unavailable_step_makes_recipe_unavailable(self):
        recipe = vocal_recipe()
        studio = StudioCapabilityProfile.build(
            environment_id="studio:missing",
            candidates=[
                candidate("inspect", "GUIDED", "vocal.timing.inspect"),
                candidate("compare", "GUIDED", "audio.compare"),
            ],
        )
        plan = self.planner.plan(recipe, studio)
        self.assertEqual(plan.status, "UNAVAILABLE")
        self.assertEqual(plan.unavailable_step_ids, ("tighten",))
        missing = next(item for item in plan.step_plans if item.step.step_id == "tighten")
        self.assertIn("NO_CANDIDATES", missing.resolution.reason_codes)

    def test_same_recipe_identity_can_use_different_routes_across_studios(self):
        recipe = vocal_recipe()
        studio_a = StudioCapabilityProfile.build(
            environment_id="studio:a",
            host_label="Ableton Live",
            candidates=[
                candidate("a-inspect", "HOST_NATIVE", "vocal.timing.inspect", task_fit=0.96),
                candidate("a-tighten", "HOST_NATIVE", "vocal.tighten", task_fit=0.96),
                candidate("a-compare", "GUIDED", "audio.compare"),
            ],
        )
        studio_b = StudioCapabilityProfile.build(
            environment_id="studio:b",
            host_label="Logic Pro",
            candidates=[
                candidate("b-inspect", "N0TE_NATIVE", "vocal.timing.inspect", task_fit=0.94),
                candidate("b-tighten", "OWNED_TOOL", "vocal.tighten", task_fit=0.92),
                candidate("b-compare", "N0TE_NATIVE", "audio.compare"),
            ],
        )
        plan_a = self.planner.plan(recipe, studio_a)
        plan_b = self.planner.plan(recipe, studio_b)
        self.assertEqual(plan_a.recipe_id, plan_b.recipe_id, recipe.recipe_id)
        tighten_a = next(item for item in plan_a.step_plans if item.step.step_id == "tighten")
        tighten_b = next(item for item in plan_b.step_plans if item.step.step_id == "tighten")
        self.assertEqual(tighten_a.resolution.recommended.candidate.route_kind, "HOST_NATIVE")
        self.assertEqual(tighten_b.resolution.recommended.candidate.route_kind, "OWNED_TOOL")

    def test_host_label_and_brand_do_not_change_recipe_plan(self):
        recipe = vocal_recipe()
        facts = [
            candidate("inspect", "GUIDED", "vocal.timing.inspect", brand="Brand A"),
            candidate("tighten", "GUIDED", "vocal.tighten", brand="Brand B"),
            candidate("compare", "GUIDED", "audio.compare", brand="Brand C"),
        ]
        left = StudioCapabilityProfile.build(
            environment_id="same", host_label="Ableton Live", candidates=facts
        )
        right = StudioCapabilityProfile.build(
            environment_id="same", host_label="Generic Other", candidates=facts
        )
        self.assertEqual(self.planner.plan(recipe, left), self.planner.plan(recipe, right))

    def test_unverified_installed_tool_cannot_satisfy_recipe_step(self):
        recipe = vocal_recipe()
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate("inspect", "GUIDED", "vocal.timing.inspect"),
                candidate(
                    "famous-tightener",
                    "OWNED_TOOL",
                    "vocal.tighten",
                    brand="Famous Vendor",
                    verified=False,
                    evidence_ref=None,
                    task_fit=1.0,
                    user_preference=1.0,
                ),
                candidate("compare", "GUIDED", "audio.compare"),
            ],
        )
        plan = self.planner.plan(recipe, studio)
        self.assertEqual(plan.status, "UNAVAILABLE")
        tighten = next(item for item in plan.step_plans if item.step.step_id == "tighten")
        self.assertIn("UNVERIFIED", tighten.resolution.reason_codes)

    def test_constraints_are_delegated_to_existing_resolver(self):
        recipe = RecipeDefinition(
            "recipe:publish:preview",
            "Preview Publish Route",
            "Inspect whether an outbound publishing route is legitimate without publishing",
            (
                RecipeStep(
                    "route",
                    "content.publish.prepare",
                    "Prepare a publishing route",
                    "CONSEQUENTIAL_ACTION",
                    ("destination and payload would be inspectable before execution",),
                    "MANUAL_RECOVERY",
                ),
            ),
        )
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate(
                    "cloud",
                    "PROVIDER",
                    "content.publish.prepare",
                    locality=0.1,
                    privacy=0.2,
                    paid=True,
                )
            ],
        )
        plan = self.planner.plan(
            recipe,
            studio,
            ResolutionConstraints(min_locality=0.9, min_privacy=0.9, allow_paid=False),
        )
        self.assertEqual(plan.status, "UNAVAILABLE")
        reasons = set(plan.step_plans[0].resolution.reason_codes)
        self.assertTrue(
            {"LOCALITY_BELOW_MINIMUM", "PRIVACY_BELOW_MINIMUM", "PAID_ROUTE_NOT_ALLOWED"}
            <= reasons
        )

    def test_authority_class_is_declaration_not_approval_or_execution(self):
        recipe = RecipeDefinition(
            "recipe:consequential",
            "Consequential Plan",
            "Declare future authority needs only",
            (
                RecipeStep(
                    "publish",
                    "content.publish.prepare",
                    "Prepare publishing action",
                    "CONSEQUENTIAL_ACTION",
                    ("exact destination and payload are inspectable",),
                    "MANUAL_RECOVERY",
                ),
            ),
        )
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[candidate("guided", "GUIDED", "content.publish.prepare")],
        )
        plan = self.planner.plan(recipe, studio)
        self.assertEqual(plan.status, "READY")
        self.assertEqual(plan.declared_authority_classes, ("CONSEQUENTIAL_ACTION",))
        self.assertFalse(hasattr(plan, "approved"))
        self.assertFalse(hasattr(plan, "authorization"))
        self.assertFalse(hasattr(plan, "executed"))
        self.assertFalse(hasattr(plan, "receipt"))

    def test_planning_is_pure_and_deterministic(self):
        recipe = vocal_recipe()
        studio = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate("inspect", "GUIDED", "vocal.timing.inspect"),
                candidate("tighten", "GUIDED", "vocal.tighten"),
                candidate("compare", "GUIDED", "audio.compare"),
            ],
        )
        before_recipe = recipe
        before_candidates = studio.candidates
        first = self.planner.plan(recipe, studio)
        second = self.planner.plan(recipe, studio)
        self.assertEqual(first, second)
        self.assertEqual(recipe, before_recipe)
        self.assertEqual(studio.candidates, before_candidates)


if __name__ == "__main__":
    unittest.main()
