import unittest

from n0te import (
    CapabilityCandidate,
    CapabilityResolutionError,
    N0TEableJob,
    ResolutionConstraints,
    StudioCapabilityProfile,
)


def candidate(candidate_id, route_kind, capability="vocal.tighten", **overrides):
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


class Core03BStudioCapabilityProfileTests(unittest.TestCase):
    def setUp(self):
        self.job = N0TEableJob(
            id="job:vocal.tighten",
            capability="vocal.tighten",
            description="Tighten a vocal while preserving performance intent",
        )

    def test_profile_normalizes_input_order_without_changing_resolution(self):
        native = candidate("native", "HOST_NATIVE", task_fit=0.92)
        n0te = candidate("n0te", "N0TE_NATIVE", task_fit=0.88)
        a = StudioCapabilityProfile.build(
            environment_id="studio:a", candidates=[native, n0te]
        )
        b = StudioCapabilityProfile.build(
            environment_id="studio:b", candidates=[n0te, native]
        )
        # Profile storage order is deliberately neutral and deterministic by candidate_id;
        # route kind/brand/host history do not establish semantic priority.
        self.assertEqual(tuple(item.candidate_id for item in a.candidates), ("n0te", "native"))
        self.assertEqual(tuple(item.candidate_id for item in b.candidates), ("n0te", "native"))
        self.assertEqual(a.resolve(self.job).recommended.candidate.candidate_id, "native")
        self.assertEqual(b.resolve(self.job).recommended.candidate.candidate_id, "native")

    def test_host_label_is_descriptive_only(self):
        facts = [
            candidate("a", "HOST_NATIVE", task_fit=0.80),
            candidate("b", "N0TE_NATIVE", task_fit=0.90),
        ]
        ableton_named = StudioCapabilityProfile.build(
            environment_id="studio:x", host_label="Ableton Live", candidates=facts
        )
        generic_named = StudioCapabilityProfile.build(
            environment_id="studio:x", host_label="Generic Other", candidates=facts
        )
        self.assertEqual(ableton_named.resolve(self.job), generic_named.resolve(self.job))
        self.assertEqual(
            ableton_named.resolve(self.job).recommended.candidate.candidate_id, "b"
        )

    def test_brand_and_display_name_do_not_change_score(self):
        left = candidate(
            "a-route",
            "PROVIDER",
            brand="Famous Brand",
            display_name="Premium Magic",
        )
        right = candidate(
            "b-route",
            "HOST_NATIVE",
            brand="Unknown Brand",
            display_name="Plain Native",
        )
        profile = StudioCapabilityProfile.build(
            environment_id="studio", candidates=[right, left]
        )
        resolution = profile.resolve(self.job)
        self.assertEqual(resolution.recommended.score, resolution.fallbacks[0].score)
        self.assertEqual(resolution.candidate_ids, ("a-route", "b-route"))
        self.assertIn("SCORE_TIE_BROKEN_BY_CANDIDATE_ID", resolution.reason_codes)

    def test_unverified_installed_name_is_visible_but_not_legitimate(self):
        installed = candidate(
            "installed-plugin",
            "OWNED_TOOL",
            display_name="Installed Plugin",
            brand="Known Vendor",
            verified=False,
            evidence_ref=None,
            task_fit=1.0,
            user_preference=1.0,
        )
        profile = StudioCapabilityProfile.build(
            environment_id="studio", candidates=[installed]
        )
        summary = profile.route_summary()[0]
        self.assertEqual(summary.verified_candidate_ids, ())
        self.assertEqual(summary.unverified_candidate_ids, ("installed-plugin",))
        resolution = profile.resolve(self.job)
        self.assertEqual(resolution.status, "UNAVAILABLE")
        self.assertIn("UNVERIFIED", resolution.reason_codes)

    def test_route_summary_reports_facts_without_ranking_routes(self):
        profile = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate("native", "HOST_NATIVE"),
                candidate("guided", "GUIDED", capability="arrangement.structure"),
                candidate(
                    "tool-unverified",
                    "OWNED_TOOL",
                    verified=False,
                    evidence_ref=None,
                ),
            ],
        )
        summaries = {item.route_kind: item for item in profile.route_summary()}
        self.assertEqual(set(summaries), {"GUIDED", "HOST_NATIVE", "OWNED_TOOL"})
        self.assertEqual(summaries["HOST_NATIVE"].verified_candidate_ids, ("native",))
        self.assertEqual(
            summaries["OWNED_TOOL"].unverified_candidate_ids,
            ("tool-unverified",),
        )
        self.assertEqual(
            summaries["GUIDED"].capabilities,
            ("arrangement.structure",),
        )

    def test_same_job_can_resolve_differently_across_studios(self):
        studio_a = StudioCapabilityProfile.build(
            environment_id="studio:a",
            host_label="Host A",
            candidates=[
                candidate("a-native", "HOST_NATIVE", task_fit=0.98, locality=1.0),
                candidate("a-guided", "GUIDED", task_fit=0.60, locality=1.0),
            ],
        )
        studio_b = StudioCapabilityProfile.build(
            environment_id="studio:b",
            host_label="Host B",
            candidates=[
                candidate("b-native", "HOST_NATIVE", compatible=False, task_fit=1.0),
                candidate("b-n0te", "N0TE_NATIVE", task_fit=0.94, locality=1.0),
            ],
        )
        self.assertEqual(
            studio_a.resolve(self.job).recommended.candidate.route_kind,
            "HOST_NATIVE",
        )
        self.assertEqual(
            studio_b.resolve(self.job).recommended.candidate.route_kind,
            "N0TE_NATIVE",
        )

    def test_gaps_are_truthful_resolver_results_not_marketing_copy(self):
        job_missing = N0TEableJob(
            id="job:missing",
            capability="pitch.correct",
            description="Correct pitch",
        )
        profile = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate(
                    "cloud-pitch",
                    "PROVIDER",
                    capability="pitch.correct",
                    locality=0.1,
                    privacy=0.2,
                    paid=True,
                )
            ],
        )
        gaps = profile.gaps(
            [job_missing],
            ResolutionConstraints(min_locality=0.9, min_privacy=0.9, allow_paid=False),
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].job_id, "job:missing")
        self.assertEqual(gaps[0].rejected_candidate_ids, ("cloud-pitch",))
        self.assertIn("LOCALITY_BELOW_MINIMUM", gaps[0].reason_codes)
        self.assertIn("PRIVACY_BELOW_MINIMUM", gaps[0].reason_codes)
        self.assertIn("PAID_ROUTE_NOT_ALLOWED", gaps[0].reason_codes)

    def test_resolve_many_is_stable_by_job_id(self):
        other = N0TEableJob(
            id="job:arrange",
            capability="arrangement.structure",
            description="Shape arrangement",
        )
        profile = StudioCapabilityProfile.build(
            environment_id="studio",
            candidates=[
                candidate("vocal", "GUIDED"),
                candidate("arrange", "GUIDED", capability="arrangement.structure"),
            ],
        )
        resolved = profile.resolve_many([self.job, other])
        self.assertEqual(tuple(item.job_id for item in resolved), ("job:arrange", "job:vocal.tighten"))

    def test_duplicate_candidate_ids_are_rejected(self):
        with self.assertRaises(CapabilityResolutionError):
            StudioCapabilityProfile.build(
                environment_id="studio",
                candidates=[
                    candidate("dup", "GUIDED"),
                    candidate("dup", "HOST_NATIVE"),
                ],
            )

    def test_duplicate_job_ids_are_rejected(self):
        profile = StudioCapabilityProfile.build(environment_id="studio", candidates=[])
        duplicate = N0TEableJob(
            id=self.job.id,
            capability="different.capability",
            description="Different job sharing an invalid duplicate ID",
        )
        with self.assertRaises(CapabilityResolutionError):
            profile.resolve_many([self.job, duplicate])

    def test_profile_and_resolution_are_pure(self):
        facts = [candidate("native", "HOST_NATIVE")]
        profile = StudioCapabilityProfile.build(environment_id="studio", candidates=facts)
        before = profile.candidates
        first = profile.resolve(self.job)
        second = profile.resolve(self.job)
        self.assertEqual(first, second)
        self.assertEqual(profile.candidates, before)
        self.assertEqual(facts[0], profile.candidates[0])


if __name__ == "__main__":
    unittest.main()
