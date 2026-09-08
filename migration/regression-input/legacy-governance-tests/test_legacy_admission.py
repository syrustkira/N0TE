import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("n0te2_governance", ROOT / "governance/check_governance.py")
gov = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gov)


class LegacyAdmissionTests(unittest.TestCase):
    def clone(self):
        td = tempfile.TemporaryDirectory()
        dst = Path(td.name) / "repo"
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        self.addCleanup(td.cleanup)
        return dst

    def write_json(self, path, data):
        Path(path).write_text(json.dumps(data, indent=2) + "\n")

    def assert_red(self, repo, needle):
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.check_legacy_admission(repo)
        self.assertIn(needle, str(cm.exception))

    def execution_evidence(self):
        return json.loads((ROOT / "governance/legacy_execution_evidence.json").read_text())

    def test_current_legacy_contract_is_green_without_git(self):
        gov.check_legacy_admission(ROOT)
        gov.run(ROOT, verify_git=False)

    def test_census_cannot_hide_unclassified_assets(self):
        repo = self.clone()
        p = repo / "governance/legacy_admission.json"
        d = json.loads(p.read_text())
        d["census"]["unclassified_leaf_files"] = 1
        self.write_json(p, d)
        self.assert_red(repo, "unclassified legacy assets")

    def test_family_counts_must_reconcile_to_total(self):
        repo = self.clone()
        p = repo / "governance/legacy_admission.json"
        d = json.loads(p.read_text())
        d["census"]["families"]["native"] -= 1
        self.write_json(p, d)
        self.assert_red(repo, "legacy family counts")

    def test_direct_legacy_source_copy_cannot_self_authorize(self):
        repo = self.clone()
        p = repo / "governance/legacy_admission.json"
        d = json.loads(p.read_text())
        d["rights"]["direct_legacy_source_copy_authorized"] = True
        self.write_json(p, d)
        self.assert_red(repo, "direct legacy source copy")

    def test_direct_legacy_test_text_copy_cannot_self_authorize(self):
        repo = self.clone()
        p = repo / "governance/legacy_admission.json"
        d = json.loads(p.read_text())
        d["rights"]["direct_legacy_test_text_copy_authorized"] = True
        self.write_json(p, d)
        self.assert_red(repo, "direct legacy test text copy")

    def test_license_gate_cannot_disappear(self):
        repo = self.clone()
        p = repo / "governance/legacy_admission.json"
        d = json.loads(p.read_text())
        d["rights"]["license_gate_id"] = ""
        self.write_json(p, d)
        self.assert_red(repo, "LICENSE-GATE-001")

    def test_contamination_scan_cannot_drop_ableton_priority_class(self):
        repo = self.clone()
        p = repo / "governance/legacy_admission.json"
        d = json.loads(p.read_text())
        d["contamination_classes"].remove("ABLETON_SEMANTIC_PRIORITY")
        self.write_json(p, d)
        self.assert_red(repo, "contamination classes")

    def test_legacy_green_cannot_become_product_completion(self):
        repo = self.clone()
        p = repo / "governance/legacy_admission.json"
        d = json.loads(p.read_text())
        d["selected_spec_evidence"]["legacy_green_does_not_close_product_nodes"] = False
        self.write_json(p, d)
        self.assert_red(repo, "legacy green")

    def test_execution_evidence_reconciles_all_selected_groups_and_files(self):
        evidence = self.execution_evidence()
        groups = evidence["groups"]
        self.assertEqual([row["id"] for row in groups], [f"TESTMAP-{n:03d}" for n in range(1, 12)])
        selected_files = {path for row in groups for path in row["selected_files"]}
        self.assertEqual(len(selected_files), 19)
        statuses = [row["legacy_selected_execution"] for row in groups]
        self.assertEqual(statuses.count("GREEN"), 10)
        self.assertEqual(statuses.count("RED"), 1)
        summary = evidence["selected_spec_summary"]
        self.assertEqual(summary["behavior_groups"], 11)
        self.assertEqual(summary["selected_test_files"], 19)
        self.assertEqual(summary["groups_with_all_selected_legacy_tests_green"], 10)
        self.assertEqual(summary["groups_with_selected_legacy_test_failure"], 1)
        self.assertFalse(summary["all_selected_legacy_behavior_green"])
        self.assertFalse(summary["legacy_execution_can_close_product_nodes"])
        self.assertTrue(summary["every_reuse_or_adapt_requires_fresh_n0te2_boundary_proof"])

    def test_offline_selected_failure_remains_explicit(self):
        evidence = self.execution_evidence()
        offline = next(row for row in evidence["groups"] if row["id"] == "TESTMAP-006")
        self.assertEqual(offline["legacy_selected_execution"], "RED")
        self.assertEqual(len(offline["observed_failures"]), 1)
        self.assertIn("printer.local", offline["observed_failures"][0])
        self.assertIn("CONNECTED_TO_OFFLINE_PENDING_STATE_CHOICE", offline["n0te2_additional_gaps"])
        self.assertIn("OFFLINE_TO_CONNECTED_EXPLICIT_RECONCILIATION_CHOICE", offline["n0te2_additional_gaps"])

    def test_pr_merge_execution_is_not_mislabeled_exact_raw_head(self):
        evidence = self.execution_evidence()
        run = evidence["observed_execution"]
        self.assertEqual(run["workflow_run_id"], 32326588695)
        self.assertEqual(run["executed_checkout_sha"], "074e53aedd1d856543d51c38bcc3825df3cb08aa")
        self.assertEqual(run["checkout_kind"], "HEAD_DERIVED_PR_MERGE")
        self.assertEqual(run["head_in_merge"], "25e2f2f5fd8ea4adc0c7e61650531430025e59db")
        self.assertFalse(run["exact_raw_head_execution"])
        self.assertTrue(run["historical_evidence_only"])

    def test_current_authority_surface_names_execution_evidence(self):
        authority = json.loads((ROOT / "governance/authority.json").read_text())
        self.assertIn("governance/legacy_execution_evidence.json", authority["current_authority_files"])
        self.assertTrue(authority["laws"]["legacy_test_failure_must_remain_visible"])


if __name__ == "__main__":
    unittest.main()
