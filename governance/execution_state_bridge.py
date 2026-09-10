from __future__ import annotations

from datetime import timedelta
from typing import Mapping

from .compiled_execution_state import CompiledExecutionState
from .execution_envelope import OUTCOME_CLASSES
from .trusted_context import TrustedContextSnapshot, validate_trusted_context_snapshot


def _upstream_dependencies(state: CompiledExecutionState, active_object: str) -> list[str]:
    return sorted(
        node
        for node, successors in state.dependency_graph.items()
        if active_object in successors
    )


def _compiled_authority_source(state: CompiledExecutionState) -> str:
    return f"compiled-context:{state.source_fingerprint}"


def compiled_state_to_trusted_snapshot(
    state: CompiledExecutionState,
    *,
    ttl_seconds: int = 300,
) -> TrustedContextSnapshot:
    if not isinstance(state, CompiledExecutionState):
        raise TypeError("state must be CompiledExecutionState")
    if not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise ValueError("ttl_seconds must be positive")

    authority_source = _compiled_authority_source(state)
    policies: dict[str, Mapping[str, object]] = {}
    for active_object in state.retained_scope_refs:
        functions = list(state.lens_dispatch.get(active_object, ()))
        dependencies = {
            "upstream": _upstream_dependencies(state, active_object),
            "downstream": list(state.dependency_graph.get(active_object, ())),
        }
        policies[active_object] = {
            "required_functions": functions,
            "required_dependencies": dependencies,
            "allowed_outcome_classes": sorted(OUTCOME_CLASSES),
            # The compiled context may carry standing authority for reversible work
            # only. More consequential classes remain absent and therefore fail
            # closed until a separately trusted authority/approval source exists.
            "authority_by_action_class": {
                "REVERSIBLE": {
                    "requires_human": False,
                    "source_refs": [authority_source],
                }
            },
        }

    raw = {
        "snapshot_id": f"compiled:{state.cursor.job_id}:{state.fingerprint[:16]}",
        "source_fingerprint": state.source_fingerprint,
        "observed_at": state.compiled_at.isoformat(),
        "expires_at": (state.compiled_at + timedelta(seconds=ttl_seconds)).isoformat(),
        "retained_scope_refs": list(state.retained_scope_refs),
        "truth_owners": dict(state.truth_owners),
        "policies": policies,
        # Compilation must never invent human approval. Empty means exactly that.
        "approvals": [],
    }
    return validate_trusted_context_snapshot(raw)
