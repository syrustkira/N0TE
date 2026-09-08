from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_construction_controller_trust_boundary",
    ROOT / "governance/check_steward_integration.py",
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

INCIDENT_206 = "INC-2026-09-05-STEWARD-INTEGRATION-206"
CONSTRUCTION_CONTROLLER = "AUTO-CONSTRUCTION-CONTROLLER-001"


def clone_stable_fixture() -> tuple[tempfile.TemporaryDirectory, Path]:
    td = tempfile.TemporaryDirectory()
    repo = Path(td.name) / "repo"
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )

    current_path = repo / "governance/current_state.json"
    current = json.loads(current_path.read_text())
    current.update(
        {
            "lifecycle_state": "STABLE",
            "active_node": None,
            "active_increment": None,
            "terminal_reason": "Synthetic construction-controller trust fixture.",
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
        "closed_incident_repair_ids",
        "repair_target_merge_sha",
        "closed_repair_receipt_id",
    ):
        receipt.pop(key, None)
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    incidents_path = repo / "governance/incidents.jsonl"
    rows = [
        json.loads(line)
        for line in incidents_path.read_text().splitlines()
        if line.strip()
    ]
    rows.append(
        {
            "id": INCIDENT_206,
            "recorded_at": "2026-09-05",
            "status": "RESOLVED_TEST_FIXTURE",
            "severity": "TEST_ONLY",
            "summary": "Synthetic fixture resolves #206 so trusted-registry immutability is isolated.",
        }
    )
    incidents_path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n"
    )
    return td, repo


def init_git(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "N0TE2 Trust Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, stdout=subprocess.DEVNULL)


def run_trusted_gate(repo: Path, baseline: str) -> None:
    with mock.patch.dict(
        os.environ,
        {
            "N0TE2_BASE_SHA": baseline,
            "N0TE2_HEAD_SHA": "",
            "N0TE2_EVENT_MODE": "PR",
            "N0TE2_DIFF_MODE": "PR_MERGE_BASE",
            "N0TE2_PR_NUMBER": "999",
            "N0TE2_META_GOVERNANCE_REOPEN": "",
        },
        clear=False,
    ):
        gate.run(repo, verify_git=True)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("authority", "MERGE_MAIN"),
        ("parent", "SELF"),
        ("allowed_mutations", ["REPOSITORY_GOVERNANCE_STATE", "MERGE_MAIN"]),
    ],
)
def test_construction_controller_trusted_fields_cannot_change_in_ordinary_pr(
    field: str,
    replacement: object,
) -> None:
    td, repo = clone_stable_fixture()
    try:
        baseline = init_git(repo)
        registry_path = repo / "governance/automation_registry.json"
        registry = json.loads(registry_path.read_text())
        controller = next(
            row for row in registry["actors"] if row.get("id") == CONSTRUCTION_CONTROLLER
        )
        controller[field] = replacement
        registry_path.write_text(json.dumps(registry, indent=2) + "\n")
        commit(repo, f"mutate construction controller {field}")

        with pytest.raises(gate.StewardIntegrationError) as exc:
            run_trusted_gate(repo, baseline)
        message = str(exc.value)
        assert "trusted gate artifact changed in an ordinary PR" in message
        assert "governance/automation_registry.json" in message
    finally:
        td.cleanup()
