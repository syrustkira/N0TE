import unittest

from n0te.capabilities import (
    SUPPORTED_TEMPLATE_CAPABILITY_KEYS,
    CapabilityCandidate,
    CapabilityResolutionError,
    CapabilityResolver,
    N0TEableJob,
    ResolutionConstraints,
    canonical_template_capability_key,
)


class Core03AResolverTests(unittest.TestCase):
    def setUp(self):
        self.job = N0TEableJob(
            "job:vocal.tighten",
            "vocal.tighten",
            "Tighten chorus vocals without changing performance intent",
        )
        self.resolver = CapabilityResolver()

    def candidate(self, candidate_id, route_kind, **overrides):
        values = dict(
            candidate_id=candidate_id,
            route_kind=route_kind,
            capability=self.job.capability,
            display_name=f"Display {candidate_id}",
            brand=f"Brand {candidate_id}",
            verified=True,
            compatible=True,
            evidence_ref=f"evidence:{candidate_id}",
            evidence_age_seconds=60,
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

    def test_same_job_identity_resolves_different_routes_by_environment(self):
        env_a = self.resolver.resolve(
            self.job,
            [
                self.candidate(
                    "native",
                    "HOST_NATIVE",
                    task_fit=0.99,
                    editability=0.95,
                    locality=1.0,
                    privacy=1.0,
                    latency=0.98,
                ),
                self.candidate(
                    "owned",
                    "OWNED_TOOL",
                    task_fit=0.88,
                    locality=1.0,
                    privacy=1.0,
                ),
            ],
        )
        env_b = self.resolver.resolve(
            self.job,
            [
                self.candidate(
                    "native",
                    "HOST_NATIVE",
                    compatible=False,
                    task_fit=1.0,
                ),
                self.candidate(
                    "n0te",
                    "N0TE_NATIVE",
                    task_fit=0.94,
                    editability=0.92,
                    locality=1.0,
                    privacy=1.0,
                ),
                self.candidate(
                    "guided",
                    "GUIDED",
                    task_fit=0.70,
                    editability=0.60,
                    locality=1.0,
                    privacy=1.0,
                    latency=0.50,
                ),
            ],
        )
        self.assertEqual(env_a.job_id, env_b.job_id)
        self.assertEqual(env_a.capability, env_b.capability)
        self.assertEqual(env_a.recommended.candidate.route_kind, "HOST_NATIVE")
        self.assertEqual(env_b.recommended.candidate.route_kind, "N0TE_NATIVE")
        self.assertIn("INCOMPATIBLE", env_b.rejected[0].reason_codes)

    def test_route_kind_has_no_bonus(self):
        provider = self.candidate("a", "PROVIDER")
        native = self.candidate("b", "HOST_NATIVE")
        resolution = self.resolver.resolve(self.job, [native, provider])
        self.assertAlmostEqual(
            resolution.recommended.score,
            resolution.fallbacks[0].score,
        )
        self.assertEqual(resolution.candidate_ids, ("a", "b"))
        self.assertIn(
            "SCORE_TIE_BROKEN_BY_CANDIDATE_ID",
            resolution.reason_codes,
        )

    def test_brand_and_display_name_do_not_change_score(self):
        first = self.candidate(
            "stable-a",
            "OWNED_TOOL",
            brand="Brand A",
            display_name="Tool A",
        )
        second = self.candidate(
            "stable-b",
            "OWNED_TOOL",
            brand="Different Brand",
            display_name="Different Name",
        )
        resolution = self.resolver.resolve(self.job, [first, second])
        self.assertAlmostEqual(
            resolution.recommended.score,
            resolution.fallbacks[0].score,
        )

    def test_unverified_installed_tool_is_excluded_even_with_perfect_preference(self):
        installed = self.candidate(
            "installed",
            "OWNED_TOOL",
            verified=False,
            evidence_ref=None,
            task_fit=1.0,
            editability=1.0,
            locality=1.0,
            privacy=1.0,
            latency=1.0,
            reversibility=1.0,
            cost_efficiency=1.0,
            portability=1.0,
            user_preference=1.0,
        )
        guided = self.candidate(
            "guided",
            "GUIDED",
            task_fit=0.50,
            user_preference=0.0,
        )
        resolution = self.resolver.resolve(self.job, [installed, guided])
        self.assertEqual(
            resolution.recommended.candidate.candidate_id,
            "guided",
        )
        self.assertIn("UNVERIFIED", resolution.rejected[0].reason_codes)

    def test_constraints_filter_before_preference(self):
        paid = self.candidate(
            "paid",
            "PROVIDER",
            paid=True,
            user_preference=1.0,
        )
        cloud = self.candidate(
            "cloud",
            "PROVIDER",
            locality=0.10,
            privacy=0.20,
            user_preference=1.0,
        )
        irreversible = self.candidate(
            "irreversible",
            "N0TE_NATIVE",
            reversibility=0.90,
            user_preference=1.0,
        )
        local = self.candidate(
            "local",
            "GUIDED",
            locality=1.0,
            privacy=1.0,
            reversibility=1.0,
            user_preference=0.0,
        )
        resolution = self.resolver.resolve(
            self.job,
            [paid, cloud, irreversible, local],
            ResolutionConstraints(
                min_locality=0.80,
                min_privacy=0.80,
                require_reversible=True,
                allow_paid=False,
            ),
        )
        self.assertEqual(
            resolution.recommended.candidate.candidate_id,
            "local",
        )
        reasons = {
            item.candidate_id: set(item.reason_codes)
            for item in resolution.rejected
        }
        self.assertIn("PAID_ROUTE_NOT_ALLOWED", reasons["paid"])
        self.assertIn("LOCALITY_BELOW_MINIMUM", reasons["cloud"])
        self.assertIn("PRIVACY_BELOW_MINIMUM", reasons["cloud"])
        self.assertIn("REVERSIBILITY_REQUIRED", reasons["irreversible"])

    def test_stale_or_unknown_evidence_is_filtered_when_freshness_required(self):
        stale = self.candidate(
            "stale",
            "HOST_NATIVE",
            evidence_age_seconds=999,
        )
        unknown = self.candidate(
            "unknown",
            "N0TE_NATIVE",
            evidence_age_seconds=None,
        )
        fresh = self.candidate(
            "fresh",
            "GUIDED",
            evidence_age_seconds=10,
        )
        resolution = self.resolver.resolve(
            self.job,
            [stale, unknown, fresh],
            ResolutionConstraints(max_evidence_age_seconds=30),
        )
        self.assertEqual(
            resolution.recommended.candidate.candidate_id,
            "fresh",
        )
        for rejected in resolution.rejected:
            self.assertIn(
                "STALE_OR_UNKNOWN_EVIDENCE",
                rejected.reason_codes,
            )

    def test_unavailable_is_truthful_and_explains_gaps(self):
        resolution = self.resolver.resolve(
            self.job,
            [
                self.candidate(
                    "wrong",
                    "HOST_NATIVE",
                    capability="different",
                ),
                self.candidate(
                    "unverified",
                    "OWNED_TOOL",
                    verified=False,
                    evidence_ref=None,
                ),
            ],
        )
        self.assertEqual(resolution.status, "UNAVAILABLE")
        self.assertIsNone(resolution.recommended)
        self.assertIn("NO_LEGITIMATE_ROUTE", resolution.reason_codes)
        self.assertIn("CAPABILITY_MISMATCH", resolution.reason_codes)
        self.assertIn("UNVERIFIED", resolution.reason_codes)

    def test_empty_candidate_set_is_unavailable_not_exception(self):
        resolution = self.resolver.resolve(self.job, [])
        self.assertEqual(resolution.status, "UNAVAILABLE")
        self.assertEqual(
            resolution.reason_codes,
            ("NO_LEGITIMATE_ROUTE", "NO_CANDIDATES"),
        )

    def test_score_breakdown_is_explicit_and_sums_to_score(self):
        resolution = self.resolver.resolve(
            self.job,
            [self.candidate("only", "GUIDED")],
        )
        breakdown = resolution.recommended.score_breakdown
        self.assertEqual(len(breakdown), 9)
        self.assertAlmostEqual(
            sum(item.contribution for item in breakdown),
            resolution.recommended.score,
        )
        self.assertTrue(
            all(
                item.attribute not in {"route_kind", "brand", "display_name"}
                for item in breakdown
            )
        )

    def test_repeated_resolution_is_pure_and_deterministic(self):
        candidates = [
            self.candidate("b", "HOST_NATIVE"),
            self.candidate("a", "GUIDED"),
        ]
        first = self.resolver.resolve(self.job, candidates)
        second = self.resolver.resolve(self.job, candidates)
        self.assertEqual(first, second)
        self.assertEqual(candidates[0].display_name, "Display b")

    def test_duplicate_candidate_ids_are_rejected_as_ambiguous(self):
        with self.assertRaises(CapabilityResolutionError):
            self.resolver.resolve(
                self.job,
                [
                    self.candidate("same", "HOST_NATIVE"),
                    self.candidate("same", "GUIDED"),
                ],
            )

    def test_template_capability_vocabulary_normalizes_case_and_rejects_unknowns(self):
        self.assertIn("vocal.tighten", SUPPORTED_TEMPLATE_CAPABILITY_KEYS)
        self.assertIn("content.generate", SUPPORTED_TEMPLATE_CAPABILITY_KEYS)
        self.assertEqual(
            canonical_template_capability_key("  VOCAL.TIGHTEN  "),
            "vocal.tighten",
        )
        self.assertEqual(
            canonical_template_capability_key("Content.Generate"),
            "content.generate",
        )
        for invalid in ("vocal.tigten", "Vocal Tighten", "unknown.capability", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CapabilityResolutionError):
                    canonical_template_capability_key(invalid)
        with self.assertRaises(CapabilityResolutionError):
            canonical_template_capability_key(123)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
