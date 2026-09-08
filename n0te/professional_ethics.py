from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .lineage import ValidationError


ETHICS_CHECK_KINDS = {
    "CONFLICT_OF_INTEREST",
    "CONFIDENTIALITY_PERMISSION",
    "INFORMED_CONSENT",
    "ATTRIBUTION_CONSENT",
    "POWER_ASYMMETRY",
    "SAFEGUARDING_CONCERN",
}
ETHICS_SIGNAL_SOURCE_KINDS = {
    "USER_DECLARED",
    "OBSERVED",
    "VERIFIED_EXTERNAL",
}
ETHICS_SIGNAL_STATES = {
    "PRESENT",
    "ABSENT",
    "UNKNOWN",
    "DISPUTED",
    "GRANTED",
    "WITHHELD",
    "WITHDRAWN",
    "COERCED",
}
ETHICS_IMPACT_CLASSES = {"ROUTINE", "MATERIAL", "HIGH_STAKES"}
ETHICS_ASSESSMENT_STATES = {"CLEAR", "REVIEW", "PAUSE", "ESCALATE"}
INDEPENDENT_REVIEW_DOMAINS = {"LEGAL", "CLINICAL", "FINANCIAL", "SAFEGUARDING"}

_SIGNAL_STATES_BY_KIND = {
    "CONFLICT_OF_INTEREST": {"PRESENT", "ABSENT", "UNKNOWN", "DISPUTED"},
    "CONFIDENTIALITY_PERMISSION": {
        "GRANTED",
        "WITHHELD",
        "WITHDRAWN",
        "UNKNOWN",
        "DISPUTED",
    },
    "INFORMED_CONSENT": {
        "GRANTED",
        "WITHHELD",
        "WITHDRAWN",
        "COERCED",
        "UNKNOWN",
        "DISPUTED",
    },
    "ATTRIBUTION_CONSENT": {
        "GRANTED",
        "WITHHELD",
        "WITHDRAWN",
        "UNKNOWN",
        "DISPUTED",
    },
    "POWER_ASYMMETRY": {"PRESENT", "ABSENT", "UNKNOWN", "DISPUTED"},
    "SAFEGUARDING_CONCERN": {"PRESENT", "ABSENT", "UNKNOWN", "DISPUTED"},
}


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field_name} must not be empty")
    return normalized


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{field_name} must be boolean")
    return value


def _require_aware(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware")
    return value


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
class ProfessionalEthicsContext:
    """One professional decision context. Role labels never create authority."""

    relationship_ref: str
    action_ref: str
    actor_ref: str
    actor_role: str
    affected_party_ref: str
    purpose: str
    requested_audiences: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    impact_class: str = "ROUTINE"
    independent_review_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        relationship_ref = _require_text(self.relationship_ref, "relationship_ref")
        action_ref = _require_text(self.action_ref, "action_ref")
        actor_ref = _require_text(self.actor_ref, "actor_ref")
        actor_role = _require_text(self.actor_role, "actor_role")
        affected_party_ref = _require_text(self.affected_party_ref, "affected_party_ref")
        purpose = _require_text(self.purpose, "purpose")
        requested_audiences = _normalize_text_tuple(
            self.requested_audiences, field_name="requested_audiences"
        )
        required_checks = tuple(
            item.upper()
            for item in _normalize_text_tuple(
                self.required_checks, field_name="required_checks"
            )
        )
        unsupported_checks = sorted(set(required_checks) - ETHICS_CHECK_KINDS)
        if unsupported_checks:
            raise ValidationError(
                "unsupported ethics check: " + ", ".join(unsupported_checks)
            )
        if "CONFIDENTIALITY_PERMISSION" in required_checks and not requested_audiences:
            raise ValidationError(
                "requested_audiences are required when confidentiality permission is checked"
            )
        impact_class = _require_text(self.impact_class, "impact_class").upper()
        if impact_class not in ETHICS_IMPACT_CLASSES:
            raise ValidationError(f"unsupported impact class: {impact_class}")
        review_domains = tuple(
            item.upper()
            for item in _normalize_text_tuple(
                self.independent_review_domains,
                field_name="independent_review_domains",
            )
        )
        unsupported_domains = sorted(set(review_domains) - INDEPENDENT_REVIEW_DOMAINS)
        if unsupported_domains:
            raise ValidationError(
                "unsupported independent review domain: "
                + ", ".join(unsupported_domains)
            )

        object.__setattr__(self, "relationship_ref", relationship_ref)
        object.__setattr__(self, "action_ref", action_ref)
        object.__setattr__(self, "actor_ref", actor_ref)
        object.__setattr__(self, "actor_role", actor_role)
        object.__setattr__(self, "affected_party_ref", affected_party_ref)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "requested_audiences", requested_audiences)
        object.__setattr__(self, "required_checks", required_checks)
        object.__setattr__(self, "impact_class", impact_class)
        object.__setattr__(self, "independent_review_domains", review_domains)


@dataclass(frozen=True)
class ProfessionalEthicsSignal:
    """Source-bound ethics evidence, never a sentiment, reputation, or risk score."""

    id: str
    relationship_ref: str
    kind: str
    state: str
    subject_ref: str
    statement: str
    source_kind: str
    source_ref: str
    observed_at: datetime
    permitted_purposes: tuple[str, ...] = ()
    permitted_audiences: tuple[str, ...] = ()
    voluntary_confirmed: bool = False

    def __post_init__(self) -> None:
        signal_id = _require_text(self.id, "ethics signal id")
        relationship_ref = _require_text(
            self.relationship_ref, "ethics signal relationship_ref"
        )
        kind = _require_text(self.kind, "ethics signal kind").upper()
        state = _require_text(self.state, "ethics signal state").upper()
        subject_ref = _require_text(self.subject_ref, "ethics signal subject_ref")
        statement = _require_text(self.statement, "ethics signal statement")
        source_kind = _require_text(
            self.source_kind, "ethics signal source_kind"
        ).upper()
        source_ref = _require_text(self.source_ref, "ethics signal source_ref")
        _require_aware(self.observed_at, field_name="observed_at")
        permitted_purposes = _normalize_text_tuple(
            self.permitted_purposes, field_name="permitted_purposes"
        )
        permitted_audiences = _normalize_text_tuple(
            self.permitted_audiences, field_name="permitted_audiences"
        )
        voluntary_confirmed = _require_bool(
            self.voluntary_confirmed, "voluntary_confirmed"
        )

        if kind not in ETHICS_CHECK_KINDS:
            raise ValidationError(f"unsupported ethics signal kind: {kind}")
        if state not in ETHICS_SIGNAL_STATES:
            raise ValidationError(f"unsupported ethics signal state: {state}")
        if state not in _SIGNAL_STATES_BY_KIND[kind]:
            raise ValidationError(
                f"ethics signal state {state} is invalid for kind {kind}"
            )
        if source_kind not in ETHICS_SIGNAL_SOURCE_KINDS:
            raise ValidationError(f"unsupported ethics signal source: {source_kind}")

        has_permission_scope = bool(permitted_purposes or permitted_audiences)
        if kind == "CONFIDENTIALITY_PERMISSION" and state == "GRANTED":
            if not permitted_purposes or not permitted_audiences:
                raise ValidationError(
                    "granted confidentiality permission requires purposes and audiences"
                )
        elif has_permission_scope:
            raise ValidationError(
                "permission scope is only valid for granted confidentiality permission"
            )

        if voluntary_confirmed and not (
            kind == "INFORMED_CONSENT" and state == "GRANTED"
        ):
            raise ValidationError(
                "voluntary_confirmed is only valid for granted informed consent"
            )

        object.__setattr__(self, "id", signal_id)
        object.__setattr__(self, "relationship_ref", relationship_ref)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "subject_ref", subject_ref)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "permitted_purposes", permitted_purposes)
        object.__setattr__(self, "permitted_audiences", permitted_audiences)
        object.__setattr__(self, "voluntary_confirmed", voluntary_confirmed)


@dataclass(frozen=True)
class ProfessionalEthicsAssessment:
    relationship_ref: str
    action_ref: str
    state: str
    evidence_ids: tuple[str, ...]
    unresolved_checks: tuple[str, ...]
    concern_kinds: tuple[str, ...]
    required_steps: tuple[str, ...]
    disclosure_required: bool
    independent_review_required: bool
    consent_satisfied: bool
    confidentiality_satisfied: bool
    attribution_consent_satisfied: bool
    safeguarding_escalation: bool
    permitted_disclosure_audiences: tuple[str, ...]
    reason: str
    action_authority_granted: bool = field(default=False, init=False)
    mutation_authorized: bool = field(default=False, init=False)
    external_action_authorized: bool = field(default=False, init=False)
    role_grants_authority: bool = field(default=False, init=False)
    legal_compliance_certified: bool = field(default=False, init=False)
    clinical_compliance_certified: bool = field(default=False, init=False)
    establishes_authorship_or_ownership: bool = field(default=False, init=False)
    reputation_score: None = field(default=None, init=False)
    authority_effect: str = field(default="UNCHANGED", init=False)
    persistence_effect: str = field(default="NONE", init=False)

    def __post_init__(self) -> None:
        if self.state not in ETHICS_ASSESSMENT_STATES:
            raise ValidationError(f"unsupported ethics assessment state: {self.state}")


def _expected_subject(context: ProfessionalEthicsContext, kind: str) -> str:
    if kind == "CONFLICT_OF_INTEREST":
        return context.actor_ref
    return context.affected_party_ref


def _latest_signals(
    context: ProfessionalEthicsContext,
    signals: tuple[ProfessionalEthicsSignal, ...],
) -> dict[str, ProfessionalEthicsSignal]:
    by_kind: dict[str, ProfessionalEthicsSignal] = {}
    for signal in signals:
        expected_subject = _expected_subject(context, signal.kind)
        if signal.subject_ref != expected_subject:
            raise ValidationError(
                f"ethics signal {signal.id} is bound to the wrong subject for {signal.kind}"
            )
        existing = by_kind.get(signal.kind)
        if existing is None or signal.observed_at > existing.observed_at:
            by_kind[signal.kind] = signal
        elif signal.observed_at == existing.observed_at and signal.id != existing.id:
            raise ValidationError(
                f"conflicting ethics signals share the latest timestamp for {signal.kind}"
            )
    return by_kind


def assess_professional_ethics(
    context: ProfessionalEthicsContext,
    signals: Iterable[ProfessionalEthicsSignal],
    *,
    as_of: datetime,
) -> ProfessionalEthicsAssessment:
    """Resolve explicit trust constraints without granting authority or certifying compliance."""

    _require_aware(as_of, field_name="as_of")
    rows = tuple(signals)
    ids = tuple(row.id for row in rows)
    if len(ids) != len(set(ids)):
        raise ValidationError("ethics signal IDs must be unique")

    foreign = tuple(
        row.id for row in rows if row.relationship_ref != context.relationship_ref
    )
    if foreign:
        raise ValidationError(
            "ethics signals belong to another relationship: " + ", ".join(foreign)
        )
    future = tuple(row.id for row in rows if row.observed_at > as_of)
    if future:
        raise ValidationError(
            "ethics signals cannot be observed after as_of: " + ", ".join(future)
        )

    latest = _latest_signals(context, rows)
    unresolved = tuple(
        check
        for check in context.required_checks
        if check not in latest or latest[check].state in {"UNKNOWN", "DISPUTED"}
    )

    conflict = latest.get("CONFLICT_OF_INTEREST")
    confidentiality = latest.get("CONFIDENTIALITY_PERMISSION")
    consent = latest.get("INFORMED_CONSENT")
    attribution = latest.get("ATTRIBUTION_CONSENT")
    power = latest.get("POWER_ASYMMETRY")
    safeguarding = latest.get("SAFEGUARDING_CONCERN")

    concerns: list[str] = list(unresolved)
    steps: list[str] = []
    state_rank = 0  # CLEAR=0, REVIEW=1, PAUSE=2, ESCALATE=3
    disclosure_required = False
    independent_review_required = bool(context.independent_review_domains)
    safeguarding_escalation = False

    for check in unresolved:
        steps.append(
            f"Obtain explicit source-bound evidence for required ethics check {check} before proceeding."
        )
        state_rank = max(state_rank, 2)

    if conflict and conflict.state == "PRESENT":
        concerns.append("CONFLICT_OF_INTEREST")
        disclosure_required = True
        steps.append(
            "Disclose the material conflict to the affected party before relying on the conflicted recommendation or decision."
        )
        state_rank = max(state_rank, 1)
        if context.impact_class in {"MATERIAL", "HIGH_STAKES"}:
            independent_review_required = True
            steps.append(
                "Use an unconflicted human or independent professional review for the material decision."
            )
            state_rank = max(state_rank, 2)

    confidentiality_satisfied = "CONFIDENTIALITY_PERMISSION" not in context.required_checks
    permitted_disclosure_audiences: tuple[str, ...] = ()
    if "CONFIDENTIALITY_PERMISSION" in context.required_checks:
        if confidentiality and confidentiality.state == "GRANTED":
            purpose_allowed = context.purpose in confidentiality.permitted_purposes
            audience_allowed = set(context.requested_audiences).issubset(
                set(confidentiality.permitted_audiences)
            )
            confidentiality_satisfied = purpose_allowed and audience_allowed
            if confidentiality_satisfied:
                permitted_disclosure_audiences = context.requested_audiences
            else:
                concerns.append("CONFIDENTIALITY_PERMISSION")
                steps.append(
                    "Limit disclosure to the explicitly permitted purpose and minimum necessary audience, or obtain new permission."
                )
                state_rank = max(state_rank, 2)
        elif confidentiality and confidentiality.state in {"WITHHELD", "WITHDRAWN"}:
            concerns.append("CONFIDENTIALITY_PERMISSION")
            steps.append(
                "Do not disclose the confidential material for this purpose or audience without new explicit permission."
            )
            state_rank = max(state_rank, 2)

    consent_satisfied = "INFORMED_CONSENT" not in context.required_checks
    if "INFORMED_CONSENT" in context.required_checks:
        if consent and consent.state == "GRANTED":
            consent_satisfied = True
            if power and power.state == "PRESENT" and not consent.voluntary_confirmed:
                consent_satisfied = False
                concerns.extend(("POWER_ASYMMETRY", "INFORMED_CONSENT"))
                steps.append(
                    "Reconfirm voluntary informed consent without pressure or role-based coercion before proceeding."
                )
                state_rank = max(state_rank, 2)
        elif consent and consent.state in {"WITHHELD", "WITHDRAWN", "COERCED"}:
            concerns.append("INFORMED_CONSENT")
            steps.append(
                "Do not proceed with the consent-dependent action unless the affected party gives valid voluntary informed consent."
            )
            state_rank = max(state_rank, 2)

    if power and power.state == "PRESENT":
        concerns.append("POWER_ASYMMETRY")
        if "INFORMED_CONSENT" not in context.required_checks:
            steps.append(
                "Treat the power asymmetry as context for disclosure and consent design; do not convert it into a risk score or inferred incapacity."
            )
            state_rank = max(state_rank, 1)

    attribution_consent_satisfied = "ATTRIBUTION_CONSENT" not in context.required_checks
    if "ATTRIBUTION_CONSENT" in context.required_checks:
        if attribution and attribution.state == "GRANTED":
            attribution_consent_satisfied = True
        elif attribution and attribution.state in {"WITHHELD", "WITHDRAWN"}:
            concerns.append("ATTRIBUTION_CONSENT")
            steps.append(
                "Do not publish or reuse the requested attribution until the contributor's attribution consent is current."
            )
            state_rank = max(state_rank, 2)

    if safeguarding and safeguarding.state == "PRESENT":
        concerns.append("SAFEGUARDING_CONCERN")
        safeguarding_escalation = True
        independent_review_required = True
        steps.insert(
            0,
            "Pause the affected workflow and route the safeguarding concern to the responsible human or specialist process.",
        )
        state_rank = 3

    if context.independent_review_domains:
        domains = ", ".join(context.independent_review_domains)
        steps.append(
            f"Route {domains} questions to the appropriate independent professional; N0TE does not certify compliance or replace that specialist."
        )
        state_rank = max(state_rank, 2)

    if independent_review_required and not any(
        "independent" in step.lower() or "specialist" in step.lower()
        for step in steps
    ):
        steps.append(
            "Obtain independent human or professional review before the material decision proceeds."
        )
        state_rank = max(state_rank, 2)

    concern_tuple = tuple(dict.fromkeys(concerns))
    step_tuple = tuple(dict.fromkeys(steps))
    state = ("CLEAR", "REVIEW", "PAUSE", "ESCALATE")[state_rank]

    if state == "CLEAR":
        reason = (
            "All explicitly required ethics checks are source-bound and satisfied; "
            "the assessment still grants no action authority."
        )
    elif state == "REVIEW":
        reason = (
            "Explicit ethics evidence raises a bounded disclosure or relationship-context "
            "concern that should be reviewed before relying on the result."
        )
    elif state == "PAUSE":
        reason = (
            "A required ethics check is missing, unsatisfied, disputed, outside permission "
            "scope, or requires independent review before the action proceeds."
        )
    else:
        reason = (
            "A source-bound safeguarding concern requires the affected workflow to pause "
            "and escalate to the responsible human or specialist process."
        )

    return ProfessionalEthicsAssessment(
        relationship_ref=context.relationship_ref,
        action_ref=context.action_ref,
        state=state,
        evidence_ids=ids,
        unresolved_checks=unresolved,
        concern_kinds=concern_tuple,
        required_steps=step_tuple,
        disclosure_required=disclosure_required,
        independent_review_required=independent_review_required,
        consent_satisfied=consent_satisfied,
        confidentiality_satisfied=confidentiality_satisfied,
        attribution_consent_satisfied=attribution_consent_satisfied,
        safeguarding_escalation=safeguarding_escalation,
        permitted_disclosure_audiences=permitted_disclosure_audiences,
        reason=reason,
    )
