from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "governance" / "check_steward_integrity.py"
spec = importlib.util.spec_from_file_location("check_steward_integrity", MODULE_PATH)
assert spec is not None and spec.loader is not None
integrity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integrity)


def test_repository_cross_ledger_integrity() -> None:
    integrity.run(ROOT, verify_git=False)


def test_repository_cross_ledger_integrity_against_active_exact_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = json.loads(
        (ROOT / "governance" / "active_receipt.json").read_text(encoding="utf-8")
    )
    if receipt.get("status") != "ACTIVE":
        pytest.skip("exact-base candidate proof applies only while a receipt is ACTIVE")
    base_sha = receipt.get("baseline_sha")
    assert isinstance(base_sha, str) and integrity.HEX40.fullmatch(base_sha)
    monkeypatch.setenv("N0TE2_BASE_SHA", base_sha)
    integrity.run(ROOT, verify_git=True)


def _requirements() -> dict:
    return {
        "sequence": {"start": 2, "end": 4},
        "canonical_scope": {"start": 2, "end": 4, "retained_requirement_count": 3},
        "canonical_extensions": [],
        "held_or_boundary": ["REQ-SCOPE-004"],
        "superseded": [],
    }


def _graph() -> dict:
    return {
        "nodes": [
            {"id": "CORE", "state": "PRESERVED", "requirements": "002-003", "depends_on": [], "any_of": []},
            {"id": "LATER-01", "state": "PRESERVED", "requirements": "004", "depends_on": [], "any_of": []},
        ]
    }


def _equivalence(original: str = "REQ-SCOPE-002") -> dict:
    return {
        "original_requirement": original,
        "replacement_implementation": "impl://replacement",
        "semantic_coverage_mapping": {original: ["impl://replacement"]},
        "retained_behavior": ["A"],
        "changed_behavior": [],
        "uncovered_residue": [],
        "acceptance_evidence": ["evidence://1"],
        "semantic_authority": "USER_ROOT",
        "lineage": ["receipt://old", "receipt://new"],
        "successor_status": "INTEGRATED",
    }


def test_held_requirement_cannot_silently_disappear() -> None:
    graph = _graph()
    graph["nodes"][1]["requirements"] = ""
    with pytest.raises(integrity.StewardIntegrityError, match="held_or_boundary and LATER-01 diverged"):
        integrity.validate_requirement_graph(_requirements(), graph)


def test_graph_requirement_cannot_escape_canonical_scope() -> None:
    graph = _graph()
    graph["nodes"][0]["requirements"] = "002,999"
    with pytest.raises(integrity.StewardIntegrityError, match="outside canonical scope"):
        integrity.validate_requirement_graph(_requirements(), graph)


def test_build_sequence_requirement_cannot_silently_disappear() -> None:
    graph = _graph()
    graph["nodes"][0]["requirements"] = "002"
    with pytest.raises(
        integrity.StewardIntegrityError,
        match="completion graph lost build-sequence requirements: REQ-SCOPE-003",
    ):
        integrity.validate_requirement_graph(_requirements(), graph)


def test_superseded_build_sequence_requirement_is_not_forced_back_into_graph() -> None:
    requirements = _requirements()
    requirements["superseded"] = ["REQ-SCOPE-003"]
    graph = _graph()
    graph["nodes"][0]["requirements"] = "002"
    integrity.validate_requirement_graph(requirements, graph)


def test_canonical_extension_is_not_forced_into_build_graph() -> None:
    requirements = _requirements()
    requirements["canonical_scope"] = {"start": 2, "end": 5, "retained_requirement_count": 4}
    requirements["canonical_extensions"] = [
        {"id": "REQ-SCOPE-005", "state": "MAPPED", "selected": False, "construction_affinity": ["CORE"]}
    ]
    integrity.validate_requirement_graph(requirements, _graph())


def test_superseded_decision_requires_successor() -> None:
    with pytest.raises(integrity.StewardIntegrityError, match="lacks durable successor"):
        integrity.validate_decisions([{"id": "DEC-1", "status": "SUPERSEDED", "supersedes": []}])


def test_decision_supersession_cycle_is_rejected() -> None:
    rows = [
        {"id": "DEC-1", "status": "ACTIVE", "supersedes": ["DEC-2"]},
        {"id": "DEC-2", "status": "ACTIVE", "supersedes": ["DEC-1"]},
    ]
    with pytest.raises(integrity.StewardIntegrityError, match="cycle"):
        integrity.validate_decisions(rows)


def test_exact_base_decision_history_cannot_be_deleted() -> None:
    base = [
        {"id": "DEC-1", "status": "ACTIVE", "supersedes": []},
        {"id": "DEC-2", "status": "ACTIVE", "supersedes": []},
    ]
    with pytest.raises(integrity.StewardIntegrityError, match="deleted exact-base history"):
        integrity.validate_decision_history_against_base(base[:1], base)


def test_exact_base_decision_history_cannot_be_rewritten_or_reordered() -> None:
    base = [
        {"id": "DEC-1", "status": "ACTIVE", "supersedes": []},
        {"id": "DEC-2", "status": "ACTIVE", "supersedes": []},
    ]
    rewritten = [dict(base[0]), dict(base[1])]
    rewritten[0]["status"] = "SUPERSEDED"
    with pytest.raises(integrity.StewardIntegrityError, match="mutated/reordered exact-base row"):
        integrity.validate_decision_history_against_base(rewritten, base)


def test_exact_base_decision_history_allows_append_only_successor() -> None:
    base = [{"id": "DEC-1", "status": "ACTIVE", "supersedes": []}]
    candidate = base + [{"id": "DEC-2", "status": "ACTIVE", "supersedes": ["DEC-1"]}]
    integrity.validate_decision_history_against_base(candidate, base)


def _receipt() -> dict:
    return {
        "status": "ACTIVE",
        "node_id": "INCIDENT-REPAIR",
        "increment_id": "INCIDENT-REPAIR-TEST",
        "baseline_sha": "a" * 40,
        "lineage": {"requirements": []},
        "incident_repair_ids": ["INC-1"],
    }


def _current() -> dict:
    return {"lifecycle_state": "ACTIVE", "active_node": "INCIDENT-REPAIR", "active_increment": "INCIDENT-REPAIR-TEST"}


def _incidents() -> dict:
    return {
        "INC-1": {
            "id": "INC-1",
            "status": "OPEN_REPAIRING",
            "repair_contract": {"future_receipt_field": "incident_repair_ids"},
        }
    }


def test_public_handoff_obligation_cannot_be_orphaned() -> None:
    receipt = _receipt()
    receipt["public_handoff_required"] = True
    with pytest.raises(integrity.StewardIntegrityError, match="no durable public_handoff_ref"):
        integrity.validate_receipt_and_current_state(_current(), receipt, {"REQ-SCOPE-002"}, set(), {}, _incidents())


def test_merge_receipt_cannot_claim_public_verified() -> None:
    receipt = _receipt()
    receipt["public_acceptance_state"] = "PUBLIC_VERIFIED"
    with pytest.raises(integrity.StewardIntegrityError, match="cannot create PUBLIC_VERIFIED"):
        integrity.validate_receipt_and_current_state(_current(), receipt, {"REQ-SCOPE-002"}, set(), {}, _incidents())


def test_equivalence_requires_semantic_proof() -> None:
    receipt = _receipt()
    receipt["current_disposition"] = "SUPERSEDED_BY_EQUIVALENT"
    with pytest.raises(integrity.StewardIntegrityError, match="requires equivalence_receipt"):
        integrity.validate_receipt_and_current_state(_current(), receipt, {"REQ-SCOPE-002"}, set(), {}, _incidents())


def test_equivalence_receipt_with_complete_lineage_passes() -> None:
    integrity.validate_equivalence_receipt(_equivalence(), {"REQ-SCOPE-002"})


def test_equivalence_requires_acceptance_evidence() -> None:
    receipt = _equivalence()
    receipt["acceptance_evidence"] = []
    with pytest.raises(integrity.StewardIntegrityError, match="acceptance_evidence must not be empty"):
        integrity.validate_equivalence_receipt(receipt, {"REQ-SCOPE-002"})


def test_equivalence_mapping_must_cover_original_requirement() -> None:
    receipt = _equivalence()
    receipt["semantic_coverage_mapping"] = {"REQ-SCOPE-003": ["impl://replacement"]}
    with pytest.raises(integrity.StewardIntegrityError, match="must cover original_requirement"):
        integrity.validate_equivalence_receipt(receipt, {"REQ-SCOPE-002", "REQ-SCOPE-003"})


def test_equivalence_cannot_hide_uncovered_residue() -> None:
    receipt = _equivalence()
    receipt["uncovered_residue"] = ["missing behavior"]
    with pytest.raises(integrity.StewardIntegrityError, match="cannot retain uncovered_residue"):
        integrity.validate_equivalence_receipt(receipt, {"REQ-SCOPE-002"})


def test_every_superseded_lineage_requirement_needs_equivalence_evidence() -> None:
    receipt = _receipt()
    receipt["lineage"] = {"requirements": ["REQ-SCOPE-002", "REQ-SCOPE-003"]}
    receipt["equivalence_receipts"] = [_equivalence("REQ-SCOPE-002")]
    with pytest.raises(integrity.StewardIntegrityError, match="superseded receipt lineage lacks equivalence evidence: REQ-SCOPE-003"):
        integrity.validate_receipt_and_current_state(
            _current(), receipt, {"REQ-SCOPE-002", "REQ-SCOPE-003"},
            {"REQ-SCOPE-002", "REQ-SCOPE-003"}, {}, _incidents(),
        )


def test_contract_cannot_grant_scope_authority() -> None:
    contract = json.loads((ROOT / "governance" / "steward_integrity_contract.json").read_text(encoding="utf-8"))
    contract["authority_boundary"]["can_select_product_scope"] = True
    with pytest.raises(integrity.StewardIntegrityError, match="cannot grant authority: can_select_product_scope"):
        integrity.validate_contract(contract)


def test_continuity_acceptance_cannot_drop_exact_base_item() -> None:
    base = {
        "acceptance_items": [
            {"id": "CONT-001", "name": "One", "state": "RECONCILE", "requirement": "Keep one"},
            {"id": "CONT-002", "name": "Two", "state": "RECONCILE", "requirement": "Keep two"},
        ]
    }
    candidate = {"acceptance_items": [dict(base["acceptance_items"][0])]}
    with pytest.raises(integrity.StewardIntegrityError, match="lost exact-base items: CONT-002"):
        integrity.validate_continuity_acceptance_against_base(candidate, base)


def test_continuity_acceptance_cannot_rewrite_exact_base_requirement() -> None:
    base = {"acceptance_items": [{"id": "CONT-001", "name": "One", "state": "RECONCILE", "requirement": "Keep one"}]}
    candidate = {"acceptance_items": [{"id": "CONT-001", "name": "One", "state": "IMPLEMENTED", "requirement": "Changed"}]}
    with pytest.raises(integrity.StewardIntegrityError, match="mutated exact-base requirement"):
        integrity.validate_continuity_acceptance_against_base(candidate, base)


def test_continuity_acceptance_allows_state_progress_without_lineage_rewrite() -> None:
    base = {"acceptance_items": [{"id": "CONT-001", "name": "One", "state": "RECONCILE", "requirement": "Keep one"}]}
    candidate = {"acceptance_items": [{"id": "CONT-001", "name": "One", "state": "IMPLEMENTED", "requirement": "Keep one"}]}
    integrity.validate_continuity_acceptance_against_base(candidate, base)


def test_action_receipt_cannot_drop_exact_base_required_field() -> None:
    base = {
        "required_request_fields": ["operation_id", "approval_state"],
        "required_result_fields": ["operation_id", "retry_safe"],
        "authority_rules": {"stale_receipt_authorizes_new_work": False},
    }
    candidate = {
        "required_request_fields": ["operation_id"],
        "required_result_fields": ["operation_id", "retry_safe"],
        "authority_rules": {"stale_receipt_authorizes_new_work": False},
    }
    with pytest.raises(integrity.StewardIntegrityError, match="lost exact-base required_request_fields: approval_state"):
        integrity.validate_action_contract_against_base(candidate, base)


def test_action_receipt_exact_base_authority_rule_cannot_drift() -> None:
    base = {
        "required_request_fields": ["operation_id"],
        "required_result_fields": ["operation_id"],
        "authority_rules": {"stale_receipt_authorizes_new_work": False},
    }
    candidate = {
        "required_request_fields": ["operation_id"],
        "required_result_fields": ["operation_id"],
        "authority_rules": {"stale_receipt_authorizes_new_work": True},
    }
    with pytest.raises(integrity.StewardIntegrityError, match="authority rule drifted"):
        integrity.validate_action_contract_against_base(candidate, base)


def test_work_continuity_cannot_drop_exact_base_checkpoint_field() -> None:
    base = {"required_checkpoint_fields": ["canonical_work_id", "owner"], "hard_rules": ["Keep work"]}
    candidate = {"required_checkpoint_fields": ["canonical_work_id"], "hard_rules": ["Keep work"]}
    with pytest.raises(integrity.StewardIntegrityError, match="lost exact-base checkpoint fields: owner"):
        integrity.validate_work_continuity_against_base(candidate, base)


def test_work_continuity_cannot_drop_exact_base_hard_rule() -> None:
    base = {"required_checkpoint_fields": ["canonical_work_id"], "hard_rules": ["Keep work", "Resume work"]}
    candidate = {"required_checkpoint_fields": ["canonical_work_id"], "hard_rules": ["Keep work"]}
    with pytest.raises(integrity.StewardIntegrityError, match="lost exact-base hard rules"):
        integrity.validate_work_continuity_against_base(candidate, base)


def test_resolved_incident_cannot_authorize_active_repair() -> None:
    incidents = _incidents()
    incidents["INC-1"]["status"] = "RESOLVED"
    with pytest.raises(integrity.StewardIntegrityError, match="non-open incident"):
        integrity.validate_receipt_and_current_state(_current(), _receipt(), {"REQ-SCOPE-002"}, set(), {}, incidents)


def test_active_receipt_must_bind_exact_current_increment() -> None:
    current = _current()
    current["active_increment"] = "OTHER"
    with pytest.raises(integrity.StewardIntegrityError, match="increment diverges"):
        integrity.validate_receipt_and_current_state(current, _receipt(), {"REQ-SCOPE-002"}, set(), {}, _incidents())


def test_explicit_base_lookup_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    requirements = _requirements()
    receipt = _receipt()
    def explode(*args: str) -> str:
        raise RuntimeError("base unavailable")
    monkeypatch.setattr(integrity, "git", explode)
    with pytest.raises(integrity.StewardIntegrityError, match="cannot inspect exact base requirements"):
        integrity.validate_new_supersessions(tmp_path, "b" * 40, requirements, receipt, {"REQ-SCOPE-002", "REQ-SCOPE-003", "REQ-SCOPE-004"})


def test_new_supersession_requires_candidate_equivalence_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    requirements = _requirements()
    requirements["superseded"] = ["REQ-SCOPE-002"]
    base_requirements = _requirements()
    monkeypatch.setattr(integrity, "git", lambda *args: json.dumps(base_requirements))
    with pytest.raises(integrity.StewardIntegrityError, match="equivalence_receipts"):
        integrity.validate_new_supersessions(tmp_path, "c" * 40, requirements, _receipt(), {"REQ-SCOPE-002", "REQ-SCOPE-003", "REQ-SCOPE-004"})
