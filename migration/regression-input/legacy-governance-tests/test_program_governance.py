from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROGRAM_SPEC = importlib.util.spec_from_file_location(
    "n0te2_program_governance",
    ROOT / "governance/check_program.py",
)
program = importlib.util.module_from_spec(PROGRAM_SPEC)
PROGRAM_SPEC.loader.exec_module(program)

LEGACY_SPEC = importlib.util.spec_from_file_location(
    "n0te2_legacy_governance",
    ROOT / "governance/check_governance.py",
)
legacy = importlib.util.module_from_spec(LEGACY_SPEC)
LEGACY_SPEC.loader.exec_module(legacy)


def clone_repo() -> tuple[tempfile.TemporaryDirectory, Path]:
    td = tempfile.TemporaryDirectory()
    repo = Path(td.name) / "repo"
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    return td, repo


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def governance_acceptance(state: str = "IN_PROGRESS") -> dict:
    return {
        "implementation_state": state,
        "integration_state": "NOT_APPLICABLE_GOVERNANCE",
        "reachability_state": "NOT_APPLICABLE_GOVERNANCE",
        "authority_safe": "NOT_APPLICABLE_GOVERNANCE",
        "controlled_consumer_workflow": "NOT_APPLICABLE_GOVERNANCE",
        "controlled_interruption_recovery": "NOT_APPLICABLE_GOVERNANCE",
        "consumer_acceptance": "NOT_APPLICABLE_GOVERNANCE",
        "real_work_eligible": False,
        "personal_production_accepted": False,
        "next_missing_proof": "governance regression",
    }


def product_acceptance() -> dict:
    return {
        "implementation_state": "IN_PROGRESS",
        "integration_state": "NOT_RUN",
        "reachability_state": "NOT_RUN",
        "authority_safe": "NOT_RUN",
        "controlled_consumer_workflow": "NOT_RUN",
        "controlled_interruption_recovery": "NOT_RUN",
        "consumer_acceptance": "NOT_RUN",
        "real_work_eligible": False,
        "personal_production_accepted": False,
        "next_missing_proof": "complete implementation and controlled proof",
    }


def activate_synthetic_program(repo: Path) -> None:
    graph_path = repo / "governance/completion_graph.json"
    graph = json.loads(graph_path.read_text())
    for node in graph["nodes"]:
        if node["id"] == "UX-01":
            node["state"] = "ACTIVE"
        elif node["state"] == "ACTIVE":
            node["state"] = "PRESERVED"
    write_json(graph_path, graph)

    current_path = repo / "governance/current_state.json"
    current = json.loads(current_path.read_text())
    current.update(
        {
            "lifecycle_state": "ACTIVE",
            "active_node": "UX-01",
            "active_increment": "UX-01-PROGRAM-COMPAT-01",
            "active_program": "PP-TEST-001",
            "program_mode": "PARENT_WITH_BOUNDED_WORK_PACKAGES",
            "compatibility_anchor_node": "UX-01",
            "product_code_authorized": True,
            "legacy_admission_authorized": False,
        }
    )
    write_json(current_path, current)

    receipt_path = repo / "governance/active_receipt.json"
    receipt = {
        "status": "ACTIVE",
        "receipt_id": "N0TE2-UX-01-PROGRAM-COMPAT-01",
        "node_id": "UX-01",
        "increment_id": "UX-01-PROGRAM-COMPAT-01",
        "baseline_sha": "1" * 40,
        "program_id": "PP-TEST-001",
        "program_requirement_id": "REQ-SCOPE-172",
        "compatibility_anchor_node": "UX-01",
        "product_code_allowed": True,
        "legacy_admission_allowed": False,
        "legacy_source_copy_allowed": False,
        "legacy_test_text_copy_allowed": False,
        "allowed_exact_paths": ["governance/program-proof.json"],
        "allowed_prefixes": ["n0te2/", "tests/core/", "governance/", "tests/governance/"],
        "external_canonical_requirements": [
            {
                "id": "REQ-SCOPE-171",
                "source": "N0TE_PRODUCT_DB/SCOPE_LEDGER",
                "source_revision": "2026-09-07/REQ-SCOPE-172",
                "semantic_key": "sem-provider-agnostic-llm-reasoning-tool-use-context-capability-substrate",
                "construction_affinity": ["UX-01", "CORE-04", "CONV-01"],
                "state": "MAPPED",
                "selected": False,
            },
            {
                "id": "REQ-SCOPE-172",
                "source": "N0TE_PRODUCT_DB/SCOPE_LEDGER",
                "source_revision": "2026-09-07/REQ-SCOPE-172",
                "semantic_key": "sem-whole-product-program-work-package-progressive-proof-personal-production-acceptance",
                "construction_affinity": ["UX-01", "CONV-01"],
                "state": "MAPPED",
                "selected": False,
            },
        ],
        "work_packages": [
            {
                "package_id": "GOV-172-TEST",
                "status": "ACTIVE",
                "kind": "GOVERNANCE",
                "requirement_ids": ["REQ-SCOPE-172"],
                "construction_affinity": ["UX-01"],
                "depends_on_packages": [],
                "product_code_allowed": False,
                "allowed_exact_paths": ["governance/program-proof.json"],
                "allowed_prefixes": [],
                "acceptance": governance_acceptance(),
            },
            {
                "package_id": "INTEL-171-TEST",
                "status": "ACTIVE",
                "kind": "PRODUCT",
                "requirement_ids": ["REQ-SCOPE-171"],
                "construction_affinity": ["CORE-04"],
                "depends_on_packages": [],
                "product_code_allowed": True,
                "stateful_or_mutating": False,
                "external_consequence": False,
                "allowed_exact_paths": ["n0te2/intelligence.py"],
                "allowed_prefixes": [],
                "acceptance": product_acceptance(),
            },
        ],
    }
    write_json(receipt_path, receipt)


def product_package(receipt: dict) -> dict:
    return next(row for row in receipt["work_packages"] if row["package_id"] == "INTEL-171-TEST")


def test_current_repository_program_contract_is_valid() -> None:
    current = json.loads((ROOT / "governance/current_state.json").read_text())
    result = program.check_program(ROOT, verify_git=True)
    if current.get("active_program"):
        assert result["status"] == "ACTIVE"
        assert result["program_id"] == current["active_program"]
        assert result["active_packages"]
    else:
        assert result == {"status": "NO_ACTIVE_PROGRAM", "active_packages": []}


def test_parent_program_accepts_product_db_requirements_beyond_local_build_manifest() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        result = program.check_program(repo, verify_git=False)
        assert result["status"] == "ACTIVE"
        assert set(result["active_packages"]) == {"GOV-172-TEST", "INTEL-171-TEST"}
    finally:
        td.cleanup()


def test_parent_program_remains_compatible_with_existing_construction_gate() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        requirements = legacy.check_requirements(repo)
        graph = legacy.check_graph(repo, requirements)
        current = legacy.check_stage(repo, graph)
        receipt = legacy.check_receipt(repo, verify_git=False, current=current)
        assert current["active_node"] == "UX-01"
        assert receipt["program_id"] == "PP-TEST-001"
        assert receipt["product_code_allowed"] is True
    finally:
        td.cleanup()


def test_held_or_superseded_requirement_cannot_be_selected() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product_package(receipt)["requirement_ids"] = ["REQ-SCOPE-044"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="held, superseded"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


@pytest.mark.parametrize("missing_field", ["integration_state", "reachability_state", "authority_safe"])
def test_real_work_requires_integration_reachability_and_authority(missing_field: str) -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        package = product_package(receipt)
        acceptance = package["acceptance"]
        acceptance.update(
            {
                "implementation_state": "COMPLETE",
                "integration_state": "PASS",
                "reachability_state": "PASS",
                "authority_safe": "PASS",
                "controlled_consumer_workflow": "PASS",
                "real_work_eligible": True,
            }
        )
        acceptance[missing_field] = "NOT_RUN"
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match=f"{missing_field} PASS"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_real_work_cannot_precede_controlled_consumer_proof() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        package = product_package(receipt)
        package["acceptance"].update(
            {
                "implementation_state": "COMPLETE",
                "integration_state": "PASS",
                "reachability_state": "PASS",
                "authority_safe": "PASS",
                "real_work_eligible": True,
            }
        )
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="controlled consumer workflow PASS"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_stateful_real_work_requires_interruption_recovery_proof() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        package = product_package(receipt)
        package["stateful_or_mutating"] = True
        package["acceptance"].update(
            {
                "implementation_state": "COMPLETE",
                "integration_state": "PASS",
                "reachability_state": "PASS",
                "authority_safe": "PASS",
                "controlled_consumer_workflow": "PASS",
                "real_work_eligible": True,
            }
        )
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="interruption/recovery PASS"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_product_package_must_explicitly_classify_side_effect_risk() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product_package(receipt).pop("external_consequence")
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="external_consequence"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_personal_production_acceptance_requires_consumer_acceptance() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        package = product_package(receipt)
        package["acceptance"].update(
            {
                "implementation_state": "COMPLETE",
                "integration_state": "PASS",
                "reachability_state": "PASS",
                "authority_safe": "PASS",
                "controlled_consumer_workflow": "PASS",
                "real_work_eligible": True,
                "personal_production_accepted": True,
                "next_missing_proof": "",
            }
        )
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="consumer_acceptance PASS"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_done_dependency_must_have_completed_its_own_contract() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        governance = next(row for row in receipt["work_packages"] if row["package_id"] == "GOV-172-TEST")
        governance["status"] = "DONE"
        product_package(receipt)["depends_on_packages"] = ["GOV-172-TEST"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="DONE governance package"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_sibling_packages_cannot_overlap_path_authority_case_insensitively() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        governance = next(row for row in receipt["work_packages"] if row["package_id"] == "GOV-172-TEST")
        governance["allowed_exact_paths"] = ["n0te2/INTELLIGENCE.PY"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="path authority overlaps"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_external_canonical_requirement_must_cite_product_db_and_not_self_select() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        receipt["external_canonical_requirements"][0]["source"] = "CHAT"
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="N0TE_PRODUCT_DB/SCOPE_LEDGER"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_orphaned_program_receipt_fails_when_current_state_has_no_program() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        current_path = repo / "governance/current_state.json"
        current = json.loads(current_path.read_text())
        current.pop("active_program")
        write_json(current_path, current)
        with pytest.raises(program.ProgramGovernanceError, match="receipt retains parent-program authority"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()
