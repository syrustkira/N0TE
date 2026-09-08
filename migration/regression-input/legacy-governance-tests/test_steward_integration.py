import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_steward_integration", ROOT / "governance/check_steward_integration.py"
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class StewardIntegrationGateTests(unittest.TestCase):
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
    def write_json(path, data):
        Path(path).write_text(json.dumps(data, indent=2) + "\n")

    @staticmethod
    def stabilize_git_fixture(repo):
        current_path = repo / "governance/current_state.json"
        current = json.loads(current_path.read_text())
        current.update(
            {
                "lifecycle_state": "STABLE",
                "active_node": None,
                "active_increment": None,
                "terminal_reason": "Synthetic Steward gate fixture baseline.",
                "wake_condition": None,
                "product_code_authorized": False,
                "legacy_admission_authorized": False,
            }
        )
        current_path.write_text(json.dumps(current, indent=2) + "\n")

        graph_path = repo / "governance/completion_graph.json"
        graph = json.loads(graph_path.read_text())
        for node in graph["nodes"]:
            if node.get("state") == "ACTIVE":
                node["state"] = "PRESERVED"
        graph_path.write_text(json.dumps(graph, separators=(",", ":")) + "\n")

        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt.update(
            {
                "status": "INACTIVE",
                "product_code_allowed": False,
                "legacy_admission_allowed": False,
            }
        )
        for key in (
            "repair_kind",
            "repair_target_kind",
            "repair_issue",
            "incident_repair_ids",
            "repair_target_merge_sha",
            "closed_repair_receipt_id",
        ):
            receipt.pop(key, None)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    @classmethod
    def init_git(cls, repo):
        cls.stabilize_git_fixture(repo)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "N0TE2 Steward Test"],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "seed"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()

    @staticmethod
    def commit(repo, message):
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()

    def run_git_gate(self, repo, baseline):
        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": baseline, "N0TE2_HEAD_SHA": ""},
            clear=False,
        ):
            gate.run(repo, verify_git=True)

    def test_current_contract_is_green_without_git(self):
        gate.run(ROOT, verify_git=False)

    def test_canonical_scope_cannot_shrink_below_retained_170(self):
        repo = self.clone()
        path = repo / "governance/requirements.json"
        doc = json.loads(path.read_text())
        doc["canonical_scope"]["end"] = 169
        doc["canonical_scope"]["retained_requirement_count"] = 168
        doc["canonical_extensions"] = [
            row for row in doc["canonical_extensions"] if row["id"] != "REQ-SCOPE-170"
        ]
        self.write_json(path, doc)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("canonical retained scope changed", str(cm.exception))

    def test_canonical_source_revision_is_pinned(self):
        repo = self.clone()
        path = repo / "governance/requirements.json"
        doc = json.loads(path.read_text())
        doc["canonical_scope"]["source_revision"] = "525"
        self.write_json(path, doc)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("source revision changed", str(cm.exception))

    def test_canonical_extension_summary_cannot_be_rewritten(self):
        repo = self.clone()
        path = repo / "governance/requirements.json"
        doc = json.loads(path.read_text())
        doc["canonical_extensions"][0]["summary"] = "Arbitrary replacement meaning."
        self.write_json(path, doc)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("extension semantics", str(cm.exception))

    def test_canonical_extension_affinity_cannot_be_remapped(self):
        repo = self.clone()
        path = repo / "governance/requirements.json"
        doc = json.loads(path.read_text())
        doc["canonical_extensions"][0]["construction_affinity"] = ["BOOT-00"]
        self.write_json(path, doc)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("extension semantics", str(cm.exception))

    def test_stable_candidate_cannot_add_product_code_without_active_receipt(self):
        repo = self.clone()
        baseline = self.init_git(repo)
        product = repo / "n0te2/steward_unauthorized_probe.py"
        product.write_text("VALUE = 'must be rejected'\n")
        self.commit(repo, "unauthorized product construction")
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            self.run_git_gate(repo, baseline)
        self.assertIn("STABLE candidate changed construction-sensitive paths", str(cm.exception))

    def test_terminal_rename_out_of_product_tree_is_still_detected(self):
        repo = self.clone()
        baseline = self.init_git(repo)
        (repo / "docs").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "mv", "n0te2/__init__.py", "docs/renamed_product.py"],
            cwd=repo,
            check=True,
        )
        self.commit(repo, "try to hide product deletion behind rename detection")
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            self.run_git_gate(repo, baseline)
        self.assertIn("n0te2/__init__.py", str(cm.exception))

    def test_all_zero_event_base_fails_closed(self):
        repo = self.clone()
        self.init_git(repo)
        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": "0" * 40, "N0TE2_HEAD_SHA": ""},
            clear=False,
        ):
            with self.assertRaises(gate.StewardIntegrationError) as cm:
                gate.run(repo, verify_git=True)
        self.assertIn("all-zero candidate base is unverifiable", str(cm.exception))

    def test_stable_governance_only_repair_is_allowed(self):
        repo = self.clone()
        baseline = self.init_git(repo)
        probe = repo / "governance/steward_probe.txt"
        probe.write_text("integration-only governance repair\n")
        self.commit(repo, "governance-only repair")
        self.run_git_gate(repo, baseline)

    def test_stable_candidate_cannot_resequence_completion_graph(self):
        repo = self.clone()
        baseline = self.init_git(repo)
        path = repo / "governance/completion_graph.json"
        graph = json.loads(path.read_text())
        song = next(row for row in graph["nodes"] if row["id"] == "SONG-01")
        song["depends_on"] = ["CORE-01", "CORE-02", "APP-01", "UX-01"]
        self.write_json(path, graph)
        self.commit(repo, "try to resequence while stable")
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            self.run_git_gate(repo, baseline)
        self.assertIn("cannot resequence completion graph from a non-ACTIVE base", str(cm.exception))

    def test_empty_receipt_prefix_fails_closed(self):
        repo = self.clone()
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        receipt["allowed_prefixes"] = [""]
        self.write_json(path, receipt)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("unsafe allowed_prefixes", str(cm.exception))

    def test_partial_component_receipt_prefix_fails_closed(self):
        repo = self.clone()
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        receipt["allowed_prefixes"] = ["n0te2"]
        self.write_json(path, receipt)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("normalized directory", str(cm.exception))

    def test_open_incident_without_explicit_nonblocking_scope_fails_closed(self):
        repo = self.clone()
        path = repo / "governance/incidents.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        rows.append(
            {
                "id": "INC-TEST-BLOCKING-001",
                "recorded_at": "2026-09-05",
                "status": "OPEN",
                "severity": "BLOCKING",
                "summary": "test blocker",
            }
        )
        path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("lacks blocking_scope", str(cm.exception))

    def test_open_incident_with_explicit_nonblocking_scope_is_allowed(self):
        repo = self.clone()
        path = repo / "governance/incidents.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        rows.append(
            {
                "id": "INC-TEST-NONBLOCKING-001",
                "recorded_at": "2026-09-05",
                "status": "OPEN_MONITORED",
                "severity": "MONITORED",
                "summary": "test monitored incident",
                "blocking_scope": "NON_BLOCKING_FOR_THIS_INCREMENT",
            }
        )
        path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")
        gate.run(repo, verify_git=False)

    def test_null_handoff_lifecycle_compatibility_hook_fails_closed(self):
        repo = self.clone()
        path = repo / "governance/handoff.json"
        handoff = json.loads(path.read_text())
        handoff["lifecycle"] = None
        self.write_json(path, handoff)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("compatibility hook must be an object", str(cm.exception))

    def test_handoff_cannot_reclaim_lifecycle_ownership(self):
        repo = self.clone()
        path = repo / "governance/handoff.json"
        handoff = json.loads(path.read_text())
        handoff["derived_runtime_truth"]["lifecycle_source"] = "governance/handoff.json"
        self.write_json(path, handoff)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("lifecycle ownership declaration changed", str(cm.exception))

    def test_legacy_handoff_lifecycle_fields_must_match_current_state(self):
        repo = self.clone()
        path = repo / "governance/handoff.json"
        handoff = json.loads(path.read_text())
        conflicting_state = "STABLE" if json.loads((repo / "governance/current_state.json").read_text()).get("lifecycle_state") == "ACTIVE" else "ACTIVE"
        handoff["lifecycle"] = {"state": conflicting_state}
        self.write_json(path, handoff)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("compatibility state contradicts current_state", str(cm.exception))

    def test_static_status_cannot_become_merge_authorization(self):
        repo = self.clone()
        path = repo / "governance/merge_policy.json"
        policy = json.loads(path.read_text())
        policy["steward_gate"]["status_is_merge_authorization"] = True
        self.write_json(path, policy)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("policy drifted: status_is_merge_authorization", str(cm.exception))

    def test_pending_review_cannot_be_removed_from_merge_policy(self):
        repo = self.clone()
        path = repo / "governance/merge_policy.json"
        policy = json.loads(path.read_text())
        policy["steward_gate"]["pending_review_blocks_merge"] = False
        self.write_json(path, policy)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("policy drifted: pending_review_blocks_merge", str(cm.exception))

    def test_open_incident_default_policy_is_pinned(self):
        repo = self.clone()
        path = repo / "governance/merge_policy.json"
        policy = json.loads(path.read_text())
        policy["steward_gate"]["open_incident_default"] = "ALLOW"
        self.write_json(path, policy)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("policy drifted: open_incident_default", str(cm.exception))

    def test_receipt_prefix_contract_is_pinned(self):
        repo = self.clone()
        path = repo / "governance/merge_policy.json"
        policy = json.loads(path.read_text())
        policy["steward_gate"]["receipt_prefix_contract"] = "RAW_PREFIX"
        self.write_json(path, policy)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("policy drifted: receipt_prefix_contract", str(cm.exception))

    def test_terminal_graph_resequencing_policy_is_pinned(self):
        repo = self.clone()
        path = repo / "governance/merge_policy.json"
        policy = json.loads(path.read_text())
        policy["steward_gate"]["terminal_graph_resequencing"] = "ALLOW"
        self.write_json(path, policy)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("policy drifted: terminal_graph_resequencing", str(cm.exception))

    def test_steward_workflow_actor_is_registered_and_non_authorizing(self):
        repo = self.clone()
        path = repo / "governance/automation_registry.json"
        registry = json.loads(path.read_text())
        actor = next(
            row
            for row in registry["actors"]
            if row["id"] == "AUTO-STEWARD-INTEGRATION-GATE-001"
        )
        actor["authority"] = "MERGE_MAIN"
        self.write_json(path, registry)
        with self.assertRaises(gate.StewardIntegrationError) as cm:
            gate.run(repo, verify_git=False)
        self.assertIn("authority contract drifted: authority", str(cm.exception))

    def test_workflow_uses_base_owned_target_event_only(self):
        workflow = (ROOT / ".github/workflows/steward-integration.yml").read_text()
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("\n  pull_request:\n", workflow)
        self.assertIn("$TRUSTED_PATH/governance/check_steward_integration.py", workflow)
        self.assertIn("STATUS_STATE: pending", workflow)
        self.assertIn("steward-meta-governance-reopen", workflow)
        self.assertIn("not merge authorization", workflow)


if __name__ == "__main__":
    unittest.main()
