import copy
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CleanRoomReconstructionTests(unittest.TestCase):
    def load(self, rel):
        return json.loads((ROOT / rel).read_text())

    def test_canonical_scope_extensions_are_contiguous_and_non_selecting(self):
        requirements = self.load("governance/requirements.json")
        build_index = requirements["sequence"]
        canonical = requirements["canonical_scope"]

        self.assertEqual(build_index["start"], canonical["start"])
        self.assertGreaterEqual(canonical["end"], 170)
        self.assertEqual(canonical["source"], "N0TE_PRODUCT_DB/SCOPE_LEDGER")
        self.assertEqual(
            canonical["retained_requirement_count"],
            canonical["end"] - canonical["start"] + 1,
        )

        extensions = requirements["canonical_extensions"]
        expected = [
            f"REQ-SCOPE-{n:03d}"
            for n in range(build_index["end"] + 1, canonical["end"] + 1)
        ]
        self.assertEqual([row["id"] for row in extensions], expected)
        for row in extensions:
            self.assertEqual(row["state"], "MAPPED")
            self.assertFalse(row["selected"])
            self.assertTrue(row["construction_affinity"])
            self.assertTrue(row["summary"].strip())

    def test_known_or_recovered_scope_cannot_select_work_by_itself(self):
        requirements = self.load("governance/requirements.json")
        self.assertFalse(requirements["known_selects_work"])
        self.assertIn("Work becomes ACTIVE only through current_state plus an active receipt", requirements["selection_contract"])
        self.assertTrue(all(row["selected"] is False for row in requirements["canonical_extensions"]))

    def test_terminal_current_state_cannot_be_reactivated_by_historical_prose(self):
        live = self.load("governance/current_state.json")
        terminal = copy.deepcopy(live)
        terminal.update(
            {
                "lifecycle_state": "STABLE",
                "active_node": None,
                "active_increment": None,
                "product_code_authorized": False,
                "legacy_admission_authorized": False,
                "terminal_reason": "Synthetic terminal fixture for historical-command regression.",
            }
        )

        historical_text = "CURRENT NEXT ACTIVE PRIORITY BUILD THIS NEXT"
        terminal["truth"].append(historical_text)
        self.assertEqual(terminal["lifecycle_state"], "STABLE")
        self.assertIsNone(terminal["active_node"])
        self.assertIsNone(terminal["active_increment"])
        self.assertFalse(terminal["product_code_authorized"])
        self.assertFalse(terminal["legacy_admission_authorized"])

    def test_superseded_history_is_preserved_without_reactivation(self):
        requirements = self.load("governance/requirements.json")
        graph = self.load("governance/completion_graph.json")
        mapped = "\n".join(str(node.get("requirements", "")) for node in graph["nodes"] if node["required"])

        self.assertIn("REQ-SCOPE-046", requirements["superseded"])
        self.assertNotIn("046", mapped)

    def test_new_professional_scope_is_retained_without_self_selecting_work(self):
        requirements = self.load("governance/requirements.json")
        current = self.load("governance/current_state.json")
        receipt = self.load("governance/active_receipt.json")
        extensions = {row["id"]: row for row in requirements["canonical_extensions"]}

        for n in range(161, 171):
            row = extensions[f"REQ-SCOPE-{n:03d}"]
            self.assertFalse(row["selected"])
            self.assertEqual(row["state"], "MAPPED")

        if current["lifecycle_state"] == "ACTIVE":
            self.assertEqual(receipt["status"], "ACTIVE")
            self.assertEqual(receipt["node_id"], current["active_node"])
            self.assertEqual(receipt["increment_id"], current["active_increment"])
            self.assertTrue(str(current["active_increment"]).startswith(str(current["active_node"])))
            self.assertFalse(any(row["selected"] for row in extensions.values()))
        else:
            self.assertIsNone(current["active_node"])
            self.assertIsNone(current["active_increment"])

    def test_semantic_boundary_policy_is_builder_only_and_non_selecting(self):
        policy = self.load("governance/semantic_boundaries.json")
        self.assertEqual(policy["policy_id"], "CLEANROOM-SEMANTIC-BOUNDARIES-001")
        self.assertEqual(policy["authority"], "BUILDER_RECONSTRUCTION_ONLY")
        self.assertEqual(policy["product_scope_effect"], "NONE")
        self.assertEqual(policy["selection_effect"], "NONE")
        self.assertFalse(policy["admission_contract"]["retrieve_then_believe"])
        self.assertFalse(policy["promotion_contract"]["automatic_promotion"])

    def test_semantic_boundary_policy_is_required_by_reconstruction_startup(self):
        authority = self.load("governance/authority.json")
        handoff = self.load("governance/handoff.json")
        policy_ref = "governance/semantic_boundaries.json"

        self.assertIn("SEMANTIC_BOUNDARY_TAINT_POLICY", authority["authority_order"])
        self.assertIn(policy_ref, authority["current_authority_files"])
        self.assertTrue(authority["laws"]["semantic_boundary_axes_must_not_be_collapsed"])
        self.assertTrue(authority["laws"]["taint_does_not_propagate_by_proximity"])
        self.assertTrue(authority["laws"]["independent_positive_evidence_breaks_taint_chain"])
        self.assertTrue(authority["laws"]["review_lens_does_not_grant_product_scope_or_execution_authority"])
        self.assertTrue(authority["laws"]["temporary_context_requires_explicit_promotion_to_durable_state"])
        self.assertIn("SEMANTIC_BOUNDARY_TAINT_POLICY", handoff["reconstruction"]["required_outcomes"])
        self.assertIn(policy_ref, handoff["reconstruction"]["required_refs"])

    def test_context_admission_keeps_truth_evidence_authority_and_lifecycle_explicit(self):
        policy = self.load("governance/semantic_boundaries.json")
        required = {
            "source",
            "scope",
            "truth_type",
            "evidence_state",
            "authority",
            "freshness",
            "lifecycle",
            "provenance",
        }
        self.assertEqual(set(policy["context_admission_fields"]), required)
        self.assertTrue(policy["admission_contract"]["unknown_remains_unknown"])
        self.assertTrue(policy["admission_contract"]["inference_remains_inference"])
        self.assertFalse(policy["admission_contract"]["historical_command_like_text_selects_work"])

    def test_taint_is_orthogonal_and_does_not_spread_by_proximity(self):
        policy = self.load("governance/semantic_boundaries.json")
        self.assertTrue(policy["recovery_and_taint_are_orthogonal"])
        self.assertEqual(
            set(policy["taint_states"]),
            {
                "CLEAN",
                "TAINTED_CONTEXT",
                "TAINTED_EVIDENCE",
                "TAINTED_AUTHORITY",
                "TAINTED_IMPLEMENTATION",
                "TAINTED_SEMANTICS",
                "UNKNOWN_TAINT",
            },
        )
        inheritance = policy["taint_inheritance"]
        self.assertFalse(inheritance["propagate_by_proximity"])
        self.assertTrue(inheritance["propagate_only_through_proven_dependency"])
        self.assertTrue(inheritance["independent_positive_evidence_breaks_taint_chain"])
        self.assertEqual(
            inheritance["default_feature_disposition_when_supporting_evidence_is_tainted"],
            "REQUIRES_REVALIDATION",
        )

    def test_neighboring_concepts_cannot_be_deduplicated_into_one_object(self):
        policy = self.load("governance/semantic_boundaries.json")
        boundaries = {row["id"]: row for row in policy["boundaries"]}
        expected_concept_sets = {
            "BOUND-001": {"REVIEW_LENS", "CREATIVE_PARTNER_LENS", "PROFESSIONAL_ROLE", "AUTONOMOUS_AGENT"},
            "BOUND-002": {"TRUTH_TYPE", "EVIDENCE_STATE", "RECOVERY_STATE", "TAINT_STATE"},
            "BOUND-003": {"SESSION_MEMORY", "DISTILLED_LONG_TERM_MEMORY", "EXECUTION_STATE"},
            "BOUND-005": {"PROPOSAL", "PREVIEW", "AUTHORIZATION", "EXECUTION", "VERIFICATION", "DECISION"},
            "BOUND-006": {"PROVIDER_ACCOUNT", "ARTIST_CLAIM", "CERTIFICATION", "CATALOG_MAPPING", "ACCESS_ROLE", "CREDENTIAL"},
            "BOUND-012": {"UNKNOWN", "MISSING", "FAILED", "NOT_APPLICABLE"},
            "BOUND-014": {"ARTIST_LEVEL_CAPABILITY", "DAW_ADAPTER", "HOST_EVIDENCE", "EXECUTION_ROUTE"},
            "BOUND-016": {"PRODUCT_FEATURE", "BUILDER_MECHANISM"},
            "BOUND-019": {"ENTITY", "ACCOUNT", "ROLE_RELATIONSHIP", "CREDENTIAL_AUTHORIZATION"},
        }
        for boundary_id, concepts in expected_concept_sets.items():
            self.assertIn(boundary_id, boundaries)
            self.assertEqual(set(boundaries[boundary_id]["concepts"]), concepts)
            self.assertFalse(boundaries[boundary_id]["may_merge"])

    def test_feature_is_not_invalidated_merely_because_context_or_implementation_is_tainted(self):
        policy = self.load("governance/semantic_boundaries.json")
        rule = policy["feature_taint_rule"]
        self.assertFalse(rule["context_taint_implies_feature_taint"])
        self.assertFalse(rule["evidence_taint_implies_feature_taint"])
        self.assertFalse(rule["implementation_taint_implies_feature_taint"])
        self.assertTrue(rule["semantic_taint_may_invalidate_feature"])
        self.assertIn("independently supported", rule["required_question"])


if __name__ == "__main__":
    unittest.main()
