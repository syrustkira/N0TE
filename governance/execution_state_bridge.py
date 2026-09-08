from __future__ import annotations

from datetime import timedelta
from typing import Mapping

from .compiled_execution_state import CompiledExecutionState
from .trusted_context import TrustedContextSnapshot, validate_trusted_context_snapshot


def compiled_state_to_trusted_snapshot(
    state: CompiledExecutionState,
    *,
    ttl_seconds: int = 300,
) -> TrustedContextSnapshot:
    if not isinstance(state, CompiledExecutionState):
        raise TypeError("state must be CompiledExecutionState")
    if not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise ValueError("ttl_seconds must be positive")

    policies: dict[str, Mapping[str, object]] = {}
    for active_object in state.retained_scope_refs:
        functions = list(state.lens_dispatch.get(active_object, ()))
        dependencies = {
            "upstream": [],
            "downstream": list(state.dependency_graph.get(active_object, ())),
        }
        policies[active_object] = {
            "required_functions": functions,
            "required_dependencies": dependencies,
            "allowed_outcome_classes": ["INTERNAL", "EXTERNAL", "HUMAN"],
        }

    raw = {
        "snapshot_id": f"compiled:{state.cursor.job_id}:{state.fingerprint[:16]}",
        "source_fingerprint": state.source_fingerprint,
        "observed_at": state.compiled_at.isoformat(),
        "expires_at": (state.compiled_at + timedelta(seconds=ttl_seconds)).isoformat(),
        "retained_scope_refs": list(state.retained_scope_refs),
        "truth_owners": dict(state.truth_owners),
        "policies": policies,
    }
    return validate_trusted_context_snapshot(raw)
