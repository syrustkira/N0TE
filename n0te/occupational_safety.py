from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .lineage import ValidationError

SAFETY_DOMAINS = {
    "HEARING",
    "VOICE",
    "ERGONOMICS",
    "FATIGUE",
    "TRAVEL",
    "LIVE",
    "MENTAL_WELLBEING",
    "ACCESSIBILITY",
}
SAFETY_WORK_KINDS = {
    "CREATIVE",
    "STUDIO",
    "MIX_MASTER",
    "SESSION_PERFORMANCE",
    "LIVE",
    "TRAVEL",
}
SAFETY_CUE_KINDS = {
    "SOUND_EXPOSURE",
    "VOCAL_LOAD",
    "ERGONOMIC_LOAD",
    "FATIGUE",
    "TRAVEL_LOAD",
    "INCIDENT",
    "ACCESSIBILITY_NEED",
    "WELLBEING_CONCERN",
    "SAFE_LIMIT_QUESTION",
}
SAFETY_CUE_SOURCE_KINDS = {
    "USER_DECLARED",
    "OBSERVED",
    "MEASURED",
    "VERIFIED_EXTERNAL",
}
SAFETY_URGENCY_LEVELS = {
    "ROUTINE",
    "REVIEW",
    "PAUSE_WORK",
    "HUMAN_SUPPORT",
}
SAFETY_GUIDANCE_SOURCE_KINDS = {
    "AUTHORITATIVE_EXTERNAL",
    "PROFESSIONAL_GUIDANCE",
    "OBSERVED_EXTERNAL",
    "USER_DECLARED",
}
VERIFIED_SAFETY_GUIDANCE_SOURCE_KINDS = {
    "AUTHORITATIVE_EXTERNAL",
    "PROFESSIONAL_GUIDANCE",
    "OBSERVED_EXTERNAL",
}
SAFETY_GUIDANCE_STATES = {
    "APPLICABLE",
    "OUT_OF_SCOPE",
    "NOT_YET_OBSERVED",
    "STALE",
    "UNVERIFIED",
}
SAFETY_ASSESSMENT_STATES = {
    "UNKNOWN",
    "MONITOR",
    "REVIEW",
    "PAUSE",
    "ESCALATE",
}

_URGENCY_RANK = {
    "ROUTINE": 0,
    "REVIEW": 1,
    "PAUSE_WORK": 2,
    "HUMAN_SUPPORT": 3,
}


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field_name} must not be empty")
    return normalized


def _require_aware(value: datetime, *, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise ValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware")


def _normalize_text_tuple(values: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValidationError(f"{field_name} must be a sequence of text values")
    normalized: list[str] = []
    for value in values:
        item = _require_text(value, field_name)
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


@dataclass(frozen=True)
class OccupationalSafetyContext:
    """One bounded music-work context; not a health record or diagnosis."""

    role: str
    work_kind: str
    context_ref: str
    quiet_requested: bool = False

    def __post_init__(self) -> None:
        role = _require_text(self.role, "role")
        work_kind = _require_text(self.work_kind, "work_kind").upper()
        context_ref = _require_text(self.context_ref, "context_ref")
        if work_kind not in SAFETY_WORK_KINDS:
            raise ValidationError(f"unsupported safety work kind: {work_kind}")
        if not isinstance(self.quiet_requested, bool):
            raise ValidationError("quiet_requested must be boolean")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "work_kind", work_kind)
        object.__setattr__(self, "context_ref", context_ref)


@dataclass(frozen=True)
class SafetyCue:
    """A source-bound work-safety cue. The statement is evidence, not diagnosis."""

    id: str
    domain: str
    kind: str
    statement: str
    source_kind: str
    source_ref: str
    observed_at: datetime
    urgency: str = "REVIEW"

    def __post_init__(self) -> None:
        cue_id = _require_text(self.id, "safety cue id")
        domain = _require_text(self.domain, "safety cue domain").upper()
        kind = _require_text(self.kind, "safety cue kind").upper()
        statement = _require_text(self.statement, "safety cue statement")
        source_kind = _require_text(self.source_kind, "safety cue source_kind").upper()
        source_ref = _require_text(self.source_ref, "safety cue source_ref")
        urgency = _require_text(self.urgency, "safety cue urgency").upper()
        if domain not in SAFETY_DOMAINS:
            raise ValidationError(f"unsupported safety domain: {domain}")
        if kind not in SAFETY_CUE_KINDS:
            raise ValidationError(f"unsupported safety cue kind: {kind}")
        if source_kind not in SAFETY_CUE_SOURCE_KINDS:
            raise ValidationError(f"unsupported safety cue source: {source_kind}")
        if urgency not in SAFETY_URGENCY_LEVELS:
            raise ValidationError(f"unsupported safety urgency: {urgency}")
        _require_aware(self.observed_at, field_name="observed_at")
        object.__setattr__(self, "id", cue_id)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "urgency", urgency)


@dataclass(frozen=True)
class SafetyGuidance:
    """Volatile external safety guidance with explicit scope and freshness."""

    id: str
    domain: str
    statement: str
    source_kind: str
    source_ref: str
    observed_at: datetime
    revalidate_after: datetime
    applies_to_roles: tuple[str, ...] = ()
    applies_to_work_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        guidance_id = _require_text(self.id, "safety guidance id")
        domain = _require_text(self.domain, "safety guidance domain").upper()
        statement = _require_text(self.statement, "safety guidance statement")
        source_kind = _require_text(
            self.source_kind, "safety guidance source_kind"
        ).upper()
        source_ref = _require_text(self.source_ref, "safety guidance source_ref")
        if domain not in SAFETY_DOMAINS:
            raise ValidationError(f"unsupported safety domain: {domain}")
        if source_kind not in SAFETY_GUIDANCE_SOURCE_KINDS:
            raise ValidationError(f"unsupported safety guidance source: {source_kind}")
        _require_aware(self.observed_at, field_name="observed_at")
        _require_aware(self.revalidate_after, field_name="revalidate_after")
        if self.revalidate_after <= self.observed_at:
            raise ValidationError("revalidate_after must be after observed_at")
        roles = _normalize_text_tuple(self.applies_to_roles, field_name="applies_to_roles")
        work_kinds = tuple(
            item.upper()
            for item in _normalize_text_tuple(
                self.applies_to_work_kinds, field_name="applies_to_work_kinds"
            )
        )
        unsupported = sorted(set(work_kinds) - SAFETY_WORK_KINDS)
        if unsupported:
            raise ValidationError(
                "unsupported safety guidance work kind: " + ", ".join(unsupported)
            )
        object.__setattr__(self, "id", guidance_id)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "applies_to_roles", roles)
        object.__setattr__(self, "applies_to_work_kinds", work_kinds)

    @property
    def verified_source(self) -> bool:
        return self.source_kind in VERIFIED_SAFETY_GUIDANCE_SOURCE_KINDS


@dataclass(frozen=True)
class SafetyGuidanceResolution:
    guidance_id: str
    domain: str
    state: str
    source_kind: str
    source_ref: str
    observed_at: datetime
    revalidate_after: datetime
    reason: str

    def __post_init__(self) -> None:
        if self.state not in SAFETY_GUIDANCE_STATES:
            raise ValidationError(f"unsupported safety guidance state: {self.state}")

    @property
    def usable_as_current_guidance(self) -> bool:
        return self.state == "APPLICABLE"


@dataclass(frozen=True)
class OccupationalSafetyAssessment:
    context_ref: str
    role: str
    work_kind: str
    state: str
    cue_ids: tuple[str, ...]
    domains: tuple[str, ...]
    guidance_resolutions: tuple[SafetyGuidanceResolution, ...]
    suggested_actions: tuple[str, ...]
    needs_fresh_guidance: bool
    must_interrupt: bool
    reason: str
    certifies_safe_exposure: bool = field(default=False, init=False)
    clinical_diagnosis: None = field(default=None, init=False)
    external_action_authorized: bool = field(default=False, init=False)
    mutation_authorized: bool = field(default=False, init=False)
    authority_effect: str = field(default="UNCHANGED", init=False)
    persistence_effect: str = field(default="NONE", init=False)

    def __post_init__(self) -> None:
        if self.state not in SAFETY_ASSESSMENT_STATES:
            raise ValidationError(f"unsupported safety assessment state: {self.state}")


_DOMAIN_ACTIONS = {
    "HEARING": (
        "Reduce or pause sound exposure when needed; verify any safe-level or duration decision against fresh authoritative guidance.",
    ),
    "VOICE": (
        "Reduce vocal load or pause when needed; route injury or medical questions to appropriate human/professional guidance.",
    ),
    "ERGONOMICS": (
        "Adjust the work setup or pause when strain or discomfort is reported; do not treat the cue as a diagnosis.",
    ),
    "FATIGUE": (
        "Use a break or recovery plan before continuing demanding work when fatigue is reported.",
    ),
    "TRAVEL": (
        "Review rest, schedule, transport, and duty-of-care constraints before continuing demanding travel or work.",
    ),
    "LIVE": (
        "Use the applicable incident or venue safety plan and escalate unresolved hazards to the responsible human.",
    ),
    "MENTAL_WELLBEING": (
        "Offer a bounded pause or support route and send health concerns to appropriate human/professional support rather than diagnosing.",
    ),
    "ACCESSIBILITY": (
        "Apply the stated accessibility preference without inferring a medical condition.",
    ),
}


def resolve_safety_guidance(
    context: OccupationalSafetyContext,
    guidance: SafetyGuidance,
    *,
    as_of: datetime,
) -> SafetyGuidanceResolution:
    """Resolve current guidance without converting it into certification or authority."""

    _require_aware(as_of, field_name="as_of")
    if guidance.applies_to_roles and context.role not in guidance.applies_to_roles:
        state = "OUT_OF_SCOPE"
        reason = "The guidance does not apply to the current professional role."
    elif (
        guidance.applies_to_work_kinds
        and context.work_kind not in guidance.applies_to_work_kinds
    ):
        state = "OUT_OF_SCOPE"
        reason = "The guidance does not apply to the current work context."
    elif as_of < guidance.observed_at:
        state = "NOT_YET_OBSERVED"
        reason = "The evaluation time predates the guidance observation."
    elif as_of >= guidance.revalidate_after:
        state = "STALE"
        reason = "The guidance reached its explicit revalidation boundary."
    elif not guidance.verified_source:
        state = "UNVERIFIED"
        reason = "The guidance is user-declared and is not independently verified."
    else:
        state = "APPLICABLE"
        reason = "The guidance matches scope, has a verified source, and is within its freshness window."

    return SafetyGuidanceResolution(
        guidance_id=guidance.id,
        domain=guidance.domain,
        state=state,
        source_kind=guidance.source_kind,
        source_ref=guidance.source_ref,
        observed_at=guidance.observed_at,
        revalidate_after=guidance.revalidate_after,
        reason=reason,
    )


def assess_occupational_safety(
    context: OccupationalSafetyContext,
    cues: Iterable[SafetyCue],
    *,
    guidance: Iterable[SafetyGuidance] = (),
    as_of: datetime,
) -> OccupationalSafetyAssessment:
    """Produce a bounded non-clinical work-safety plan from explicit evidence."""

    _require_aware(as_of, field_name="as_of")
    cue_rows = tuple(cues)
    guidance_rows = tuple(guidance)
    cue_ids = tuple(row.id for row in cue_rows)
    guidance_ids = tuple(row.id for row in guidance_rows)
    if len(cue_ids) != len(set(cue_ids)):
        raise ValidationError("safety cue IDs must be unique")
    if len(guidance_ids) != len(set(guidance_ids)):
        raise ValidationError("safety guidance IDs must be unique")
    future_cues = tuple(row.id for row in cue_rows if row.observed_at > as_of)
    if future_cues:
        raise ValidationError(
            "safety cues cannot be observed after as_of: " + ", ".join(future_cues)
        )

    domains = tuple(sorted({row.domain for row in cue_rows}))
    resolutions = tuple(
        resolve_safety_guidance(context, row, as_of=as_of)
        for row in guidance_rows
        if not domains or row.domain in domains
    )
    current_guidance_domains = {
        row.domain for row in resolutions if row.usable_as_current_guidance
    }
    guidance_sensitive_domains = {
        row.domain
        for row in cue_rows
        if row.kind == "SAFE_LIMIT_QUESTION" or row.source_kind == "MEASURED"
    }
    needs_fresh_guidance = bool(
        guidance_sensitive_domains - current_guidance_domains
    )

    if not cue_rows:
        state = "UNKNOWN"
        reason = "No source-bound occupational safety cue is available for this work context."
    else:
        strongest = max(cue_rows, key=lambda row: _URGENCY_RANK[row.urgency]).urgency
        if strongest == "HUMAN_SUPPORT":
            state = "ESCALATE"
            reason = "Source-bound evidence explicitly requires appropriate human/professional support."
        elif strongest == "PAUSE_WORK":
            state = "PAUSE"
            reason = "Source-bound evidence explicitly calls for pausing the affected work before continuing."
        elif strongest == "REVIEW":
            state = "REVIEW"
            reason = "Source-bound evidence should be reviewed in a bounded protective plan before or during the work."
        else:
            state = "MONITOR"
            reason = "Only routine source-bound safety context is present; no higher urgency is claimed."

    actions: list[str] = []
    for domain in domains:
        for action in _DOMAIN_ACTIONS[domain]:
            if action not in actions:
                actions.append(action)
    if state == "ESCALATE":
        actions.insert(
            0,
            "Pause the affected work and seek the appropriate responsible human or professional support; N0TE does not diagnose or certify the condition.",
        )
    elif state == "PAUSE":
        actions.insert(
            0,
            "Pause the affected work and review the recorded safety context before resuming.",
        )
    if needs_fresh_guidance:
        actions.append(
            "Do not infer a universal safe limit from the measurement or question; obtain fresh authoritative guidance for the applicable context."
        )

    must_interrupt = state in {"PAUSE", "ESCALATE"}
    return OccupationalSafetyAssessment(
        context_ref=context.context_ref,
        role=context.role,
        work_kind=context.work_kind,
        state=state,
        cue_ids=cue_ids,
        domains=domains,
        guidance_resolutions=resolutions,
        suggested_actions=tuple(actions),
        needs_fresh_guidance=needs_fresh_guidance,
        must_interrupt=must_interrupt,
        reason=reason,
    )
