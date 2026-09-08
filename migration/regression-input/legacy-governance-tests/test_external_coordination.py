import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "governance"))

from external_coordination import (  # noqa: E402
    acceptance_evidence_status,
    build_action_result,
    canonical_digest,
    changed,
    compact_continue_snapshot,
    normalize_runtime_actor,
    operation_state,
    prepare_action_request,
    runtime_actor_conflict,
    select_executor,
)


def load_json(name: str):
    return json.loads((ROOT / "governance" / name).read_text(encoding="utf-8"))


def test_operating_layers_keep_product_builder_and_cloud_plane_separate():
    data = load_json("operating_layers.json")
    layers = {row["class"]: row for row in data["layers"]}
    assert "EMBEDDED_ARTIST_HQ_SIBLING" in layers
    assert "CLOUD_COORDINATION_PLANE" in layers
    assert "TEMPORARY_CONSTRUCTION_MACHINERY" in layers
    assert layers["EMBEDDED_ARTIST_HQ_SIBLING"]["persistent"] is True
    assert layers["TEMPORARY_CONSTRUCTION_MACHINERY"]["persistent"] is False
    assert "N0TE product semantics" in layers["TEMPORARY_CONSTRUCTION_MACHINERY"]["does_not_own"]
    assert any("Foreground outcomes" in rule for rule in data["scope_invariants"])


def test_external_capability_matrix_is_not_a_product_capability_system():
    data = load_json("executor_capabilities.json")
    assert data["scope"] == "EXTERNAL_COORDINATION_ONLY"
    assert data["not_product_capabilities"] is True
    assert data["security"]["store_secrets"] is False


def test_runtime_state_does_not_infer_scheduler_from_semantic_active():
    actor = {"id": "x", "lifecycle": {"state": "ACTIVE"}}
    normalized = normalize_runtime_actor(actor, None)
    assert normalized.semantic_lifecycle == "ACTIVE"
    assert normalized.scheduler_enabled is None
    assert normalized.health == "UNOBSERVED_RUNTIME"
    assert runtime_actor_conflict(normalized) is None


def test_runtime_observation_detects_active_semantics_but_disabled_scheduler():
    actor = {"id": "x", "lifecycle": {"state": "ACTIVE"}}
    normalized = normalize_runtime_actor(
        actor,
        {"scheduler_enabled": False, "health": "NOT_RUNNING", "observed_at": "2026-09-02"},
    )
    assert normalized.semantic_lifecycle == "ACTIVE"
    assert normalized.scheduler_enabled is False
    assert normalized.health == "NOT_RUNNING"
    conflict = runtime_actor_conflict(normalized)
    assert conflict["conflict"] == "SEMANTIC_ACTIVE_RUNTIME_DISABLED"
    assert conflict["requires_reconciliation"] is True


def test_runtime_observation_detects_nonactive_semantics_but_enabled_scheduler():
    actor = {"id": "x", "lifecycle": {"state": "DORMANT"}}
    normalized = normalize_runtime_actor(
        actor,
        {"scheduler_enabled": True, "health": "RUNNING", "observed_at": "2026-09-02"},
    )
    conflict = runtime_actor_conflict(normalized)
    assert conflict["conflict"] == "SEMANTIC_NONACTIVE_RUNTIME_ENABLED"


def test_continue_snapshot_digest_suppresses_unchanged_loops():
    snap = compact_continue_snapshot(
        work_id="UX-01-CONTEXT-LIFECYCLE-01",
        state="BLOCKED",
        owner="N0TE-BUILD-HARNESS",
        blocker="windows-test",
        next_action="repair exact failing test",
        evidence_basis={"head": "abc", "run": 1},
    )
    assert changed(None, snap) is True
    assert changed(snap["change_digest"], snap) is False
    assert canonical_digest({"b": 2, "a": 1}) == canonical_digest({"a": 1, "b": 2})


def test_executor_selection_prefers_observed_working_and_preserves_limits():
    matrix = load_json("executor_capabilities.json")
    assert operation_state(matrix, "CHAT_GITHUB_CONNECTOR", "file_read") == "OBSERVED_WORKING"
    choice = select_executor(matrix, "file_read", ["CHAT_GITHUB_CONNECTOR", "LOCAL_GH"])
    assert choice["surface"] == "CHAT_GITHUB_CONNECTOR"
    assert choice["state"] == "OBSERVED_WORKING"

    logs = select_executor(
        matrix,
        "workflow_job_log_read",
        ["CHAT_GITHUB_CONNECTOR", "LOCAL_GH"],
    )
    assert logs["surface"] == "CHAT_GITHUB_CONNECTOR"
    assert logs["state"] == "OBSERVED_WORKING"


def test_action_receipt_preserves_trace_and_memory_consultation_refs():
    contract = load_json("action_receipt_contract.json")
    request = prepare_action_request(
        contract,
        operation_id="op-1",
        trace_id="trace-song-resume-1",
        requested_by="CHATGPT-BROAD-OPERATOR",
        semantic_target="artist:TellMeN0TE/song:Trapout",
        desired_outcome="resume the correct Song context without losing prior decisions",
        executor_class="N0TE_PRODUCT",
        authority_basis={"class": "SAFE_RETRIEVAL"},
        state_basis={"head": "abc", "song_version": "v3"},
        preconditions=["artist identity resolved", "song identity resolved"],
        idempotency_key="resume:TellMeN0TE:Trapout:v3",
        approval_state="NOT_REQUIRED",
        artifact_refs=["song:Trapout:v3"],
        expected_effect="return a read-only reconstructed Song context",
        consulted_context_refs=["decision:creative-lock-1", "preference:workflow-1"],
    )
    assert request["consulted_context_refs"] == ["decision:creative-lock-1", "preference:workflow-1"]

    result = build_action_result(
        contract,
        request,
        executor="N0TE",
        result_state="SUCCEEDED",
        observed_effect={"song_context_restored": True},
        evidence_refs=["receipt:resume-1"],
        observed_at="2026-09-02T15:00:00Z",
        retry_safe=True,
        reconciliation_required=False,
    )
    assert result["trace_id"] == request["trace_id"]
    assert result["operation_id"] == request["operation_id"]


def test_acceptance_spine_does_not_infer_missing_reachability_or_value():
    spine = load_json("acceptance_evidence_spine.json")
    evidence = {
        "requirement_id": "REQ-SCOPE-999",
        "canonical_scope_ref": "scope:REQ-SCOPE-999",
        "implementation_refs": ["commit:abc"],
        "integration_refs": ["test:integration"],
        "user_reachability_refs": [],
        "verification_refs": ["ci:green"],
        "failure_recovery_refs": [],
        "authority_security_refs": [],
        "consumer_acceptance_refs": [],
        "value_evidence_refs": ["artist:liked-result"],
    }
    status = acceptance_evidence_status(spine, evidence)
    assert status["states"]["MAPPED"] is True
    assert status["states"]["IMPLEMENTED"] is True
    assert status["states"]["INTEGRATED"] is True
    assert status["states"]["REACHABLE"] is False
    assert status["states"]["VERIFIED"] is True
    assert status["states"]["VALUE_EVIDENCED"] is True
    assert status["highest_contiguous_state"] == "INTEGRATED"
