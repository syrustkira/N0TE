from datetime import datetime, timezone

from governance.compiled_execution_state import compile_execution_state
from governance.continuation import advance_after_acceptance, decide_continue


def _state(blockers=(), state="READY"):
    return compile_execution_state({
        "retained_scope_refs": ["service_acquisition"],
        "truth_owners": {"WHY": "w", "WHAT": "x", "HOW": "y", "NOW": "n", "PROOF": "p"},
        "dependency_graph": {"service_acquisition": ["qualified_inquiry"]},
        "lens_dispatch": {"service_acquisition": ["epistemology", "distribution", "sales"]},
        "cursor": {
            "job_id": "job",
            "outcome": "demand evidence",
            "active_object": "service_acquisition",
            "current_step": "run acquisition increment",
            "acceptance": "external evidence",
            "state": state,
            "blockers": list(blockers),
        },
        "facts": [],
        "incidents": [],
        "source_fingerprint": "s",
        "compiled_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
    })


def test_continue_resumes_cursor_instead_of_rediscovering_project():
    decision = decide_continue(_state())
    assert decision.active_object == "service_acquisition"
    assert decision.current_step == "run acquisition increment"
    assert decision.required_functions == ("epistemology", "distribution", "sales")


def test_continue_stops_at_real_blocker():
    assert decide_continue(_state(blockers=("approval",))).stop_reason == "BLOCKED"


def test_acceptance_traverses_canonical_dependency_not_model_salience():
    assert advance_after_acceptance(_state(), completed_object="service_acquisition") == "qualified_inquiry"
