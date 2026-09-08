from datetime import datetime, timezone

from governance.compiled_execution_state import compile_execution_state
from governance.context_budget import build_execution_projection


def test_projection_is_broad_in_state_but_small_in_payload():
    state = compile_execution_state({
        "retained_scope_refs": ["artist", "producer", "services"],
        "truth_owners": {"WHY": "w", "WHAT": "x", "HOW": "y", "NOW": "n", "PROOF": "p"},
        "dependency_graph": {"services": ["service_acquisition"]},
        "lens_dispatch": {
            "artist": ["epistemology"],
            "producer": ["production"],
            "services": ["epistemology", "offer"],
        },
        "cursor": {
            "job_id": "j",
            "outcome": "conversion",
            "active_object": "services",
            "current_step": "verify path",
            "acceptance": "verified path",
            "state": "READY",
            "blockers": [],
        },
        "facts": [],
        "incidents": ["KNOWN_LOOP"],
        "source_fingerprint": "s",
        "compiled_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
    })
    projection = build_execution_projection(state)
    assert projection["retained_scope_refs"] == ["artist", "producer", "services"]
    assert projection["required_functions"] == ["epistemology", "offer"]
    assert "history" not in projection
