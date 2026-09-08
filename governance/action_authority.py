from __future__ import annotations

from dataclasses import dataclass


class StandingAuthorityError(ValueError):
    pass


@dataclass(frozen=True)
class StandingAuthorityDecision:
    requires_human: bool
    reason: str


def evaluate_standing_authority(*, action_class: str, consequences: list[str], manifest: dict) -> StandingAuthorityDecision:
    action_class = str(action_class).strip().upper()
    if not isinstance(manifest, dict):
        raise StandingAuthorityError("manifest must be an object")
    standing = manifest.get("standing_authority")
    if not isinstance(standing, dict):
        raise StandingAuthorityError("manifest lacks standing_authority")
    auto = {str(item).strip().upper() for item in standing.get("auto", [])}
    human_when = {str(item).strip() for item in standing.get("human_required_when", [])}
    actual = {str(item).strip() for item in consequences}
    matched = sorted(human_when.intersection(actual))
    if matched:
        return StandingAuthorityDecision(True, f"consequential class requires artist authority: {matched}")
    if action_class in auto:
        return StandingAuthorityDecision(False, "inside standing authority")
    return StandingAuthorityDecision(True, f"action class {action_class} is outside standing authority")
