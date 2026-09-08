import json
from pathlib import Path

from governance.check_governance import (
    evaluate_current_construction_program,
    load_current_requirement_evidence,
)
from governance.model import evaluate_construction_program, validate_construction_program

ROOT = Path(__file__).resolve().parents[2]


def load(path: str):
    return json.loads((ROOT / path).read_text())


def test_first_real_program_is_durably_complete_and_no_longer_current():
    current = evaluate_current_construction_program()
    if current is not None:
        current_program, _ = current
        assert current_program["program_id"] != "PROGRAM-PERSONAL-PRODUCTION-001"

    program = load("governance/programs/PROGRAM-PERSONAL-PRODUCTION-001.json")
    assert validate_construction_program(program)
    assert program["state"] == "COMPLETE"
    assert program["completion_receipt"] == (
        "governance/evidence/PROGRAM-PERSONAL-PRODUCTION-001-completion-2026-09-08.json"
    )
    receipt = load(program["completion_receipt"])
    execution = receipt["lifecycle"]["sibling_execution"]
    assert execution["main_commit"] == "c0cb39fe47779f7a36f6bec877c4861ab73776cf"
    assert execution["resulting_main_ci_run"] == 34261064328
    assert execution["resulting_main_ci_result"] == "PASS"

    assert [package["state"] for package in program["work_packages"]] == [
        "COMPLETE",
        "COMPLETE",
    ]
    result = evaluate_construction_program(program)
    assert result["eligible_work_package_ids"] == []
    assert result["blocked_work_packages"] == {}

    paths = [set(package["paths"]) for package in program["work_packages"]]
    assert paths[0].isdisjoint(paths[1])


def test_completed_children_preserve_evidence_dimensions_instead_of_collapsing_acceptance():
    program = load("governance/programs/PROGRAM-PERSONAL-PRODUCTION-001.json")
    by_id = {package["work_id"]: package for package in program["work_packages"]}

    headquarters = by_id["WP-003-HQ-REACHABILITY"]["evidence"]
    for dimension in ("MAPPED", "IMPLEMENTED", "INTEGRATED", "REACHABLE", "VERIFIED"):
        assert headquarters[dimension]["state"] == "PROVEN"
    for dimension in (
        "RECOVERABLE",
        "AUTHORITY_SAFE",
        "CONSUMER_ACCEPTED",
        "VALUE_EVIDENCED",
    ):
        assert headquarters[dimension]["state"] == "UNPROVEN"

    compare = by_id["WP-010-COMPARE-DECIDE-REACHABILITY"]["evidence"]
    for dimension in (
        "MAPPED",
        "IMPLEMENTED",
        "INTEGRATED",
        "REACHABLE",
        "VERIFIED",
        "AUTHORITY_SAFE",
    ):
        assert compare[dimension]["state"] == "PROVEN"
    for dimension in ("RECOVERABLE", "CONSUMER_ACCEPTED", "VALUE_EVIDENCED"):
        assert compare[dimension]["state"] == "UNPROVEN"


def test_current_requirement_receipts_are_executable_and_keep_parent_acceptance_unproven():
    manifest = load("governance/canonical_scope_manifest.json")
    current = load_current_requirement_evidence(manifest["retained_requirement_ids"])
    assert set(current) == {
        "REQ-SCOPE-003",
        "REQ-SCOPE-010",
        "REQ-SCOPE-079",
        "REQ-SCOPE-090",
        "REQ-SCOPE-172",
    }

    assert current["REQ-SCOPE-003"]["claims"]["REACHABLE"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-010"]["claims"]["REACHABLE"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-010"]["claims"]["AUTHORITY_SAFE"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-079"]["claims"]["VERIFIED"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-079"]["claims"]["AUTHORITY_SAFE"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-079"]["claims"]["REACHABLE"]["state"] == "UNPROVEN"
    assert current["REQ-SCOPE-090"]["claims"]["VERIFIED"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-090"]["claims"]["RECOVERABLE"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-090"]["claims"]["AUTHORITY_SAFE"]["state"] == "PROVEN"
    assert current["REQ-SCOPE-090"]["claims"]["REACHABLE"]["state"] == "UNPROVEN"
    assert current["REQ-SCOPE-172"]["claims"]["REACHABLE"]["state"] == "PROVEN"

    for receipt in current.values():
        assert receipt["claims"]["CONSUMER_ACCEPTED"]["state"] == "UNPROVEN"
        assert receipt["claims"]["VALUE_EVIDENCED"]["state"] == "UNPROVEN"
