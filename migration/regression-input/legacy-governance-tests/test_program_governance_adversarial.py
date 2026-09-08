from __future__ import annotations

import json

import pytest

from tests.governance.test_program_governance import (
    activate_synthetic_program,
    clone_repo,
    product_package,
    write_json,
)

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "n0te2_program_governance_adversarial",
    ROOT / "governance/check_program.py",
)
program = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(program)


def _fully_evidenced_real_work(package: dict) -> None:
    acceptance = package["acceptance"]
    acceptance.update(
        {
            "implementation_state": "COMPLETE",
            "integration_state": "PASS",
            "reachability_state": "PASS",
            "authority_safe": "PASS",
            "controlled_consumer_workflow": "PASS",
            "controlled_interruption_recovery": "NOT_RUN",
            "consumer_acceptance": "NOT_RUN",
            "real_work_eligible": True,
            "personal_production_accepted": False,
            "next_missing_proof": "consumer acceptance",
            "canonical_scope_ref": "N0TE_PRODUCT_DB/SCOPE_LEDGER",
            "implementation_refs": ["tests://implementation"],
            "integration_refs": ["tests://integration"],
            "user_reachability_refs": ["tests://reachability"],
            "verification_refs": ["tests://controlled-consumer"],
            "failure_recovery_refs": [],
            "authority_security_refs": ["tests://authority"],
            "consumer_acceptance_refs": [],
            "value_evidence_refs": [],
        }
    )


def test_package_dependency_cycle_is_rejected() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        governance = next(row for row in receipt["work_packages"] if row["package_id"] == "GOV-172-TEST")
        product = product_package(receipt)
        governance["depends_on_packages"] = ["INTEL-171-TEST"]
        product["depends_on_packages"] = ["GOV-172-TEST"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="dependency graph contains a cycle"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_indexed_requirement_must_use_completion_graph_affinity() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product = product_package(receipt)
        product["requirement_ids"] = ["REQ-SCOPE-039"]
        product["construction_affinity"] = ["UX-01"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="no construction affinity permitted"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_external_requirement_affinity_must_reference_known_completion_node() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        receipt["external_canonical_requirements"][0]["construction_affinity"] = ["NOT-A-NODE"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="unknown completion node"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_held_package_cannot_carry_partial_implementation() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product = product_package(receipt)
        product["status"] = "HELD"
        product["acceptance"]["implementation_state"] = "IN_PROGRESS"
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="HELD package"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_external_semantic_keys_are_unique_case_insensitively() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        first = receipt["external_canonical_requirements"][0]["semantic_key"]
        receipt["external_canonical_requirements"][1]["semantic_key"] = first.upper()
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="semantic_key duplicates"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_git_diff_authorization_preserves_exact_path_case() -> None:
    package = {"allowed_exact_paths": ["n0te2/foo.py"], "allowed_prefixes": []}
    assert program._path_allowed("n0te2/foo.py", package) is True
    assert program._path_allowed("n0te2/FOO.py", package) is False


def test_parent_envelope_containment_preserves_exact_git_path_case() -> None:
    receipt = {"allowed_exact_paths": ["n0te2/foo.py"], "allowed_prefixes": ["tests/governance/"]}
    assert program._path_allowed_by_parent_exact("n0te2/foo.py", receipt) is True
    assert program._path_allowed_by_parent_exact("n0te2/FOO.py", receipt) is False
    assert program._prefix_allowed_by_parent("tests/governance/unit/", receipt) is True
    assert program._prefix_allowed_by_parent("Tests/governance/unit/", receipt) is False


def test_governance_package_cannot_authorize_product_paths() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        governance = next(row for row in receipt["work_packages"] if row["package_id"] == "GOV-172-TEST")
        governance["allowed_exact_paths"] = ["n0te2/governance_escape.py"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="governance package cannot authorize product"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_child_path_grant_must_remain_inside_parent_envelope() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product_package(receipt)["allowed_exact_paths"] = ["README.md"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="escapes the parent receipt envelope"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_child_cannot_claim_unrelated_completion_affinity() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product_package(receipt)["construction_affinity"] = ["CORE-04", "DAW-01"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="unsupported by its requirements"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_completion_graph_dependencies_gate_package_activation() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product = product_package(receipt)
        product["requirement_ids"] = ["REQ-SCOPE-170"]
        product["construction_affinity"] = ["OPS-02"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="graph dependencies were DONE"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_done_package_cannot_depend_on_unfinished_package() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        governance = next(row for row in receipt["work_packages"] if row["package_id"] == "GOV-172-TEST")
        governance["status"] = "DONE"
        governance["acceptance"]["implementation_state"] = "COMPLETE"
        governance["acceptance"]["authority_through_sha"] = "2" * 40
        governance["depends_on_packages"] = ["INTEL-171-TEST"]
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="claimed DONE before package dependencies were DONE"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_done_package_requires_frozen_authority_checkpoint() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        governance = next(row for row in receipt["work_packages"] if row["package_id"] == "GOV-172-TEST")
        governance["status"] = "DONE"
        governance["acceptance"]["implementation_state"] = "COMPLETE"
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="authority_through_sha"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_blocked_progress_requires_frozen_authority_checkpoint() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product = product_package(receipt)
        product["status"] = "BLOCKED"
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="authority_through_sha"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_active_package_cannot_carry_frozen_authority_checkpoint() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        product_package(receipt)["acceptance"]["authority_through_sha"] = "2" * 40
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="cannot carry frozen authority_through_sha"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_historical_authority_does_not_cover_later_change(monkeypatch) -> None:
    package = {
        "allowed_exact_paths": ["n0te2/foo.py"],
        "allowed_prefixes": [],
        "acceptance": {"authority_through_sha": "2" * 40},
    }
    monkeypatch.setattr(program, "git", lambda _repo, *args: "3" * 40)
    monkeypatch.setattr(program, "_is_ancestor", lambda _repo, older, newer: False)
    assert program._historical_path_covered(
        Path("."),
        path="n0te2/foo.py",
        baseline="1" * 40,
        head="4" * 40,
        mutable_packages=[],
        historical_packages=[package],
    ) is False


def test_historical_authority_covers_change_through_checkpoint(monkeypatch) -> None:
    package = {
        "allowed_exact_paths": ["n0te2/foo.py"],
        "allowed_prefixes": [],
        "acceptance": {"authority_through_sha": "2" * 40},
    }
    monkeypatch.setattr(program, "git", lambda _repo, *args: "2" * 40)
    monkeypatch.setattr(program, "_is_ancestor", lambda _repo, older, newer: True)
    assert program._historical_path_covered(
        Path("."),
        path="n0te2/foo.py",
        baseline="1" * 40,
        head="4" * 40,
        mutable_packages=[],
        historical_packages=[package],
    ) is True


def test_active_successor_can_cover_path_after_historical_freeze(monkeypatch) -> None:
    historical = {
        "allowed_exact_paths": ["n0te2/foo.py"],
        "allowed_prefixes": [],
        "acceptance": {"authority_through_sha": "2" * 40},
    }
    active = {"allowed_exact_paths": ["n0te2/foo.py"], "allowed_prefixes": []}

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("active authority should cover before historical lookup")

    monkeypatch.setattr(program, "git", unexpected_git)
    assert program._historical_path_covered(
        Path("."),
        path="n0te2/foo.py",
        baseline="1" * 40,
        head="4" * 40,
        mutable_packages=[active],
        historical_packages=[historical],
    ) is True


def test_real_work_pass_labels_without_evidence_refs_are_rejected() -> None:
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
            }
        )
        write_json(path, receipt)
        with pytest.raises(program.ProgramGovernanceError, match="requires implementation_refs"):
            program.check_program(repo, verify_git=False)
    finally:
        td.cleanup()


def test_fully_evidenced_non_risky_real_work_can_be_promoted() -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        path = repo / "governance/active_receipt.json"
        receipt = json.loads(path.read_text())
        _fully_evidenced_real_work(product_package(receipt))
        write_json(path, receipt)
        result = program.check_program(repo, verify_git=False)
        assert "INTEL-171-TEST" in result["active_packages"]
    finally:
        td.cleanup()
