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
    "n0te2_incident_repair_exact_paths",
    ROOT / "governance/check_incident_repair_authority.py",
)
authority = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(authority)

INCIDENT_206 = "INC-2026-09-05-STEWARD-INTEGRATION-206"
MERGED_PRODUCT_PATH = "n0te2/app_runtime.py"
FIXTURE_INCREMENT = "INCIDENT-REPAIR-EXACT-PATH-TEST"


def normalize_active_repair_fixture(repo: Path) -> None:
    state_path = repo / "governance/current_state.json"
    state = json.loads(state_path.read_text())
    state.update(
        {
            "lifecycle_state": "ACTIVE",
            "active_node": "INCIDENT-REPAIR",
            "active_increment": FIXTURE_INCREMENT,
            "terminal_reason": None,
            "wake_condition": "Synthetic exact-path repair fixture remains active.",
            "next_admissible_action": "Exercise only the synthetic incident repair.",
            "product_code_authorized": False,
            "legacy_admission_authorized": False,
        }
    )
    state_path.write_text(json.dumps(state, indent=2) + "\n")

    receipt_path = repo / "governance/active_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    for key in (
        "closed_incident_repair_ids",
        "closed_repair_receipt_id",
        "repair_target_merge_sha",
    ):
        receipt.pop(key, None)
    receipt.update(
        {
            "status": "ACTIVE",
            "receipt_id": f"N0TE2-{FIXTURE_INCREMENT}",
            "node_id": "INCIDENT-REPAIR",
            "increment_id": FIXTURE_INCREMENT,
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
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    incidents_path = repo / "governance/incidents.jsonl"
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


def clone_active_repair() -> tuple[tempfile.TemporaryDirectory, Path]:
    td = tempfile.TemporaryDirectory()
    repo = Path(td.name) / "repo"
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    normalize_active_repair_fixture(repo)
    return td, repo


def init_git(repo: Path) -> str:
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
        ["git", "config", "user.name", "N0TE2 Exact Path Test"],
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
    return head(repo)


def head(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()


def commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def bind_receipt(repo: Path, base: str, *, broad_prefixes: bool) -> None:
    path = repo / "governance/active_receipt.json"
    receipt = json.loads(path.read_text())
    receipt["baseline_sha"] = base
    receipt["allowed_exact_paths"] = ["governance/active_receipt.json"]
    receipt["allowed_prefixes"] = ["governance/"] if broad_prefixes else []
    path.write_text(json.dumps(receipt, indent=2) + "\n")


def prepare_merged_product_repair(
    repo: Path,
    *,
    broad_prefixes: bool = False,
    surplus_exact_path: bool = False,
) -> str:
    target_merge = init_git(repo)

    incidents_path = repo / "governance/incidents.jsonl"
    rows = [
        json.loads(line)
        for line in incidents_path.read_text().splitlines()
        if line.strip()
    ]
    incident = next(row for row in reversed(rows) if row.get("id") == INCIDENT_206)
    incident.setdefault("evidence", {})["main_at_discovery"] = target_merge
    incidents_path.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n"
    )
    commit(repo, "bind merged-product incident discovery fixture")
    base = head(repo)

    state_path = repo / "governance/current_state.json"
    state = json.loads(state_path.read_text())
    state["product_code_authorized"] = True
    state_path.write_text(json.dumps(state, indent=2) + "\n")

    receipt_path = repo / "governance/active_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    allowed_exact = [
        "governance/active_receipt.json",
        "governance/current_state.json",
        MERGED_PRODUCT_PATH,
    ]
    if surplus_exact_path:
        allowed_exact.append("governance/authority.json")
    receipt.update(
        {
            "baseline_sha": base,
            "product_code_allowed": True,
            "repair_target_kind": "MERGED_PRODUCT",
            "repair_target_merge_sha": target_merge,
            "allowed_exact_paths": allowed_exact,
            "allowed_prefixes": ["n0te2/"] if broad_prefixes else [],
            "forbidden_prefixes": [],
        }
    )
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    product_path = repo / MERGED_PRODUCT_PATH
    product_path.write_text(product_path.read_text() + "\n# merged-product exact-manifest fixture\n")
    commit(repo, "stage bounded merged-product repair fixture")
    return base


def run_authority(repo: Path, base: str) -> None:
    with mock.patch.dict(
        os.environ,
        {
            "N0TE2_BASE_SHA": base,
            "N0TE2_EVENT_MODE": "PR",
            "N0TE2_META_GOVERNANCE_REOPEN": "",
        },
        clear=False,
    ):
        authority.run(repo)


def test_current_pr_manifest_satisfies_specialized_repair_authority() -> None:
    receipt = json.loads((ROOT / "governance/active_receipt.json").read_text())
    base = receipt["baseline_sha"]
    with mock.patch.dict(
        os.environ,
        {
            "N0TE2_BASE_SHA": base,
            "N0TE2_EVENT_MODE": "PR",
            "N0TE2_META_GOVERNANCE_REOPEN": "MAIN_STEWARD_LABEL",
        },
        clear=False,
    ):
        authority.run(ROOT)


def test_governance_repair_rejects_broad_path_prefixes() -> None:
    td, repo = clone_active_repair()
    try:
        base = init_git(repo)
        bind_receipt(repo, base, broad_prefixes=True)
        commit(repo, "try broad governance repair prefix")

        with pytest.raises(authority.RepairAuthorityError) as exc:
            run_authority(repo, base)
        assert "must enumerate exact allowed paths" in str(exc.value)
    finally:
        td.cleanup()


def test_governance_repair_rejects_unused_surplus_exact_paths() -> None:
    td, repo = clone_active_repair()
    try:
        base = init_git(repo)
        bind_receipt(repo, base, broad_prefixes=False)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        receipt["allowed_exact_paths"].append("governance/authority.json")
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        commit(repo, "try surplus governance repair authority")

        with pytest.raises(authority.RepairAuthorityError) as exc:
            run_authority(repo, base)
        assert "must exactly equal changed paths" in str(exc.value)
    finally:
        td.cleanup()


def test_governance_repair_accepts_exact_changed_file_manifest() -> None:
    td, repo = clone_active_repair()
    try:
        base = init_git(repo)
        bind_receipt(repo, base, broad_prefixes=False)
        commit(repo, "bind exact governance repair path")

        run_authority(repo, base)
    finally:
        td.cleanup()


def test_merged_product_repair_rejects_broad_path_prefixes() -> None:
    td, repo = clone_active_repair()
    try:
        base = prepare_merged_product_repair(repo, broad_prefixes=True)

        with pytest.raises(authority.RepairAuthorityError) as exc:
            run_authority(repo, base)
        assert "MERGED_PRODUCT repair must enumerate exact allowed paths" in str(exc.value)
    finally:
        td.cleanup()


def test_merged_product_repair_rejects_unused_surplus_exact_paths() -> None:
    td, repo = clone_active_repair()
    try:
        base = prepare_merged_product_repair(repo, surplus_exact_path=True)

        with pytest.raises(authority.RepairAuthorityError) as exc:
            run_authority(repo, base)
        assert "MERGED_PRODUCT repair allowed_exact_paths must exactly equal changed paths" in str(exc.value)
    finally:
        td.cleanup()


def test_merged_product_repair_accepts_exact_changed_file_manifest() -> None:
    td, repo = clone_active_repair()
    try:
        base = prepare_merged_product_repair(repo)
        run_authority(repo, base)
    finally:
        td.cleanup()
