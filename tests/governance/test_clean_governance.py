import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from governance.model import (
    EVIDENCE_DIMENSIONS, claim_state, derive_parent_state, evidence_is_fresh,
    expire_bootstrap_authority, legacy_veto_blocks_authorized_migration,
    reopen_from_contradiction, scoped_acceptance, select_next_work,
    validate_constitutional_migration, validate_discovery_disposition,
    validate_requirement_evidence, validate_scope_projection,
)

ROOT = Path(__file__).resolve().parents[2]
def data(name): return json.loads((ROOT / "governance" / name).read_text())
def proven_record(dim="MAPPED"):
    return {d: {"state": "PROVEN" if d == dim else "UNPROVEN", "evidence_refs": []} for d in EVIDENCE_DIMENSIONS}


def test_semantic_projection_includes_current_drive_scope_171_and_172():
    m = data("canonical_scope_manifest.json")
    assert m["retained_requirement_ids"][-2:] == ["REQ-SCOPE-171", "REQ-SCOPE-172"]
    assert validate_scope_projection(m["retained_requirement_ids"], m["retained_requirement_ids"]) == "REQ-SCOPE-172"


def test_future_scope_mismatch_fails_visibly_without_hardcoded_max_logic():
    m = data("canonical_scope_manifest.json")
    future = m["retained_requirement_ids"] + ["REQ-SCOPE-173"]
    with pytest.raises(ValueError, match="REQ-SCOPE-173"):
        validate_scope_projection(future, m["retained_requirement_ids"])


def test_every_requirement_has_every_non_collapsing_evidence_dimension():
    m, e = data("canonical_scope_manifest.json"), data("evidence_state.json")
    assert validate_requirement_evidence(m["retained_requirement_ids"], e)
    for rid in m["retained_requirement_ids"]:
        assert set(e["requirements"][rid]) == set(EVIDENCE_DIMENSIONS)


def test_construction_or_generic_done_never_becomes_acceptance():
    r = proven_record()
    r["CONSUMER_ACCEPTED"] = {"state": "DONE", "evidence_refs": ["legacy construction graph"]}
    assert claim_state(r, "CONSUMER_ACCEPTED") == "UNPROVEN"


def test_missing_evidence_remains_unproven():
    assert claim_state({}, "VERIFIED") == "UNPROVEN"


def test_parent_rollup_requires_every_required_sibling():
    a, b = proven_record("CONSUMER_ACCEPTED"), proven_record()
    children = [
        {"class": "REQUIRED", "evidence": a},
        {"class": "REQUIRED", "evidence": b},
        {"class": "HOST_OPTIONAL", "evidence": b},
    ]
    assert derive_parent_state(children) == "UNPROVEN"
    b["CONSUMER_ACCEPTED"]["state"] = "PROVEN"
    assert derive_parent_state(children) == "PROVEN"


def test_scoped_acceptance_cannot_globalize_itself():
    good, bad = proven_record("CONSUMER_ACCEPTED"), proven_record()
    rows = [
        {"scope_id": "linux-ableton", "class": "REQUIRED", "evidence": good},
        {"scope_id": "windows-ableton", "class": "REQUIRED", "evidence": bad},
    ]
    assert scoped_acceptance(rows, "linux-ableton") == "PROVEN"
    assert derive_parent_state(rows) == "UNPROVEN"


def test_contradictory_observation_reopens_current_and_downstream_claims():
    r = {d: {"state": "PROVEN", "evidence_refs": ["old"]} for d in EVIDENCE_DIMENSIONS}
    candidate = reopen_from_contradiction(r, "REACHABLE", {"source": "user", "claim": "cannot reach Services"})
    assert r["IMPLEMENTED"]["state"] == "PROVEN"
    start = EVIDENCE_DIMENSIONS.index("REACHABLE")
    assert all(r[d]["state"] == "UNPROVEN" for d in EVIDENCE_DIMENSIONS[start:])
    assert candidate["kind"] == "BOUNDED_REPAIR_CANDIDATE"
    assert candidate["authority_granted"] is False


def test_stale_evidence_invalidates():
    now = datetime.now(timezone.utc)
    assert evidence_is_fresh((now - timedelta(seconds=5)).isoformat(), 60, now)
    assert not evidence_is_fresh((now - timedelta(hours=2)).isoformat(), 60, now)


def test_discovery_closure_has_only_explicit_dispositions():
    for value in data("discovery_closure.json")["allowed_dispositions"]:
        assert validate_discovery_disposition(value)
    with pytest.raises(ValueError):
        validate_discovery_disposition("MENTIONED_IN_CHAT")


def test_unfinished_work_resumes_before_adjacency():
    item = {
        "canonical_work_id": "CURRENT-WORK",
        "outcome": "resume me",
        "owner": "current governance",
        "state": "ACTIVE",
        "progress_checkpoint": "durable checkpoint",
        "state_basis_or_evidence": ["receipt"],
        "remaining_work": ["finish"],
        "blocker_or_waiting_condition": None,
        "next_admissible_action": "finish",
        "wake_condition": "next invocation",
        "completion_condition": "verified finish",
    }
    selected = select_next_work([item], adjacent={"canonical_work_id": "ADJACENT"})
    assert selected["canonical_work_id"] == "CURRENT-WORK"


def test_old_governance_cannot_veto_explicit_authorized_migration():
    a = copy.deepcopy(data("authority.json"))
    a["bootstrap_migration_authority"] = {
        "state": "ACTIVE_UNTIL_VERIFIED_CUTOVER",
        "may_be_vetoed_by_legacy_lifecycle": False,
        "ordinary_product_construction_may_use": False,
    }
    assert legacy_veto_blocks_authorized_migration(a, {"blocks": True}) is False


def test_candidate_governance_cannot_certify_itself():
    with pytest.raises(ValueError, match="cannot certify itself"):
        validate_constitutional_migration({"candidate_owner": "candidate", "candidate_certifier": "candidate", "external_invariants": ["semantic parity"], "ordinary_product_path": False})
    assert validate_constitutional_migration({"candidate_owner": "candidate", "candidate_certifier": "independent-verifier", "external_invariants": ["semantic parity"], "ordinary_product_path": False})


def test_bootstrap_authority_expires_only_after_proven_cutover():
    a = copy.deepcopy(data("authority.json"))
    a["bootstrap_migration_authority"] = {
        "state": "ACTIVE_UNTIL_VERIFIED_CUTOVER",
        "may_be_vetoed_by_legacy_lifecycle": False,
        "ordinary_product_construction_may_use": False,
    }
    with pytest.raises(ValueError):
        expire_bootstrap_authority(a, False)
    assert expire_bootstrap_authority(a, True)["bootstrap_migration_authority"]["state"] == "EXPIRED"
