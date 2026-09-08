from __future__ import annotations

from dataclasses import dataclass

from .evidence import SOURCE_KINDS
from .lineage import ValidationError


@dataclass(frozen=True)
class BeliefSourcePresentation:
    """Read-only consumer wording for one canonical evidence source kind."""

    source_kind: str
    label: str
    explanation: str


_BELIEF_SOURCE_PRESENTATIONS = {
    "USER_DECLARED": BeliefSourcePresentation(
        source_kind="USER_DECLARED",
        label="YOU TOLD N0TE",
        explanation=(
            "You said this or supplied it directly. That declaration is not independently "
            "verified merely because N0TE can show it here."
        ),
    ),
    "OBSERVED": BeliefSourcePresentation(
        source_kind="OBSERVED",
        label="OBSERVED NOW",
        explanation=(
            "Observed directly from the current legitimately available state. "
            "The observation is only as broad as the scope shown with it."
        ),
    ),
    "MEASURED": BeliefSourcePresentation(
        source_kind="MEASURED",
        label="MEASURED",
        explanation=(
            "Calculated from exact bound evidence. A measurement can describe the "
            "material without deciding whether the artist should prefer it."
        ),
    ),
    "PROVIDER_VERIFIED": BeliefSourcePresentation(
        source_kind="PROVIDER_VERIFIED",
        label="PROVIDER VERIFIED",
        explanation=(
            "Confirmed by provider evidence for the stated scope. Provider verification "
            "does not become broader artistic, legal, or certification truth."
        ),
    ),
    "REMEMBERED": BeliefSourcePresentation(
        source_kind="REMEMBERED",
        label="REMEMBERED",
        explanation=(
            "Recovered from durable N0TE memory that was legitimately promoted earlier. "
            "Remembered context can still require freshness or scope checks."
        ),
    ),
    "INFERRED": BeliefSourcePresentation(
        source_kind="INFERRED",
        label="INFERRED",
        explanation=(
            "Reasoned from other evidence. An inference is a testable interpretation, "
            "not an observation, measurement, or provider verification."
        ),
    ),
}


def present_belief_source(source_kind: str) -> BeliefSourcePresentation:
    """Return canonical consumer wording without creating or upgrading evidence."""

    kind = str(source_kind).strip().upper()
    if kind not in SOURCE_KINDS:
        raise ValidationError(f"unsupported evidence source: {kind}")
    presentation = _BELIEF_SOURCE_PRESENTATIONS.get(kind)
    if presentation is None:
        raise ValidationError(f"evidence source has no consumer presentation: {kind}")
    return presentation
