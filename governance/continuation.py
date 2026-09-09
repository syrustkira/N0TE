from __future__ import annotations

from dataclasses import dataclass

from .compiled_execution_state import CompiledExecutionState


class ContinuationError(ValueError):
    pass


@dataclass(frozen=True)
class ContinuationDecision:
    job_id: str
    active_object: str
    current_step: str
    required_functions: tuple[str, ...]
    next_dependencies: tuple[str, ...]
    stop_reason: str | None


def decide_continue(state: CompiledExecutionState) -> ContinuationDecision:
    if not isinstance(state, CompiledExecutionState):
        raise TypeError("state must be CompiledExecutionState")

    cursor = state.cursor
    stop_reason: str | None = None
    if cursor.blockers:
        stop_reason = "BLOCKED"
    elif cursor.state in {"WAITING", "HUMAN_REQUIRED"}:
        stop_reason = cursor.state

    return ContinuationDecision(
        job_id=cursor.job_id,
        active_object=cursor.active_object,
        current_step=cursor.current_step,
        required_functions=state.required_functions(),
        next_dependencies=state.next_dependencies(),
        stop_reason=stop_reason,
    )


def advance_after_acceptance(
    state: CompiledExecutionState,
    *,
    completed_object: str,
) -> tuple[str, ...]:
    """Return every canonical successor that becomes available after acceptance.

    Construction must not silently serialize safe sibling work by selecting only the
    first successor. Collision, authority and dependency checks happen at the work
    allocator/integration boundary; this function preserves the complete canonical
    ready set for that decision.
    """
    if completed_object != state.cursor.active_object:
        raise ContinuationError("cannot advance a cursor for a different active object")
    return state.next_dependencies()
