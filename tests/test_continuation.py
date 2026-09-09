from datetime import datetime, timezone

from governance.compiled_execution_state import compile_execution_state
from governance.continuation import advance_after_acceptance, decide_continue


def _state(blockers=(), state="READY"):
    return compile_execution_state({
        "retained_scope_refs": ["n0te", "runtime_context", "daw_integration", "music_intelligence"],
        "truth_owners": {"WHY": "w", "WHAT": "x", "HOW": "y", "NOW": "n", "PROOF": "p"},
        "dependency_graph": {
            "n0te": ["runtime_context", "daw_integration", "music_intelligence"],
            "runtime_context": [],
            "daw_integration": [],
            "music_intelligence": [],
        },
        "lens_dispatch": {"n0te": ["epistemology", "parallelization", "integration"]},
        "cursor": {
            "job_id": "job",
            "outcome": "build N0TE",
            "active_object": "n0te",
            "current_step": "continue construction",
            "acceptance": "verified integration",
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
    assert decision.active_object == "n0te"
    assert decision.current_step == "continue construction"
    assert decision.required_functions == ("epistemology", "parallelization", "integration")
    assert decision.next_dependencies == ("runtime_context", "daw_integration", "music_intelligence")


def test_continue_stops_at_real_blocker():
    assert decide_continue(_state(blockers=("approval",))).stop_reason == "BLOCKED"


def test_acceptance_preserves_all_parallel_canonical_successors():
    assert advance_after_acceptance(_state(), completed_object="n0te") == (
        "runtime_context",
        "daw_integration",
        "music_intelligence",
    )
