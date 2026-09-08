from __future__ import annotations

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

GOV_SPEC = importlib.util.spec_from_file_location(
    "n0te2_governance_incident_repair",
    ROOT / "governance/check_governance.py",
)
gov = importlib.util.module_from_spec(GOV_SPEC)
GOV_SPEC.loader.exec_module(gov)

HANDOFF_SPEC = importlib.util.spec_from_file_location(
    "n0te2_handoff_incident_repair",
    ROOT / "governance/build_handoff.py",
)
handoff = importlib.util.module_from_spec(HANDOFF_SPEC)
HANDOFF_SPEC.loader.exec_module(handoff)

AUTH_SPEC = importlib.util.spec_from_file_location(
    "n0te2_incident_repair_authority",
    ROOT / "governance/check_incident_repair_authority.py",
)
authority = importlib.util.module_from_spec(AUTH_SPEC)
AUTH_SPEC.loader.exec_module(authority)

INCIDENT_206 = "INC-2026-09-05-STEWARD-INTEGRATION-206"
REPAIR_NODE = "INCIDENT-REPAIR"
REPAIR_INCREMENT = "INCIDENT-REPAIR-TEST-01"


class IncidentRepairLifecycleTests(unittest.TestCase):
    def clone(self) -> Path:
        td = tempfile.TemporaryDirectory()
        dst = Path(td.name) / "repo"
        shutil.copytree(
            ROOT,
            dst,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        self.addCleanup(td.cleanup)

        state_path = dst / "governance/current_state.json"
        state = self.read_json(state_path)
        state.update(
            {
                "lifecycle_state": "ACTIVE",
                "active_node": REPAIR_NODE,
                "active_increment": REPAIR_INCREMENT,
                "terminal_reason": None,
                "wake_condition": "Synthetic lifecycle repair fixture remains active.",
                "next_admissible_action": "Exercise only the synthetic incident repair.",
                "product_code_authorized": False,
                "legacy_admission_authorized": False,
            }
        )
        self.write_json(state_path, state)

        receipt_path = dst / "governance/active_receipt.json"
        receipt = self.read_json(receipt_path)
        for key in (
            "closed_incident_repair_ids",
            "closed_repair_receipt_id",
            "repair_target_merge_sha",
        ):
            receipt.pop(key, None)
        receipt.update(
            {
                "status": "ACTIVE",
                "receipt_id": f"N0TE2-{REPAIR_INCREMENT}",
                "node_id": REPAIR_NODE,
                "increment_id": REPAIR_INCREMENT,
                "baseline_sha": "a" * 40,
                "product_code_allowed": False,
                "legacy_admission_allowed": False,
                "legacy_source_copy_allowed": False,
                "legacy_test_text_copy_allowed": False,
                "repair_kind": "INCIDENT_REPAIR",
                "repair_target_kind": "GOVERNANCE",
                "repair_issue": 247,
                "incident_repair_ids": [INCIDENT_206],
                "allowed_exact_paths": [],
                "allowed_prefixes": [],
                "forbidden_prefixes": ["app/", "src/", "legacy/", "vendor/"],
            }
        )
        self.write_json(receipt_path, receipt)

        incidents_path = dst / "governance/incidents.jsonl"
        rows = [
            json.loads(line)
            for line in incidents_path.read_text().splitlines()
            if line.strip()
        ]
        normalized: list[dict] = []
        kept_incident = False
        for row in rows:
            if row.get("id") == INCIDENT_206:
                if kept_incident:
                    continue
                kept_incident = True
            normalized.append(row)
        incidents_path.write_text(
            "\n".join(json.dumps(row, separators=(",", ":")) for row in normalized)
            + "\n"
        )
        return dst

    @staticmethod
    def write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2) + "\n")

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text())

    def stabilize(self, repo: Path) -> None:
        state_path = repo / "governance/current_state.json"
        state = self.read_json(state_path)
        state.update(
            {
                "lifecycle_state": "STABLE",
                "active_node": None,
                "active_increment": None,
                "terminal_reason": "Test fixture is stable.",
                "wake_condition": "A bounded test repair is selected.",
                "next_admissible_action": "Select one bounded test repair.",
                "product_code_authorized": False,
                "legacy_admission_authorized": False,
            }
        )
        self.write_json(state_path, state)

        graph_path = repo / "governance/completion_graph.json"
        graph = self.read_json(graph_path)
        for node in graph["nodes"]:
            if node["state"] == "ACTIVE":
                node["state"] = "PRESERVED"
        self.write_json(graph_path, graph)

        receipt_path = repo / "governance/active_receipt.json"
        receipt = self.read_json(receipt_path)
        for key in (
            "repair_kind",
            "repair_target_kind",
            "repair_issue",
            "repair_target_merge_sha",
            "closed_repair_receipt_id",
            "incident_repair_ids",
            "closed_incident_repair_ids",
        ):
            receipt.pop(key, None)
        receipt.update(
            {
                "status": "INACTIVE",
                "product_code_allowed": False,
                "legacy_admission_allowed": False,
                "legacy_source_copy_allowed": False,
                "legacy_test_text_copy_allowed": False,
            }
        )
        self.write_json(receipt_path, receipt)

    def activate_repair(
        self,
        repo: Path,
        *,
        baseline_sha: str,
        target_kind: str = "GOVERNANCE",
        product_code: bool = False,
        target_merge_sha: str | None = None,
        allowed_exact_paths: list[str] | None = None,
    ) -> None:
        state_path = repo / "governance/current_state.json"
        state = self.read_json(state_path)
        state.update(
            {
                "lifecycle_state": "ACTIVE",
                "active_node": REPAIR_NODE,
                "active_increment": REPAIR_INCREMENT,
                "terminal_reason": None,
                "wake_condition": "Repair remains active until its exact receipt is dispositioned.",
                "next_admissible_action": "Complete only the bounded incident repair.",
                "product_code_authorized": product_code,
                "legacy_admission_authorized": False,
            }
        )
        self.write_json(state_path, state)

        graph_path = repo / "governance/completion_graph.json"
        graph = self.read_json(graph_path)
        for node in graph["nodes"]:
            if node["state"] == "ACTIVE":
                node["state"] = "PRESERVED"
        self.write_json(graph_path, graph)

        receipt_path = repo / "governance/active_receipt.json"
        receipt = self.read_json(receipt_path)
        receipt.pop("closed_incident_repair_ids", None)
        receipt.pop("closed_repair_receipt_id", None)
        receipt.update(
            {
                "status": "ACTIVE",
                "receipt_id": f"N0TE2-{REPAIR_INCREMENT}",
                "node_id": REPAIR_NODE,
                "increment_id": REPAIR_INCREMENT,
                "baseline_sha": baseline_sha,
                "product_code_allowed": product_code,
                "legacy_admission_allowed": False,
                "legacy_source_copy_allowed": False,
                "legacy_test_text_copy_allowed": False,
                "repair_kind": "INCIDENT_REPAIR",
                "repair_target_kind": target_kind,
                "repair_issue": 247,
                "incident_repair_ids": [INCIDENT_206],
                "allowed_exact_paths": allowed_exact_paths or [],
                "allowed_prefixes": ["governance/", "tests/governance/"],
                "forbidden_prefixes": ["app/", "src/", "legacy/", "vendor/"],
            }
        )
        if target_merge_sha is None:
            receipt.pop("repair_target_merge_sha", None)
        else:
            receipt["repair_target_merge_sha"] = target_merge_sha
        self.write_json(receipt_path, receipt)

    def close_repair(
        self,
        repo: Path,
        *,
        baseline_sha: str,
        resolve_incident: bool,
        evidence_bound: bool = True,
    ) -> None:
        active_receipt = self.read_json(repo / "governance/active_receipt.json")
        active_receipt_id = active_receipt["receipt_id"]
        closure_receipt_id = f"{active_receipt_id}-CLOSURE"
        active_incident_ids = list(active_receipt["incident_repair_ids"])

        if resolve_incident:
            incidents_path = repo / "governance/incidents.jsonl"
            rows = [
                json.loads(line)
                for line in incidents_path.read_text().splitlines()
                if line.strip()
            ]
            resolution = {
                "id": INCIDENT_206,
                "recorded_at": "2026-09-05",
                "status": "RESOLVED_TEST_FIXTURE",
                "severity": "TEST_ONLY",
                "summary": "Append-only test event resolves the exact incident repair.",
            }
            if evidence_bound:
                resolution.update(
                    {
                        "resolved_at": "2026-09-05",
                        "resolution_condition": "MET in the bounded test fixture with exact repair and closure receipt binding.",
                        "evidence": {
                            "test_fixture": True,
                            "exact_repair_receipt": active_receipt_id,
                            "exact_closure_receipt": closure_receipt_id,
                        },
                        "repair_receipt_id": active_receipt_id,
                        "closure_receipt_id": closure_receipt_id,
                    }
                )
            rows.append(resolution)
            incidents_path.write_text(
                "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
                + "\n"
            )

        state_path = repo / "governance/current_state.json"
        state = self.read_json(state_path)
        state.update(
            {
                "lifecycle_state": "STABLE",
                "active_node": None,
                "active_increment": None,
                "terminal_reason": "Exact incident repair is closed in the test fixture.",
                "wake_condition": None,
                "next_admissible_action": "Refresh live authority before selecting further work.",
                "product_code_authorized": False,
                "legacy_admission_authorized": False,
            }
        )
        self.write_json(state_path, state)

        receipt_path = repo / "governance/active_receipt.json"
        receipt = self.read_json(receipt_path)
        receipt.update(
            {
                "status": "INACTIVE",
                "receipt_id": closure_receipt_id,
                "node_id": None,
                "increment_id": None,
                "baseline_sha": baseline_sha,
                "product_code_allowed": False,
                "legacy_admission_allowed": False,
                "legacy_source_copy_allowed": False,
                "legacy_test_text_copy_allowed": False,
                "repair_kind": "INCIDENT_REPAIR_CLOSURE",
                "closed_incident_repair_ids": active_incident_ids,
                "closed_repair_receipt_id": active_receipt_id,
                "allowed_exact_paths": sorted(authority.CLOSURE_PATHS),
                "allowed_prefixes": [],
            }
        )
        receipt.pop("incident_repair_ids", None)
        receipt.pop("repair_target_kind", None)
        receipt.pop("repair_target_merge_sha", None)
        self.write_json(receipt_path, receipt)

    @staticmethod
    def init_git(repo: Path) -> str:
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "N0TE2 Repair Test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    @staticmethod
    def commit(repo: Path, message: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, stdout=subprocess.DEVNULL)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    def test_governance_repair_uses_zero_graph_nodes_and_registry_stays_declarative(self) -> None:
        repo = self.clone()
        self.stabilize(repo)
        self.activate_repair(repo, baseline_sha="a" * 40)

        registry = self.read_json(repo / "governance/automation_registry.json")
        controller = next(
            row
            for row in registry["actors"]
            if row["id"] == "AUTO-CONSTRUCTION-CONTROLLER-001"
        )
        self.assertEqual(controller["lifecycle"]["state"], "DORMANT")
        self.assertEqual(controller["lifecycle"]["health"], "DERIVED_FROM_CURRENT_STATE")
        self.assertFalse(registry["runtime_state_contract"]["registry_is_runtime_source"])

        gov.run(repo, verify_git=False)
        with mock.patch.object(handoff, "git", return_value="a" * 40):
            runtime = handoff.build_runtime_handoff(repo)
        self.assertEqual(runtime["lifecycle"]["mode"], "INCIDENT_REPAIR")
        self.assertEqual(runtime["incident_repair"]["incident_repair_ids"], [INCIDENT_206])

    def test_incident_repair_cannot_activate_a_completion_graph_node_too(self) -> None:
        repo = self.clone()
        self.stabilize(repo)
        self.activate_repair(repo, baseline_sha="a" * 40)
        graph_path = repo / "governance/completion_graph.json"
        graph = self.read_json(graph_path)
        next(node for node in graph["nodes"] if node["id"] == "UX-01")["state"] = "ACTIVE"
        self.write_json(graph_path, graph)
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.run(repo, verify_git=False)
        self.assertIn("cannot make a completion-graph node ACTIVE", str(cm.exception))

    def test_governance_repair_cannot_authorize_product_code(self) -> None:
        repo = self.clone()
        self.stabilize(repo)
        self.activate_repair(repo, baseline_sha="a" * 40, product_code=True)
        with self.assertRaises(gov.GovernanceError) as cm:
            gov.run(repo, verify_git=False)
        self.assertIn("GOVERNANCE incident repair cannot authorize product code", str(cm.exception))

    def test_inactive_incident_ids_are_not_ambient_repair_authority(self) -> None:
        repo = self.clone()
        self.stabilize(repo)
        baseline = self.init_git(repo)
        receipt_path = repo / "governance/active_receipt.json"
        receipt = self.read_json(receipt_path)
        receipt["incident_repair_ids"] = [INCIDENT_206]
        self.write_json(receipt_path, receipt)
        self.commit(repo, "smuggle inactive incident ids")
        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": baseline, "N0TE2_EVENT_MODE": "PR", "N0TE2_META_GOVERNANCE_REOPEN": ""},
            clear=False,
        ):
            with self.assertRaises(authority.RepairAuthorityError) as cm:
                authority.run(repo)
        self.assertIn("INACTIVE receipt cannot carry active incident_repair_ids", str(cm.exception))
        with mock.patch.object(handoff, "git", return_value="a" * 40):
            with self.assertRaises(handoff.HandoffError) as handoff_cm:
                handoff.build_runtime_handoff(repo)
        self.assertIn("INACTIVE receipt cannot carry incident repair authority", str(handoff_cm.exception))

    def test_protected_ordinary_checker_requires_meta_governance_reopen(self) -> None:
        repo = self.clone()
        self.stabilize(repo)
        baseline = self.init_git(repo)
        path = repo / "governance/check_governance.py"
        path.write_text(path.read_text() + "\n# unauthorized weakening probe\n")
        self.commit(repo, "change protected ordinary checker")
        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": baseline, "N0TE2_EVENT_MODE": "PR", "N0TE2_META_GOVERNANCE_REOPEN": ""},
            clear=False,
        ):
            with self.assertRaises(authority.RepairAuthorityError) as cm:
                authority.run(repo)
        self.assertIn("meta-governance reopen", str(cm.exception))

    def test_merged_product_repair_cannot_add_new_construction_path(self) -> None:
        repo = self.clone()
        self.stabilize(repo)
        target_merge = self.init_git(repo)

        incidents_path = repo / "governance/incidents.jsonl"
        rows = [json.loads(line) for line in incidents_path.read_text().splitlines() if line.strip()]
        incident = next(row for row in rows if row.get("id") == INCIDENT_206)
        incident.setdefault("evidence", {})["main_at_discovery"] = target_merge
        incidents_path.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")
        base = self.commit(repo, "bind incident discovery fixture")

        new_path = repo / "n0te2/new_feature_disguised_as_repair.py"
        self.activate_repair(
            repo,
            baseline_sha=base,
            target_kind="MERGED_PRODUCT",
            product_code=True,
            target_merge_sha=target_merge,
            allowed_exact_paths=["n0te2/new_feature_disguised_as_repair.py"],
        )
        new_path.write_text("VALUE = 1\n")
        self.commit(repo, "attempt new product through repair mode")

        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": base, "N0TE2_EVENT_MODE": "PR", "N0TE2_META_GOVERNANCE_REOPEN": "MAIN_STEWARD_LABEL"},
            clear=False,
        ):
            with self.assertRaises(authority.RepairAuthorityError) as cm:
                authority.run(repo)
        self.assertIn("absent from the target merge", str(cm.exception))

    def test_repair_closure_fails_while_named_incident_is_still_open(self) -> None:
        repo = self.clone()
        base = self.init_git(repo)
        self.close_repair(repo, baseline_sha=base, resolve_incident=False)
        self.commit(repo, "attempt closure while incident remains open")

        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": base, "N0TE2_EVENT_MODE": "PR", "N0TE2_META_GOVERNANCE_REOPEN": ""},
            clear=False,
        ):
            with self.assertRaises(authority.RepairAuthorityError) as cm:
                authority.run(repo)
        self.assertIn("requires durable RESOLVED incident truth", str(cm.exception))

    def test_repair_closure_rejects_unbound_resolution_label(self) -> None:
        repo = self.clone()
        base = self.init_git(repo)
        self.close_repair(
            repo,
            baseline_sha=base,
            resolve_incident=True,
            evidence_bound=False,
        )
        self.commit(repo, "attempt closure with naked resolution label")

        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": base, "N0TE2_EVENT_MODE": "PR", "N0TE2_META_GOVERNANCE_REOPEN": ""},
            clear=False,
        ):
            with self.assertRaises(authority.RepairAuthorityError) as cm:
                authority.run(repo)
        self.assertIn("resolution event lacks resolved_at", str(cm.exception))

    def test_repair_closure_accepts_append_only_resolution_event(self) -> None:
        repo = self.clone()
        base = self.init_git(repo)
        self.close_repair(repo, baseline_sha=base, resolve_incident=True)
        self.commit(repo, "close repair with evidence-bound append-only incident resolution")

        gov.run(repo, verify_git=False)
        with mock.patch.object(handoff, "git", return_value="b" * 40):
            runtime = handoff.build_runtime_handoff(repo)
        self.assertEqual(runtime["lifecycle"]["mode"], "TERMINAL")
        self.assertIsNone(runtime["incident_repair"])
        self.assertNotIn(INCIDENT_206, runtime["open_incidents"])
        with mock.patch.dict(
            os.environ,
            {"N0TE2_BASE_SHA": base, "N0TE2_EVENT_MODE": "PR", "N0TE2_META_GOVERNANCE_REOPEN": ""},
            clear=False,
        ):
            authority.run(repo)


if __name__ == "__main__":
    unittest.main()
