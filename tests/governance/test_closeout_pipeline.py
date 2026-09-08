import json
from pathlib import Path

import pytest

from governance.closeout import CloseoutError, apply_closeout, extract_request
from governance.model import EVIDENCE_DIMENSIONS


def cell(state="UNPROVEN", refs=()):
    return {"state": state, "evidence_refs": list(refs)}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def fixture_root(tmp_path: Path):
    receipt_ref = "governance/evidence/REQ-SCOPE-090-before.json"
    old_claims = {dim: cell() for dim in EVIDENCE_DIMENSIONS}
    old_claims["MAPPED"] = cell(
        "PROVEN", ["N0TE_PRODUCT_DB/SCOPE_LEDGER:REQ-SCOPE-090"]
    )
    old_claims["RECOVERABLE"] = cell("PROVEN", ["prior:recovery-proof"])
    write_json(
        tmp_path / receipt_ref,
        {
            "schema_version": 1,
            "kind": "REQUIREMENT_EVIDENCE_RECEIPT",
            "requirement_id": "REQ-SCOPE-090",
            "semantic_owner": "N0TE_PRODUCT_DB/SCOPE_LEDGER",
            "claims": old_claims,
        },
    )

    package_evidence = {dim: cell() for dim in EVIDENCE_DIMENSIONS}
    package_evidence["MAPPED"] = cell(
        "PROVEN", ["N0TE_PRODUCT_DB/SCOPE_LEDGER:REQ-SCOPE-090"]
    )
    program = {
        "schema_version": 1,
        "program_id": "PROGRAM-X",
        "state": "ACTIVE",
        "authority_source": "test",
        "allowed_paths": [
            "n0te/consumer_shell.py",
            "tests/acceptance/test_produce.py",
        ],
        "allowed_requirement_ids": ["REQ-SCOPE-090"],
        "allowed_authority_classes": ["REVERSIBLE_WRITE"],
        "work_packages": [
            {
                "work_id": "WP-X",
                "parent_program_id": "PROGRAM-X",
                "state": "ACTIVE",
                "paths": [
                    "n0te/consumer_shell.py",
                    "tests/acceptance/test_produce.py",
                ],
                "requirement_ids": ["REQ-SCOPE-090"],
                "authority_class": "REVERSIBLE_WRITE",
                "dependencies": [],
                "mechanical_closeout_allowlist": [
                    "IMPLEMENTED",
                    "INTEGRATED",
                    "REACHABLE",
                    "VERIFIED",
                    "AUTHORITY_SAFE",
                ],
                "evidence": package_evidence,
            }
        ],
    }
    write_json(
        tmp_path / "governance/programs/PROGRAM-X.json",
        program,
    )
    write_json(
        tmp_path / "governance/current_state.json",
        {
            "schema_version": 1,
            "active_construction_program": "governance/programs/PROGRAM-X.json",
            "current_requirement_evidence": {"REQ-SCOPE-090": receipt_ref},
        },
    )
    return tmp_path


def valid_request():
    return {
        "schema_version": 1,
        "program_id": "PROGRAM-X",
        "work_id": "WP-X",
        "complete": True,
        "claims": {
            "IMPLEMENTED": {
                "state": "PROVEN",
                "evidence_refs": ["n0te/consumer_shell.py"],
            },
            "INTEGRATED": {
                "state": "PROVEN",
                "evidence_refs": ["n0te/consumer_shell.py"],
            },
            "REACHABLE": {
                "state": "PROVEN",
                "evidence_refs": ["tests/acceptance/test_produce.py"],
                "observed_result": "Normal Produce path exercised the bounded work.",
            },
            "VERIFIED": {
                "state": "PROVEN",
                "evidence_refs": ["tests/acceptance/test_produce.py"],
            },
            "AUTHORITY_SAFE": {
                "state": "PROVEN",
                "evidence_refs": ["tests/acceptance/test_produce.py"],
            },
        },
    }


def test_extract_request_requires_explicit_machine_marker():
    body = """Text before.
<!-- N0TE_CLOSEOUT
{"schema_version":1,"program_id":"P","work_id":"W","complete":true,"claims":{"VERIFIED":{"state":"PROVEN","evidence_refs":[]}}}
N0TE_CLOSEOUT -->
Text after."""
    request = extract_request(body)
    assert request["program_id"] == "P"
    with pytest.raises(CloseoutError):
        extract_request("no marker")


def test_mechanical_closeout_promotes_only_allowlisted_dimensions_and_preserves_prior_truth(tmp_path):
    root = fixture_root(tmp_path)
    changed = apply_closeout(
        root,
        valid_request(),
        main_sha="a" * 40,
        ci_run=12345,
        observed_at="2026-09-08T20:00:00Z",
    )

    state = json.loads((root / "governance/current_state.json").read_text())
    assert "active_construction_program" not in state
    assert state["last_completed_construction_program"]["program_id"] == "PROGRAM-X"

    program = json.loads(
        (root / "governance/programs/PROGRAM-X.json").read_text()
    )
    package = program["work_packages"][0]
    assert package["state"] == "COMPLETE"
    assert program["state"] == "COMPLETE"
    assert package["evidence"]["REACHABLE"]["state"] == "PROVEN"
    assert package["evidence"]["CONSUMER_ACCEPTED"]["state"] == "UNPROVEN"
    assert package["evidence"]["VALUE_EVIDENCED"]["state"] == "UNPROVEN"

    receipt_path = root / state["current_requirement_evidence"]["REQ-SCOPE-090"]
    receipt = json.loads(receipt_path.read_text())
    assert receipt["claims"]["RECOVERABLE"]["state"] == "PROVEN"
    assert "prior:recovery-proof" in receipt["claims"]["RECOVERABLE"]["evidence_refs"]
    assert receipt["claims"]["REACHABLE"]["state"] == "PROVEN"
    assert f"main:{'a' * 40}" in receipt["claims"]["REACHABLE"]["evidence_refs"]
    assert "ci-run:12345" in receipt["claims"]["REACHABLE"]["evidence_refs"]
    assert receipt["claims"]["CONSUMER_ACCEPTED"]["state"] == "UNPROVEN"
    assert receipt["claims"]["VALUE_EVIDENCED"]["state"] == "UNPROVEN"
    assert any(path.endswith("completion-" + "a" * 12 + ".json") for path in changed)


def test_mechanical_closeout_never_promotes_consumer_acceptance_or_value(tmp_path):
    root = fixture_root(tmp_path)
    request = valid_request()
    request["claims"]["CONSUMER_ACCEPTED"] = {
        "state": "PROVEN",
        "evidence_refs": ["tests/acceptance/test_produce.py"],
    }
    with pytest.raises(CloseoutError, match="cannot prove"):
        apply_closeout(root, request, main_sha="b" * 40, ci_run=10)


def test_mechanical_closeout_rejects_evidence_outside_package_paths(tmp_path):
    root = fixture_root(tmp_path)
    request = valid_request()
    request["claims"]["REACHABLE"]["evidence_refs"] = ["n0te/transactions.py"]
    with pytest.raises(CloseoutError, match="outside work-package paths"):
        apply_closeout(root, request, main_sha="c" * 40, ci_run=11)


def test_mechanical_closeout_requires_predeclared_dimension_allowlist(tmp_path):
    root = fixture_root(tmp_path)
    program_path = root / "governance/programs/PROGRAM-X.json"
    program = json.loads(program_path.read_text())
    program["work_packages"][0]["mechanical_closeout_allowlist"].remove("REACHABLE")
    write_json(program_path, program)
    with pytest.raises(CloseoutError, match="exceed mechanical allowlist"):
        apply_closeout(root, valid_request(), main_sha="d" * 40, ci_run=12)
