from __future__ import annotations

import importlib.util
import json

import pytest

from tests.governance.test_program_governance import activate_synthetic_program, clone_repo, write_json

ROOT_SPEC = importlib.util.spec_from_file_location(
    "n0te2_runtime_handoff",
    __import__("pathlib").Path(__file__).resolve().parents[2] / "governance/build_handoff.py",
)
handoff = importlib.util.module_from_spec(ROOT_SPEC)
ROOT_SPEC.loader.exec_module(handoff)


def test_parent_program_boundaries_are_emitted_in_runtime_handoff(monkeypatch) -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        monkeypatch.setattr(handoff, "git", lambda _repo, *args: "1" * 40 if args[:2] == ("rev-parse", "HEAD") else "")
        runtime = handoff.build_runtime_handoff(repo)
        assert runtime["lifecycle"]["mode"] == "PARENT_PROGRAM"
        assert runtime["lifecycle"]["active_program"] == "PP-TEST-001"
        assert runtime["parent_program_validation"]["status"] == "ACTIVE"
        program = runtime["parent_program"]
        assert program["program_id"] == "PP-TEST-001"
        assert set(program["active_package_ids"]) == {"GOV-172-TEST", "INTEL-171-TEST"}
        by_id = {row["package_id"]: row for row in program["package_boundaries"]}
        assert by_id["INTEL-171-TEST"]["allowed_exact_paths"] == ["n0te2/intelligence.py"]
        assert by_id["INTEL-171-TEST"]["depends_on_packages"] == []
        assert by_id["INTEL-171-TEST"]["requirement_ids"] == ["REQ-SCOPE-171"]
        assert by_id["INTEL-171-TEST"]["stateful_or_mutating"] is False
        assert by_id["INTEL-171-TEST"]["external_consequence"] is False
    finally:
        td.cleanup()


def test_runtime_handoff_rejects_invalid_parent_program_before_emitting_boundaries(monkeypatch) -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        receipt_path = repo / "governance/active_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        by_id = {row["package_id"]: row for row in receipt["work_packages"]}
        by_id["GOV-172-TEST"]["depends_on_packages"] = ["INTEL-171-TEST"]
        by_id["INTEL-171-TEST"]["depends_on_packages"] = ["GOV-172-TEST"]
        write_json(receipt_path, receipt)
        monkeypatch.setattr(handoff, "git", lambda _repo, *args: "1" * 40 if args[:2] == ("rev-parse", "HEAD") else "")
        with pytest.raises(handoff.HandoffError, match="parent-program authority is invalid"):
            handoff.build_runtime_handoff(repo)
    finally:
        td.cleanup()


def test_runtime_handoff_rejects_stale_parent_program_compatibility_fields(monkeypatch) -> None:
    td, repo = clone_repo()
    try:
        activate_synthetic_program(repo)
        handoff_path = repo / "governance/handoff.json"
        durable_handoff = json.loads(handoff_path.read_text())
        durable_handoff.setdefault("lifecycle", {})["active_program"] = "STALE-PROGRAM"
        write_json(handoff_path, durable_handoff)
        monkeypatch.setattr(handoff, "git", lambda _repo, *args: "1" * 40 if args[:2] == ("rev-parse", "HEAD") else "")
        with pytest.raises(handoff.HandoffError, match="handoff active program is stale"):
            handoff.build_runtime_handoff(repo)
    finally:
        td.cleanup()
