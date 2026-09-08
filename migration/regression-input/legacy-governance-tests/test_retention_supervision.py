import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("n0te2_governance_retention", ROOT / "governance/check_governance.py")
gov = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gov)
HANDOFF_SPEC = importlib.util.spec_from_file_location("n0te2_handoff_retention", ROOT / "governance/build_handoff.py")
handoff_mod = importlib.util.module_from_spec(HANDOFF_SPEC)
HANDOFF_SPEC.loader.exec_module(handoff_mod)


class RetentionSupervisionRegressionTests(unittest.TestCase):
    def clone(self):
        td = tempfile.TemporaryDirectory()
        dst = Path(td.name) / "repo"
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        self.addCleanup(td.cleanup)
        return dst

    @staticmethod
    def write_json(path, payload):
        Path(path).write_text(json.dumps(payload, indent=2) + "\n")

    def assert_red(self, repo, needle):
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.run(repo, verify_git=False)
        self.assertIn(needle, str(cm.exception))

    def stabilize(self, repo):
        state_path = repo / "governance/current_state.json"
        state = json.loads(state_path.read_text())
        state.update(
            {
                "lifecycle_state": "STABLE",
                "active_node": None,
                "active_increment": None,
                "product_code_authorized": False,
                "legacy_admission_authorized": False,
                "terminal_reason": "No currently justified dependency-ready construction work exists.",
                "wake_condition": None,
            }
        )
        self.write_json(state_path, state)

        graph_path = repo / "governance/completion_graph.json"
        graph = json.loads(graph_path.read_text())
        for node in graph["nodes"]:
            if node["state"] == "ACTIVE":
                node["state"] = "PRESERVED"
        self.write_json(graph_path, graph)

        handoff_path = repo / "governance/handoff.json"
        handoff = json.loads(handoff_path.read_text())
        handoff["lifecycle"] = {"state": "STABLE", "active_node": None, "active_increment": None}
        self.write_json(handoff_path, handoff)

        automation_path = repo / "governance/automation_registry.json"
        automation = json.loads(automation_path.read_text())
        controller = next(
            row
            for row in automation["actors"]
            if row["id"] == "AUTO-CONSTRUCTION-CONTROLLER-001"
        )
        controller["lifecycle"]["state"] = "DORMANT"
        self.write_json(automation_path, automation)

        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["status"] = "INACTIVE"
        receipt["product_code_allowed"] = False
        self.write_json(receipt_path, receipt)

    def activate_ux01(self, repo, *, activate_receipt=True):
        state_path = repo / "governance/current_state.json"
        state = json.loads(state_path.read_text())
        state.update(
            {
                "lifecycle_state": "ACTIVE",
                "active_node": "UX-01",
                "active_increment": "UX-01-CONTEXT-LIFECYCLE-01",
                "terminal_reason": None,
                "wake_condition": None,
                "product_code_authorized": True,
                "legacy_admission_authorized": False,
            }
        )
        self.write_json(state_path, state)

        graph_path = repo / "governance/completion_graph.json"
        graph = json.loads(graph_path.read_text())
        for node in graph["nodes"]:
            if node["state"] == "ACTIVE":
                node["state"] = "PRESERVED"
            if node["id"] == "UX-01":
                node["state"] = "ACTIVE"
        self.write_json(graph_path, graph)

        handoff_path = repo / "governance/handoff.json"
        handoff = json.loads(handoff_path.read_text())
        handoff["lifecycle"] = {
            "state": "ACTIVE",
            "active_node": "UX-01",
            "active_increment": "UX-01-CONTEXT-LIFECYCLE-01",
        }
        self.write_json(handoff_path, handoff)

        automation_path = repo / "governance/automation_registry.json"
        automation = json.loads(automation_path.read_text())
        controller = next(
            row
            for row in automation["actors"]
            if row["id"] == "AUTO-CONSTRUCTION-CONTROLLER-001"
        )
        controller["lifecycle"]["state"] = "ACTIVE"
        self.write_json(automation_path, automation)

        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["status"] = "INACTIVE"
        receipt["product_code_allowed"] = False
        if activate_receipt:
            receipt.update(
                {
                    "status": "ACTIVE",
                    "node_id": "UX-01",
                    "increment_id": "UX-01-CONTEXT-LIFECYCLE-01",
                    "receipt_id": "N0TE2-UX-01-CONTEXT-LIFECYCLE-01",
                    "product_code_allowed": True,
                    "legacy_admission_allowed": False,
                    "legacy_source_copy_allowed": False,
                    "legacy_test_text_copy_allowed": False,
                }
            )
        self.write_json(receipt_path, receipt)

    def test_known_scope_does_not_select_work(self):
        doc = json.loads((ROOT / "governance/requirements.json").read_text())
        self.assertEqual(doc["default_classification"], "KNOWN")
        self.assertTrue(doc["known_blocks_candidate"])
        self.assertFalse(doc["known_selects_work"])
        self.assertEqual(doc["sequence_role"], "BUILD_GRAPH_INDEX")
        canonical = doc["canonical_scope"]
        sequence = doc["sequence"]
        self.assertEqual(canonical["start"], sequence["start"])
        self.assertGreater(canonical["end"], sequence["end"])
        self.assertEqual(
            canonical["retained_requirement_count"],
            canonical["end"] - canonical["start"] + 1,
        )
        extensions = doc["canonical_extensions"]
        self.assertEqual(
            [row["id"] for row in extensions],
            [
                f"REQ-SCOPE-{n:03d}"
                for n in range(sequence["end"] + 1, canonical["end"] + 1)
            ],
        )
        self.assertTrue(all(row["state"] == "MAPPED" for row in extensions))
        self.assertTrue(all(row["selected"] is False for row in extensions))

    def test_stable_lifecycle_allows_zero_active_nodes(self):
        repo = self.clone()
        self.stabilize(repo)
        gov.run(repo, verify_git=False)

    def test_stable_cannot_leave_zombie_active_node(self):
        repo = self.clone()
        state_path = repo / "governance/current_state.json"
        state = json.loads(state_path.read_text())
        state.update(
            {
                "lifecycle_state": "STABLE",
                "active_node": "UX-01",
                "active_increment": None,
                "product_code_authorized": False,
                "terminal_reason": "done",
            }
        )
        self.write_json(state_path, state)
        self.assert_red(repo, "STABLE cannot retain an active_node")

    def test_terminal_handoff_cannot_leave_active_construction_receipt(self):
        repo = self.clone()
        self.stabilize(repo)
        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["status"] = "ACTIVE"
        receipt["product_code_allowed"] = True
        self.write_json(receipt_path, receipt)
        with mock.patch.object(handoff_mod, "git", return_value="a" * 40):
            with self.assertRaises(handoff_mod.HandoffError) as cm:
                handoff_mod.build_runtime_handoff(repo)
        self.assertIn("cannot carry an ACTIVE construction receipt", str(cm.exception))

    def test_active_handoff_requires_matching_active_receipt(self):
        repo = self.clone()
        self.activate_ux01(repo, activate_receipt=False)
        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["status"], "INACTIVE")
        self.assertFalse(receipt["product_code_allowed"])
        with mock.patch.object(handoff_mod, "git", return_value="a" * 40):
            with self.assertRaises(handoff_mod.HandoffError) as cm:
                handoff_mod.build_runtime_handoff(repo)
        self.assertIn("ACTIVE lifecycle requires an ACTIVE construction or repair receipt", str(cm.exception))

    def test_automation_cannot_escape_supervision_graph(self):
        repo = self.clone()
        path = repo / "governance/automation_registry.json"
        payload = json.loads(path.read_text())
        payload["actors"][0]["parent"] = "SELF"
        self.write_json(path, payload)
        self.assert_red(repo, "escaped supervision parent")

    def test_automation_reactivation_must_be_observable(self):
        repo = self.clone()
        path = repo / "governance/automation_registry.json"
        payload = json.loads(path.read_text())
        payload["actors"][0]["observability"]["reactivation_is_event"] = False
        self.write_json(path, payload)
        self.assert_red(repo, "reactivate silently")

    def test_constitutional_invariant_cannot_disappear(self):
        repo = self.clone()
        path = repo / "governance/invariants.json"
        payload = json.loads(path.read_text())
        payload["constitutional"] = [r for r in payload["constitutional"] if r["id"] != "INV-HANDOFF-001"]
        self.write_json(path, payload)
        self.assert_red(repo, "invariant missing")

    def test_handoff_must_match_current_selection(self):
        repo = self.clone()
        path = repo / "governance/handoff.json"
        payload = json.loads(path.read_text())
        payload["lifecycle"]["active_increment"] = "UX-01-WRONG"
        self.write_json(path, payload)
        self.assert_red(repo, "handoff active increment is stale")

    def test_discovery_closure_is_durable_and_reconstructable(self):
        policy = json.loads((ROOT / "governance/discovery_closure.json").read_text())
        self.assertEqual(policy["policy_id"], "DISCOVERY-CLOSURE-001")
        self.assertEqual(
            set(policy["allowed_dispositions"]),
            {"EXECUTED", "DURABLY_CAPTURED", "DUPLICATE", "BLOCKED", "REJECTED", "NON_ACTIONABLE"},
        )
        self.assertIn("ORPHAN_DISCOVERY", policy["failure_definition"])
        self.assertEqual(policy["unfinished_work_policy"], "governance/work_continuity.json")

        invariants = json.loads((ROOT / "governance/invariants.json").read_text())
        invariant_ids = {row["id"] for row in invariants["constitutional"]}
        self.assertTrue({"INV-CLOSE-001", "INV-CLOSE-002"}.issubset(invariant_ids))

        handoff = json.loads((ROOT / "governance/handoff.json").read_text())
        self.assertIn("DISCOVERY_CLOSURE", handoff["reconstruction"]["required_outcomes"])
        self.assertIn("governance/discovery_closure.json", handoff["reconstruction"]["required_refs"])

        authority = json.loads((ROOT / "governance/authority.json").read_text())
        self.assertIn("governance/discovery_closure.json", authority["current_authority_files"])
        self.assertTrue(authority["laws"]["material_discoveries_require_cycle_disposition"])
        self.assertTrue(authority["laws"]["safe_authorized_discovered_work_executes_instead_of_stopping_at_advice"])
        self.assertTrue(authority["laws"]["discovery_closure_does_not_grant_new_scope_or_authority"])

    def test_unfinished_work_is_durable_across_turn_and_run_boundaries(self):
        policy = json.loads((ROOT / "governance/work_continuity.json").read_text())
        self.assertEqual(policy["policy_id"], "WORK-CONTINUITY-001")
        self.assertEqual(policy["authority"], "USER_ROOT")
        self.assertTrue(policy["runtime_boundary"]["hard_state_continuity"])
        self.assertFalse(policy["runtime_boundary"]["pretend_background_execution"])
        self.assertIn("WORK_LOSS", policy["failure_definition"])
        self.assertEqual(
            set(policy["legal_unfinished_states"]),
            {"ACTIVE", "WAITING", "BLOCKED"},
        )
        required_fields = {
            "canonical_work_id",
            "outcome",
            "owner",
            "state",
            "progress_checkpoint",
            "state_basis_or_evidence",
            "remaining_work",
            "blocker_or_waiting_condition",
            "next_admissible_action",
            "wake_condition",
            "completion_condition",
        }
        self.assertEqual(set(policy["required_checkpoint_fields"]), required_fields)

        invariants = json.loads((ROOT / "governance/invariants.json").read_text())
        invariant_ids = {row["id"] for row in invariants["constitutional"]}
        self.assertTrue({"INV-WORK-001", "INV-WORK-002", "INV-WORK-003"}.issubset(invariant_ids))

        handoff = json.loads((ROOT / "governance/handoff.json").read_text())
        self.assertIn("UNFINISHED_WORK_CONTINUITY", handoff["reconstruction"]["required_outcomes"])
        self.assertIn("governance/work_continuity.json", handoff["reconstruction"]["required_refs"])

        authority = json.loads((ROOT / "governance/authority.json").read_text())
        self.assertIn("WORK_CONTINUITY_POLICY", authority["authority_order"])
        self.assertIn("governance/work_continuity.json", authority["current_authority_files"])
        self.assertTrue(authority["laws"]["turn_or_run_boundary_is_not_task_completion"])
        self.assertTrue(authority["laws"]["unfinished_material_work_requires_durable_checkpoint"])
        self.assertTrue(authority["laws"]["unfinished_work_reconstructs_before_unrelated_reselection"])
        self.assertTrue(authority["laws"]["continuity_must_not_fake_background_execution"])
        self.assertTrue(authority["laws"]["partial_execution_must_not_be_reported_complete"])


if __name__ == "__main__":
    unittest.main()
