import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_governance_increment", ROOT / "governance/check_governance.py"
)
gov = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gov)


class ProductIncrementReceiptTests(unittest.TestCase):
    def clone(self):
        td = tempfile.TemporaryDirectory()
        dst = Path(td.name) / "repo"
        shutil.copytree(
            ROOT,
            dst,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        self.addCleanup(td.cleanup)
        return dst

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data, indent=2) + "\n")

    def activate_ux01(self, repo):
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
        self.write(state_path, state)

        graph_path = repo / "governance/completion_graph.json"
        graph = json.loads(graph_path.read_text())
        for node in graph["nodes"]:
            if node["state"] == "ACTIVE":
                node["state"] = "PRESERVED"
            if node["id"] == "UX-01":
                node["state"] = "ACTIVE"
        self.write(graph_path, graph)

        handoff_path = repo / "governance/handoff.json"
        handoff = json.loads(handoff_path.read_text())
        handoff["lifecycle"] = {
            "state": "ACTIVE",
            "active_node": "UX-01",
            "active_increment": "UX-01-CONTEXT-LIFECYCLE-01",
        }
        self.write(handoff_path, handoff)

        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        for key in (
            "repair_kind",
            "repair_target_kind",
            "repair_issue",
            "repair_target_merge_sha",
            "incident_repair_ids",
            "closed_incident_repair_ids",
            "closed_repair_receipt_id",
        ):
            receipt.pop(key, None)
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
                "allowed_exact_paths": [],
                "allowed_prefixes": ["n0te2/"],
            }
        )
        self.write(receipt_path, receipt)

    def test_receipt_increment_must_match_current_increment(self):
        repo = self.clone()
        self.activate_ux01(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        receipt["increment_id"] = "WRONG-INCREMENT"
        self.write(path, receipt)
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.run(repo, verify_git=False)
        self.assertIn("receipt increment must match", str(cm.exception))

    def test_increment_cannot_claim_a_different_parent_node(self):
        repo = self.clone()
        self.activate_ux01(repo)
        state_path = repo / "governance/current_state.json"
        receipt_path = repo / "governance/active_receipt.json"
        state = json.loads(state_path.read_text())
        receipt = json.loads(receipt_path.read_text())
        active = state["active_node"]
        state["active_increment"] = "DAW-00-UNRELATED"
        receipt["increment_id"] = "DAW-00-UNRELATED"
        receipt["receipt_id"] = "N0TE2-DAW-00-UNRELATED"
        self.write(state_path, state)
        self.write(receipt_path, receipt)
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.run(repo, verify_git=False)
        self.assertIn(f"must belong to active node {active}", str(cm.exception))

    def test_active_increment_is_explicitly_parent_bound(self):
        repo = self.clone()
        self.activate_ux01(repo)
        state = json.loads((repo / "governance/current_state.json").read_text())
        increment = str(state["active_increment"])
        self.assertTrue(
            increment.startswith(str(state["active_node"])),
            f"{increment} must remain explicitly bound to {state['active_node']}",
        )
        registry_before = (repo / "governance/automation_registry.json").read_text()
        gov.run(repo, verify_git=False)
        self.assertEqual(
            (repo / "governance/automation_registry.json").read_text(),
            registry_before,
            "valid normal ACTIVE construction must not require registry lifecycle mutation",
        )


if __name__ == "__main__":
    unittest.main()
