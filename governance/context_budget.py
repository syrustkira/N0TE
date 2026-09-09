from __future__ import annotations

from dataclasses import asdict

from .compiled_execution_state import CompiledExecutionState
from .continuation import decide_continue


def build_execution_projection(state: CompiledExecutionState) -> dict:
    """Return the smallest broad-enough context the coordinator needs for execution.

    History and full source documents are deliberately excluded. They are retrieved
    only when a contradiction, provenance need, unresolved UNKNOWN, rights/legal
    issue or incident replay requires expansion.
    """
    if not isinstance(state, CompiledExecutionState):
        raise TypeError("state must be CompiledExecutionState")
    decision = decide_continue(state)
    material_facts = {
        fact_id: {
            "value": fact.value,
            "state": fact.state,
            "source_ref": fact.source_ref,
            "observed_at": fact.observed_at.isoformat(),
            "source_revision": fact.source_revision,
        }
        for fact_id, fact in sorted(state.facts.items())
        if fact.state in {"UNKNOWN", "UNTESTED", "TESTING", "BLOCKED", "REJECTED", "RISKY", "STALE", "OBSERVED", "VERIFIED"}
    }
    return {
        "cursor": asdict(state.cursor),
        "retained_scope_refs": list(state.retained_scope_refs),
        "truth_owners": dict(state.truth_owners),
        "required_functions": list(decision.required_functions),
        "next_dependencies": list(decision.next_dependencies),
        "material_facts": material_facts,
        "relevant_incidents": list(state.incidents),
        "compiled_fingerprint": state.fingerprint,
    }
