from __future__ import annotations

from datetime import datetime, timezone

from governance.compiled_execution_state import compile_execution_state
from governance.execution_state_bridge import compiled_state_to_trusted_snapshot


def test_compiled_state_becomes_gate_snapshot_without_second_reasoning_loop():
    raw = {
        "retained_scope_refs": ["artist", "services"],
        "truth_owners": {
            "WHY": "master-context",
            "WHAT": "public-scope",
            "HOW": "product-db",
            "NOW": "os",
            "PROOF": "runtime",
        },
        "dependency_graph": {"artist": [], "services": ["service_acquisition"]},
        "lens_dispatch": {
            "artist": ["epistemology"],
            "services": ["epistemology", "anti_flattening", "offer"],
        },
        "cursor": {
            "job_id": "job-1",
            "outcome": "represent services without flattening",
            "active_object": "services",
            "current_step": "verify buyer path",
            "acceptance": "buyer path verified",
            "state": "ACTIVE",
            "blockers": [],
        },
        "facts": [],
        "incidents": [],
        "source_fingerprint": "source-1",
        "compiled_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
    }
    state = compile_execution_state(raw)
    snapshot = compiled_state_to_trusted_snapshot(state)
    assert snapshot.retained_scope_refs == frozenset({"artist", "services"})
    assert snapshot.policies["services"]["required_functions"] == frozenset({
        "epistemology",
        "anti_flattening",
        "offer",
    })
    assert snapshot.policies["services"]["required_dependencies"]["downstream"] == frozenset({"service_acquisition"})
