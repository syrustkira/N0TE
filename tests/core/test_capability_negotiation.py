import unittest
from dataclasses import fields

from n0te.capabilities import CapabilityResolver, N0TEableJob
from n0te.capability_evidence import CapabilityObservation
from n0te.capability_negotiation import (
    CapabilityNegotiationError,
    CapabilityNegotiationRequest,
    CapabilityNegotiationResult,
    CapabilityNegotiator,
    CapabilityRouteCharacterization,
    OperationDepth,
)
from n0te.entitlements import EntitlementSnapshot
from n0te.evidence_freshness import (
    FreshnessDependency,
    assess_capability_observation_freshness,
    assess_evidence_freshness,
)


class CapabilityNegotiationTests(unittest.TestCase):
    def setUp(self):
        self.job = N0TEableJob(
            id="job:vocal.tighten",
            capability="vocal.tighten",
            description="Tighten the chorus while preserving performance intent",
        )
        self.strong = OperationDepth("SET_PARAMETER", "NATIVE_REVERSIBLE")
        self.guided = OperationDepth("GUIDED_EDIT", "MANUAL")
        self.negotiator = CapabilityNegotiator()

    def observation(self, **overrides):
        values = dict(
            sequence=1,
            id="capobs_1",
            workspace_id="workspace_1",
            workspace_observation_id="wobs_1",
            host_runtime_fingerprint="env_1",
            route_id="route_1",
            route_kind="HOST_NATIVE",
            capability=self.job.capability,
            display_name="Current host route",
            brand="Host Brand",
            availability="AVAILABLE",
            evidence_kind="ADAPTER_TEST",
            evidence_ref="evidence:capability:1",
            observed_at_epoch_seconds=100,
            task_fit=0.9,
            editability=0.9,
            locality=1.0,
            privacy=1.0,
            latency=0.9,
            reversibility=1.0,
            cost_efficiency=0.8,
            portability=0.7,
            paid=False,
        )
        values.update(overrides)
        return CapabilityObservation(**values)

    def request(self, **overrides):
        values = dict(
            job=self.job,
            profile_id="profile_1",
            subject_id="version_1",
            workspace_observation_id="wobs_1",
            environment_fingerprint="env_1",
            acceptable_paths=(self.strong, self.guided),
        )
        values.update(overrides)
        return CapabilityNegotiationRequest(**values)

    def characterization(self, observation=None, **overrides):
        values = dict(
            observation=observation or self.observation(),
            supported_paths=(self.strong, self.guided),
            characterization_ref="evidence:characterization:1",
            access_kind=None,
            entitlement_required=False,
            permission_required=False,
        )
        values.update(overrides)
        return CapabilityRouteCharacterization(**values)

    def freshness(
        self,
        observation=None,
        *,
        current_workspace_observation_id="wobs_1",
        current_host_runtime_fingerprint="env_1",
        dependencies=(),
        source_current=True,
        expires_at_epoch_seconds=None,
        max_age_seconds=None,
    ):
        observation = observation or self.observation()
        return assess_capability_observation_freshness(
            observation,
            as_of_epoch_seconds=120,
            current_workspace_observation_id=current_workspace_observation_id,
            current_host_runtime_fingerprint=current_host_runtime_fingerprint,
            dependencies=dependencies,
            source_current=source_current,
            expires_at_epoch_seconds=expires_at_epoch_seconds,
            max_age_seconds=max_age_seconds,
        )

    def access(self, **overrides):
        values = dict(
            profile_id="profile_1",
            route_id="route_1",
            capability=self.job.capability,
            access_kind="PLUGIN",
            as_of_epoch_seconds=120,
            resolution_status="RESOLVED",
            validity_state="CURRENT",
            entitlement_state="GRANTED",
            permission_state="GRANTED",
            eligibility_entitlement_state="GRANTED",
            eligibility_permission_state="GRANTED",
            quota_status="UNKNOWN",
            quota_remaining=None,
            quota_unit=None,
            strongest_source_class="OBSERVED",
            provider_verified=False,
            strong_access_evidence=True,
            facts=(),
            active_fact_ids=(),
            fingerprint="access:fingerprint:1",
        )
        values.update(overrides)
        return EntitlementSnapshot(**values)

    def test_strongest_common_path_is_rankable_but_never_authorized(self):
        observation = self.observation()
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(observation),
            freshness=self.freshness(observation),
        )

        self.assertEqual(result.status, "NEGOTIABLE")
        self.assertEqual(result.strongest_common_path, self.strong)
        self.assertEqual(result.strongest_common_path_index, 0)
        self.assertTrue(result.rankable)
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.route_kind, "HOST_NATIVE")
        self.assertIn(
            "RANKING_CANDIDATE_EXPOSED_AFTER_NEGOTIATION",
            result.reason_codes,
        )
        self.assertFalse(result.grants_any_authority)
        self.assertFalse(result.action_authority_granted)
        self.assertFalse(result.execution_authority_granted)
        self.assertFalse(result.mutation_authority_granted)
        self.assertFalse(result.external_action_authority_granted)
        self.assertFalse(result.purchase_authority_granted)
        self.assertFalse(result.activation_authority_granted)
        self.assertFalse(result.provider_write_authority_granted)

        constructor = {
            item.name: getattr(result, item.name)
            for item in fields(result)
            if item.init
        }
        constructor["action_authority_granted"] = True
        with self.assertRaises(TypeError):
            CapabilityNegotiationResult(**constructor)

    def test_weaker_common_path_is_explicitly_degraded(self):
        observation = self.observation()
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(
                observation,
                supported_paths=(self.guided,),
            ),
            freshness=self.freshness(observation),
        )
        self.assertEqual(result.status, "DEGRADED")
        self.assertEqual(result.strongest_common_path, self.guided)
        self.assertEqual(result.strongest_common_path_index, 1)
        self.assertIsNotNone(result.candidate)
        self.assertIn("STRONGEST_COMMON_PATH_IS_DEGRADED", result.reason_codes)

    def test_no_common_operation_depth_is_unavailable_not_a_brand_badge(self):
        observation = self.observation(brand="Famous Brand")
        unsupported = OperationDepth("RENDER_STEMS", "HOST_NATIVE")
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(observation, supported_paths=(unsupported,)),
            freshness=self.freshness(observation),
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertIsNone(result.strongest_common_path)
        self.assertIsNone(result.candidate)
        self.assertIn("NO_COMMON_OPERATION_DEPTH", result.reason_codes)

    def test_unknown_capability_availability_stays_unknown(self):
        observation = self.observation(
            availability="UNKNOWN",
            evidence_ref=None,
        )
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(observation),
            freshness=self.freshness(observation),
        )
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.availability, "UNKNOWN")
        self.assertIsNone(result.candidate)
        self.assertIn("CAPABILITY_AVAILABILITY_UNKNOWN", result.reason_codes)

    def test_explicit_unavailability_wins_over_possible_path(self):
        observation = self.observation(availability="UNAVAILABLE")
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(observation),
            freshness=self.freshness(observation),
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertEqual(result.strongest_common_path, self.strong)
        self.assertIsNone(result.candidate)
        self.assertIn("CAPABILITY_UNAVAILABLE", result.reason_codes)

    def test_changed_environment_becomes_revalidation_required(self):
        old_observation = self.observation(
            workspace_observation_id="wobs_old",
            host_runtime_fingerprint="env_old",
        )
        freshness = self.freshness(
            old_observation,
            current_workspace_observation_id="wobs_new",
            current_host_runtime_fingerprint="env_new",
        )
        result = self.negotiator.negotiate(
            self.request(
                workspace_observation_id="wobs_new",
                environment_fingerprint="env_new",
            ),
            self.characterization(old_observation),
            freshness=freshness,
        )
        self.assertEqual(freshness.state, "REVALIDATION_REQUIRED")
        self.assertEqual(result.status, "REVALIDATION_REQUIRED")
        self.assertIsNone(result.candidate)
        self.assertTrue(
            any(code.startswith("FRESHNESS:DEPENDENCY_CHANGED") for code in result.reason_codes)
        )

    def test_unknown_dependency_stays_unknown(self):
        observation = self.observation()
        freshness = self.freshness(
            observation,
            dependencies=(
                FreshnessDependency(
                    kind="PLUGIN",
                    key="plugin-binary",
                    observed_fingerprint="plugin-old",
                    current_fingerprint=None,
                ),
            ),
        )
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(observation),
            freshness=freshness,
        )
        self.assertEqual(freshness.state, "UNKNOWN")
        self.assertEqual(result.status, "UNKNOWN")
        self.assertIsNone(result.candidate)

    def test_missing_required_access_evidence_is_unknown(self):
        observation = self.observation()
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(
                observation,
                access_kind="PLUGIN",
                entitlement_required=True,
            ),
            freshness=self.freshness(observation),
        )
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.access_resolution_status, "UNKNOWN")
        self.assertEqual(result.entitlement_state, "UNKNOWN")
        self.assertEqual(result.eligibility_entitlement_state, "UNKNOWN")
        self.assertIn("ACCESS_EVIDENCE_MISSING", result.reason_codes)

    def test_descriptive_grant_without_eligible_access_stays_unknown(self):
        observation = self.observation()
        declared_only = self.access(
            entitlement_state="GRANTED",
            eligibility_entitlement_state="UNKNOWN",
            strongest_source_class="DECLARED",
            strong_access_evidence=False,
        )
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(
                observation,
                access_kind="PLUGIN",
                entitlement_required=True,
            ),
            freshness=self.freshness(observation),
            access=declared_only,
        )
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.entitlement_state, "GRANTED")
        self.assertEqual(result.eligibility_entitlement_state, "UNKNOWN")
        self.assertIsNone(result.candidate)
        self.assertIn("ENTITLEMENT_ELIGIBILITY_UNKNOWN", result.reason_codes)

    def test_access_conflict_is_preserved(self):
        observation = self.observation()
        access = self.access(
            resolution_status="CONFLICT",
            entitlement_state="UNKNOWN",
            eligibility_entitlement_state="UNKNOWN",
        )
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(
                observation,
                access_kind="PLUGIN",
                entitlement_required=True,
            ),
            freshness=self.freshness(observation),
            access=access,
        )
        self.assertEqual(result.status, "CONFLICT")
        self.assertEqual(result.access_resolution_status, "CONFLICT")
        self.assertIn("ACCESS_CONFLICT", result.reason_codes)
        self.assertIn("ENTITLEMENT_ELIGIBILITY_UNKNOWN", result.reason_codes)

    def test_denied_required_entitlement_is_unavailable(self):
        observation = self.observation()
        access = self.access(
            entitlement_state="DENIED",
            eligibility_entitlement_state="DENIED",
        )
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(
                observation,
                access_kind="PLUGIN",
                entitlement_required=True,
            ),
            freshness=self.freshness(observation),
            access=access,
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertIn("ENTITLEMENT_DENIED", result.reason_codes)
        self.assertIsNone(result.candidate)

    def test_expired_access_requires_revalidation(self):
        observation = self.observation()
        access = self.access(
            validity_state="EXPIRED",
            eligibility_entitlement_state="UNKNOWN",
            eligibility_permission_state="UNKNOWN",
        )
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(
                observation,
                access_kind="PLUGIN",
                entitlement_required=True,
            ),
            freshness=self.freshness(observation),
            access=access,
        )
        self.assertEqual(result.status, "REVALIDATION_REQUIRED")
        self.assertIn("ACCESS_EXPIRED", result.reason_codes)
        self.assertIsNone(result.candidate)

    def test_permission_only_route_does_not_invent_entitlement_requirement(self):
        observation = self.observation()
        access = self.access(
            access_kind="PERMISSION",
            entitlement_state="NOT_REQUIRED",
            eligibility_entitlement_state="NOT_REQUIRED",
            permission_state="GRANTED",
            eligibility_permission_state="GRANTED",
        )
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(
                observation,
                access_kind="PERMISSION",
                permission_required=True,
            ),
            freshness=self.freshness(observation),
            access=access,
        )
        self.assertEqual(result.status, "NEGOTIABLE")
        self.assertEqual(result.entitlement_state, "NOT_REQUIRED")
        self.assertEqual(result.permission_state, "GRANTED")
        self.assertEqual(result.eligibility_permission_state, "GRANTED")
        self.assertIn("PERMISSION_ELIGIBLE_GRANTED", result.reason_codes)

    def test_mismatched_access_route_fails_closed_as_invalid_binding(self):
        observation = self.observation()
        with self.assertRaises(CapabilityNegotiationError):
            self.negotiator.negotiate(
                self.request(),
                self.characterization(
                    observation,
                    access_kind="PLUGIN",
                    entitlement_required=True,
                ),
                freshness=self.freshness(observation),
                access=self.access(route_id="different-route"),
            )

    def test_freshness_must_be_bound_to_workspace_and_runtime(self):
        observation = self.observation()
        generic = assess_evidence_freshness(
            observed_at_epoch_seconds=100,
            as_of_epoch_seconds=120,
        )
        with self.assertRaises(CapabilityNegotiationError):
            self.negotiator.negotiate(
                self.request(),
                self.characterization(observation),
                freshness=generic,
            )

    def test_duplicate_and_untyped_operation_depth_inputs_fail_closed(self):
        with self.assertRaises(CapabilityNegotiationError):
            self.request(acceptable_paths=(self.strong, self.strong))
        with self.assertRaises(CapabilityNegotiationError):
            self.request(acceptable_paths=("SET_PARAMETER",))
        with self.assertRaises(CapabilityNegotiationError):
            self.characterization(
                entitlement_required=1,
                access_kind="PLUGIN",
            )

    def test_rankable_candidate_remains_owned_by_existing_resolver(self):
        observation = self.observation(route_kind="PROVIDER")
        result = self.negotiator.negotiate(
            self.request(),
            self.characterization(observation),
            freshness=self.freshness(observation),
        )
        resolution = CapabilityResolver().resolve(self.job, [result.candidate])
        self.assertEqual(resolution.status, "RESOLVED")
        self.assertEqual(
            resolution.recommended.candidate.candidate_id,
            observation.candidate_id,
        )
        self.assertEqual(resolution.recommended.candidate.route_kind, "PROVIDER")


if __name__ == "__main__":
    unittest.main()
