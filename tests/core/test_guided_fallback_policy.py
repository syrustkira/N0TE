import unittest

from n0te.capabilities import (
    CapabilityCandidate,
    CapabilityResolver,
    N0TEableJob,
)


class GuidedFallbackPolicyTests(unittest.TestCase):
    def setUp(self):
        self.job = N0TEableJob(
            "job:track.write",
            "track.write",
            "Apply one bounded verified track change",
        )
        self.resolver = CapabilityResolver()

    def candidate(self, candidate_id, route_kind, **overrides):
        values = dict(
            candidate_id=candidate_id,
            route_kind=route_kind,
            capability=self.job.capability,
            display_name=candidate_id,
            brand=None,
            verified=True,
            compatible=True,
            evidence_ref=f"evidence:{candidate_id}",
            evidence_age_seconds=1,
            task_fit=0.50,
            editability=0.50,
            locality=0.50,
            privacy=0.50,
            latency=0.50,
            reversibility=1.0,
            cost_efficiency=0.50,
            portability=0.50,
            user_preference=0.50,
            paid=False,
        )
        values.update(overrides)
        return CapabilityCandidate(**values)

    def test_guided_cannot_outrank_legitimate_automation(self):
        automated = self.candidate(
            "automated",
            "HOST_NATIVE",
            task_fit=0.10,
            editability=0.10,
            locality=0.10,
            privacy=0.10,
            latency=0.10,
            cost_efficiency=0.10,
            portability=0.10,
            user_preference=0.0,
        )
        guided = self.candidate(
            "guided",
            "GUIDED",
            task_fit=1.0,
            editability=1.0,
            locality=1.0,
            privacy=1.0,
            latency=1.0,
            cost_efficiency=1.0,
            portability=1.0,
            user_preference=1.0,
        )

        resolution = self.resolver.resolve(self.job, [guided, automated])

        self.assertEqual(
            resolution.recommended.candidate.candidate_id,
            "automated",
        )
        self.assertEqual(resolution.fallbacks, ())
        guided_rejection = next(
            item for item in resolution.rejected if item.candidate_id == "guided"
        )
        self.assertEqual(
            guided_rejection.reason_codes,
            ("GUIDED_FALLBACK_ONLY_AUTOMATION_AVAILABLE",),
        )
        self.assertIn(
            "AUTOMATION_AVAILABLE_GUIDED_EXCLUDED",
            resolution.reason_codes,
        )

    def test_guided_resolves_when_no_legitimate_automation_exists(self):
        unavailable_automation = self.candidate(
            "automation",
            "N0TE_NATIVE",
            compatible=False,
            task_fit=1.0,
        )
        guided = self.candidate("guided", "GUIDED", task_fit=0.40)

        resolution = self.resolver.resolve(
            self.job,
            [unavailable_automation, guided],
        )

        self.assertEqual(
            resolution.recommended.candidate.candidate_id,
            "guided",
        )
        self.assertNotIn(
            "AUTOMATION_AVAILABLE_GUIDED_EXCLUDED",
            resolution.reason_codes,
        )
        automation_rejection = next(
            item for item in resolution.rejected if item.candidate_id == "automation"
        )
        self.assertIn("INCOMPATIBLE", automation_rejection.reason_codes)


if __name__ == "__main__":
    unittest.main()
