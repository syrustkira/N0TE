from __future__ import annotations

from dataclasses import dataclass


EVENTS = frozenset({
    "USER_REPEATED_CONTEXT",
    "USER_CORRECTED_EXISTING_RULE",
    "USER_CORRECTED_STATE_CLASS",
    "USER_RECONSTRUCTED_OWNER",
    "TASK_REOPENED",
    "EXECUTION_REWORK",
    "PERMIT_BLOCK",
    "RETRIEVAL_FAILURE",
    "FUNCTION_DISPATCH_FAILURE",
    "STALE_STATE_INCIDENT",
})


@dataclass(frozen=True)
class SupervisionEvent:
    event: str
    job_id: str
    seconds: int = 0

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise ValueError(f"unsupported supervision event: {self.event}")
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("job_id must be non-empty")
        if not isinstance(self.seconds, int) or self.seconds < 0:
            raise ValueError("seconds must be a non-negative integer")


def summarize_supervision(events: list[SupervisionEvent]) -> dict[str, int]:
    summary = {event: 0 for event in EVENTS}
    summary["HUMAN_RECONSTRUCTION_SECONDS"] = 0
    for item in events:
        if not isinstance(item, SupervisionEvent):
            raise TypeError("events must contain SupervisionEvent values")
        summary[item.event] += 1
        if item.event in {
            "USER_REPEATED_CONTEXT",
            "USER_CORRECTED_EXISTING_RULE",
            "USER_CORRECTED_STATE_CLASS",
            "USER_RECONSTRUCTED_OWNER",
            "EXECUTION_REWORK",
        }:
            summary["HUMAN_RECONSTRUCTION_SECONDS"] += item.seconds
    return summary
