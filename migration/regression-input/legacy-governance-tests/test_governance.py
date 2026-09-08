import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("n0te2_governance", ROOT / "governance/check_governance.py")
gov = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gov)


class GovernanceRegressionTests(unittest.TestCase):
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

    def write_json(self, path, data):
        Path(path).write_text(json.dumps(data, indent=2) + "\n")

    def assert_red(self, repo, needle=None):
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.run(repo, verify_git=False)
        if needle:
            self.assertIn(needle, str(cm.exception))

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
                "allowed_exact_paths": ["governance/active_receipt.json"],
                "allowed_prefixes": ["n0te2/"],
            }
        )
        self.write_json(receipt_path, receipt)

    def test_current_contract_is_green_without_git(self):
        gov.run(ROOT, verify_git=False)

    def test_all_six_daws_plus_generic_are_required(self):
        repo = self.clone()
        p = repo / "governance/completion_graph.json"
        d = json.loads(p.read_text())
        gate = next(n for n in d["nodes"] if n["id"] == "DAW-TEST-READY")
        gate["depends_on"].remove("DAW-06")
        self.write_json(p, d)
        self.assert_red(repo, "DAW-TEST-READY")

    def test_three_platform_gate_cannot_drop_linux(self):
        repo = self.clone()
        p = repo / "governance/completion_graph.json"
        d = json.loads(p.read_text())
        gate = next(n for n in d["nodes"] if n["id"] == "PLATFORM-TEST-READY")
        gate["depends_on"].remove("PLATFORM-03")
        self.write_json(p, d)
        self.assert_red(repo, "platform gate")

    def test_core_architectures_cannot_flatten(self):
        repo = self.clone()
        p = repo / "governance/platform_support.json"
        d = json.loads(p.read_text())
        d["core_architectures"]["macOS"] = ["arm64"]
        self.write_json(p, d)
        self.assert_red(repo, "architecture matrix")

    def test_os_marketing_name_cannot_raise_floor(self):
        repo = self.clone()
        p = repo / "governance/platform_support.json"
        d = json.loads(p.read_text())
        d["policy"]["version_name_alone_is_breakpoint"] = True
        self.write_json(p, d)
        self.assert_red(repo, "marketing name")

    def test_plugins_cannot_collapse_to_vst3(self):
        repo = self.clone()
        p = repo / "governance/plugin_contract.json"
        d = json.loads(p.read_text())
        d["formats"] = {"VST3": d["formats"]["VST3"]}
        self.write_json(p, d)
        self.assert_red(repo, "plugin format model flattened")

    def test_custom_plugin_paths_are_required(self):
        repo = self.clone()
        p = repo / "governance/plugin_contract.json"
        d = json.loads(p.read_text())
        d["ask_for_additional_locations"] = False
        self.write_json(p, d)
        self.assert_red(repo, "additional plugin locations")

    def test_generic_other_is_not_manual_only(self):
        repo = self.clone()
        p = repo / "governance/completion_graph.json"
        d = json.loads(p.read_text())
        node = next(n for n in d["nodes"] if n["id"] == "DAW-07")
        node["depends_on"].remove("AUDIO-02")
        self.write_json(p, d)
        self.assert_red(repo, "manual-only")

    def test_held_scope_cannot_self_activate(self):
        repo = self.clone()
        p = repo / "governance/current_state.json"
        d = json.loads(p.read_text())
        d["active_node"] = "HOLD-001"
        self.write_json(p, d)
        self.assert_red(repo, "held item became active")

    def test_dependency_cycle_is_rejected(self):
        repo = self.clone()
        p = repo / "governance/completion_graph.json"
        d = json.loads(p.read_text())
        root = next(n for n in d["nodes"] if n["id"] == "BOOT-00")
        root["dependency_mode"] = "ALL"
        root["depends_on"] = ["BOOT-02"]
        self.write_json(p, d)
        self.assert_red(repo, "cycle")

    def test_orphan_required_scope_is_rejected(self):
        repo = self.clone()
        p = repo / "governance/completion_graph.json"
        d = json.loads(p.read_text())
        for n in d["nodes"]:
            reqs = [
                r
                for r in gov.expand_requirement_expr(n.get("requirements", ""))
                if r != "REQ-SCOPE-002"
            ]
            n["requirements"] = ",".join(r.rsplit("-", 1)[1] for r in reqs)
        self.write_json(p, d)
        self.assert_red(repo, "orphan active requirements")

    def test_canonical_scope_can_exceed_build_graph_without_being_selected(self):
        doc = json.loads((ROOT / "governance/requirements.json").read_text())
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

    def test_stale_product_north_star_cannot_be_current_authority(self):
        repo = self.clone()
        p = repo / "governance/authority.json"
        d = json.loads(p.read_text())
        d["current_authority_files"].append("PRODUCT_NORTH_STAR.md")
        self.write_json(p, d)
        self.assert_red(repo, "stale authority")

    def test_missing_acceptance_resource_cannot_be_global_stop(self):
        repo = self.clone()
        p = repo / "governance/authority.json"
        d = json.loads(p.read_text())
        d["laws"]["missing_acceptance_resource_stops_unrelated_construction"] = True
        self.write_json(p, d)
        self.assert_red(repo, "resource-wait loop")

    def test_core_receipt_must_explicitly_authorize_product_code(self):
        repo = self.clone()
        self.activate_ux01(repo)
        p = repo / "governance/active_receipt.json"
        d = json.loads(p.read_text())
        d["product_code_allowed"] = False
        self.write_json(p, d)
        self.assert_red(repo, "must explicitly authorize bounded product code")

    def test_core_receipt_cannot_reopen_legacy_copy(self):
        repo = self.clone()
        self.activate_ux01(repo)
        p = repo / "governance/active_receipt.json"
        d = json.loads(p.read_text())
        d["legacy_source_copy_allowed"] = True
        self.write_json(p, d)
        self.assert_red(repo, "direct legacy source copy")

    def test_receipt_rejects_unselected_product_path_change(self):
        repo = self.clone()
        self.activate_ux01(repo)
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
            ["git", "config", "user.name", "N0TE2 Test"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "maintenance.auto", "false"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "gc.auto", "0"],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "seed complete repository"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        baseline = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
        ).strip()
        rp = repo / "governance/active_receipt.json"
        receipt = json.loads(rp.read_text())
        active = receipt["node_id"]
        receipt["baseline_sha"] = baseline
        self.write_json(rp, receipt)
        subprocess.run(
            ["git", "add", "governance/active_receipt.json"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "bind synthetic receipt baseline"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        gov.run(repo, verify_git=True)

        (repo / "src").mkdir(exist_ok=True)
        (repo / "src/product.py").write_text("print('outside selected slice')\n")
        subprocess.run(["git", "add", "src/product.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "bad adjacent product change"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.run(repo, verify_git=True)
        self.assertIn(f"outside {active} receipt", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
