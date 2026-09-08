from __future__ import annotations

from dataclasses import dataclass


class ScaffoldBudgetError(ValueError):
    pass


@dataclass(frozen=True)
class ScaffoldChangeJustification:
    observed_failure_ref: str | None = None
    blocked_outcome_ref: str | None = None
    expected_supervision_reduction: str | None = None

    def validate(self) -> None:
        if not (self.observed_failure_ref or self.blocked_outcome_ref):
            raise ScaffoldBudgetError(
                "new scaffold requires an observed recurring failure or a blocked real outcome"
            )
        if not self.expected_supervision_reduction or not self.expected_supervision_reduction.strip():
            raise ScaffoldBudgetError(
                "new scaffold must state how it is expected to reduce supervision or unlock execution"
            )


def require_scaffold_justification(raw: dict) -> ScaffoldChangeJustification:
    if not isinstance(raw, dict):
        raise ScaffoldBudgetError("scaffold justification must be an object")
    item = ScaffoldChangeJustification(
        observed_failure_ref=raw.get("observed_failure_ref"),
        blocked_outcome_ref=raw.get("blocked_outcome_ref"),
        expected_supervision_reduction=raw.get("expected_supervision_reduction"),
    )
    item.validate()
    return item
