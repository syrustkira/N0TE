import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(path):
    return json.loads((ROOT / path).read_text())


def test_activation_state_routes_current_work_through_bounded_construction_program_governance():
    state = load("governance/current_state.json")
    program = state["construction_program_governance"]
    assert program["state"] == "ACTIVE"
    assert program["requirement_id"] == "REQ-SCOPE-172"
    assert program["multiple_eligible_sibling_packages"] is True
    assert program["implementation_acceptance_separate"] is True
    assert program["activation_receipt"] == (
        "migration/receipts/constitutional-req-scope-172-2026-09-08.json"
    )
    assert "every dependency-valid" in state["work_selection_rule"]


def test_req_scope_172_temporary_authority_remains_expired_after_later_constitutional_migrations():
    state = load("governance/current_state.json")
    assert state["construction_program_governance"]["activation_receipt"] == (
        "migration/receipts/constitutional-req-scope-172-2026-09-08.json"
    )

    receipt = load(
        "migration/receipts/constitutional-req-scope-172-2026-09-08.json"
    )
    temporary = receipt["temporary_migration_authority"]
    assert temporary["state_after_activation_condition_is_satisfied"] == "EXPIRED"
    assert temporary["permanent_bypass"] is False

    authority = load("governance/authority.json")
    pending = authority.get("pending_constitutional_migration")
    if pending is not None:
        assert pending.get("migration_id") != "CONST-REQ-SCOPE-172-2026-09-08"


def test_activation_receipt_is_conditioned_on_verified_main_state():
    receipt = load(
        "migration/receipts/constitutional-req-scope-172-2026-09-08.json"
    )
    assert receipt["candidate"]["ci_result"] == "PASS"
    assert receipt["implementation_merge"]["post_merge_ci_result"] == "PASS"
    assert receipt["constitutional_sequence"]["activation"] == (
        "CONDITIONAL_ON_MERGE_TO_MAIN_AND_RESULTING_MAIN_HEAD_CI_PASS"
    )
    assert receipt["temporary_migration_authority"][
        "state_after_activation_condition_is_satisfied"
    ] == "EXPIRED"
    assert receipt["temporary_migration_authority"]["permanent_bypass"] is False


def test_requirement_evidence_promotes_only_dimensions_actually_proven():
    evidence = load(
        "governance/evidence/REQ-SCOPE-172-constitutional-activation-2026-09-08.json"
    )
    claims = evidence["claims"]
    assert claims["MAPPED"]["state"] == "PROVEN"
    assert claims["IMPLEMENTED"]["state"] == "PROVEN"
    assert claims["INTEGRATED"]["state"] == "PROVEN"
    assert claims["VERIFIED"]["state"] == "PROVEN"
    assert claims["AUTHORITY_SAFE"]["state"] == "PROVEN"
    for dimension in (
        "REACHABLE",
        "RECOVERABLE",
        "CONSUMER_ACCEPTED",
        "VALUE_EVIDENCED",
    ):
        assert claims[dimension]["state"] == "UNPROVEN"
