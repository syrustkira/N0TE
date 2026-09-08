from __future__ import annotations

from dataclasses import dataclass


class RecoveryError(ValueError):
    pass


@dataclass(frozen=True)
class RecoveryState:
    action_id: str
    status: str
    reason: str
    requires_fresh_observation: bool
    retry_allowed: bool


def uncertain_external_action(action_id: str, reason: str) -> RecoveryState:
    action_id = str(action_id).strip()
    reason = str(reason).strip()
    if not action_id or not reason:
        raise RecoveryError("action_id and reason are required")
    return RecoveryState(
        action_id=action_id,
        status="UNKNOWN_RECOVERY_REQUIRED",
        reason=reason,
        requires_fresh_observation=True,
        retry_allowed=False,
    )


def resolve_recovery(state: RecoveryState, *, observed: bool, observation_ref: str | None = None) -> RecoveryState:
    if not isinstance(state, RecoveryState):
        raise TypeError("state must be RecoveryState")
    if state.status != "UNKNOWN_RECOVERY_REQUIRED":
        raise RecoveryError("only unknown recovery states can be resolved")
    if not observed:
        return state
    if not observation_ref or not str(observation_ref).strip():
        raise RecoveryError("fresh observation requires observation_ref")
    return RecoveryState(
        action_id=state.action_id,
        status="RECOVERED",
        reason=f"fresh observation: {str(observation_ref).strip()}",
        requires_fresh_observation=False,
        retry_allowed=True,
    )
