from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageStore, NotFoundError, ValidationError
from .opportunities import (
    CAPTURE_OPPORTUNITY_KEY_PREFIX,
    OPPORTUNITY_KEY_PREFIX,
    PROVENANCE_REQUIRED,
    BusinessOpportunity,
    BusinessOpportunityService,
)

OPPORTUNITY_MATCH_DIMENSIONS = (
    "FIT",
    "READINESS",
    "VALUE",
    "COST",
    "DEADLINE",
    "RELATIONSHIP",
)
OPPORTUNITY_MATCH_STATES = {
    "SATISFIED",
    "GAP",
    "BLOCKED",
    "UNKNOWN",
    "CONFLICT",
    "NOT_APPLICABLE",
}
OPPORTUNITY_MATCH_RESOLVED_STATES = OPPORTUNITY_MATCH_STATES | {"NEEDS_REVALIDATION"}
OPPORTUNITY_MATCH_DISPOSITIONS = {
    "READY_TO_DECIDE",
    "CLOSE_GAPS",
    "REVIEW_CONFLICT",
    "NEEDS_REVALIDATION",
    "INSUFFICIENT_EVIDENCE",
    "BLOCKED",
}
OPPORTUNITY_MATCH_AUTHORITY = "ADVISE_ONLY"

_DEFAULT_NEXT_ACTION = {
    "FIT": "Gather explicit Artist or Song fit evidence for this opportunity.",
    "READINESS": "Confirm what is ready and which deliverables or prerequisites remain.",
    "VALUE": "Clarify the concrete value or upside before prioritizing this opportunity.",
    "COST": "Clarify the money, time, effort, or tradeoffs required.",
    "DEADLINE": "Confirm the current deadline and whether the required work can be completed in time.",
    "RELATIONSHIP": "Confirm the relationship or access context and any dependency on the relevant person or organization.",
}

_REVIEW_ORDER = (
    "READY_TO_DECIDE",
    "CLOSE_GAPS",
    "REVIEW_CONFLICT",
    "NEEDS_REVALIDATION",
    "INSUFFICIENT_EVIDENCE",
    "BLOCKED",
)


def _clean_text(value: str, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text")
    text = " ".join(value.split())
    if not text:
        raise ValidationError(f"{field_name} must not be empty")
    if len(text) > maximum:
        raise ValidationError(f"{field_name} is too long")
    return text


def _normalize_dimension(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("opportunity match dimension must be text")
    dimension = value.strip().upper().replace("-", "_").replace(" ", "_")
    if dimension not in OPPORTUNITY_MATCH_DIMENSIONS:
        raise ValidationError(f"unsupported opportunity match dimension: {dimension}")
    return dimension


def _normalize_state(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("opportunity match state must be text")
    state = value.strip().upper().replace("-", "_").replace(" ", "_")
    if state not in OPPORTUNITY_MATCH_STATES:
        raise ValidationError(f"unsupported opportunity match state: {state}")
    return state


def _claim_value_fingerprint(claim: EvidenceClaim) -> str:
    try:
        return json.dumps(
            claim.value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "opportunity match evidence value is not canonically comparable"
        ) from exc


@dataclass(frozen=True)
class OpportunityMatchCriterion:
    """One explicit assessment judgment with exact EvidenceMemory basis.

    The criterion is an assessment input, not new evidence. Its state is always
    interpreted as a provisional inference over the referenced evidence.
    """

    dimension: str
    state: str
    reason: str
    basis_claim_ids: tuple[str, ...] = ()
    next_action: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimension", _normalize_dimension(self.dimension))
        object.__setattr__(self, "state", _normalize_state(self.state))
        object.__setattr__(
            self,
            "reason",
            _clean_text(self.reason, "reason", maximum=800),
        )
        if isinstance(self.basis_claim_ids, (str, bytes)) or not isinstance(
            self.basis_claim_ids,
            tuple,
        ):
            raise ValidationError(
                "basis_claim_ids must be a tuple of evidence claim ids"
            )
        normalized_ids: list[str] = []
        for claim_id in self.basis_claim_ids:
            if not isinstance(claim_id, str) or not claim_id.strip():
                raise ValidationError("basis claim id must be non-empty text")
            normalized_ids.append(claim_id.strip())
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValidationError("opportunity match basis claims must be unique")
        object.__setattr__(self, "basis_claim_ids", tuple(normalized_ids))
        if self.state != "UNKNOWN" and not normalized_ids:
            raise ValidationError(
                f"{self.state} opportunity match state requires basis evidence"
            )
        if self.next_action is not None:
            object.__setattr__(
                self,
                "next_action",
                _clean_text(self.next_action, "next_action", maximum=500),
            )


@dataclass(frozen=True)
class OpportunityMatchBasis:
    claim_id: str
    scope_kind: str
    scope_id: str
    key: str
    source_kind: str
    source_ref: str | None
    current: bool


@dataclass(frozen=True)
class OpportunityMatchDimensionResult:
    dimension: str
    state: str
    reason: str
    basis: tuple[OpportunityMatchBasis, ...]
    next_action: str | None
    assessment_truth_kind: str = field(default="INFERRED", init=False)
    authority: str = field(default=OPPORTUNITY_MATCH_AUTHORITY, init=False)
    action_authority_granted: bool = field(default=False, init=False)
    external_action_authority_granted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.dimension not in OPPORTUNITY_MATCH_DIMENSIONS:
            raise ValidationError(
                f"invalid opportunity match result dimension: {self.dimension}"
            )
        if self.state not in OPPORTUNITY_MATCH_RESOLVED_STATES:
            raise ValidationError(
                f"invalid opportunity match result state: {self.state}"
            )


@dataclass(frozen=True)
class OpportunityMatchAssessment:
    opportunity_id: str
    opportunity_kind: str
    song_id: str | None
    person_id: str | None
    source_current: bool
    disposition: str
    dimensions: tuple[OpportunityMatchDimensionResult, ...]
    actionable_gaps: tuple[str, ...]
    assessment_truth_kind: str = field(default="INFERRED", init=False)
    authority: str = field(default=OPPORTUNITY_MATCH_AUTHORITY, init=False)
    application_authority_granted: bool = field(default=False, init=False)
    messaging_authority_granted: bool = field(default=False, init=False)
    acceptance_authority_granted: bool = field(default=False, init=False)
    contract_authority_granted: bool = field(default=False, init=False)
    payment_authority_granted: bool = field(default=False, init=False)
    purchase_authority_granted: bool = field(default=False, init=False)
    scheduling_authority_granted: bool = field(default=False, init=False)
    publication_authority_granted: bool = field(default=False, init=False)
    provider_authority_granted: bool = field(default=False, init=False)
    external_action_authority_granted: bool = field(default=False, init=False)
    fit_score: None = field(default=None, init=False)
    readiness_score: None = field(default=None, init=False)
    value_score: None = field(default=None, init=False)
    cost_score: None = field(default=None, init=False)
    priority_score: None = field(default=None, init=False)
    predicted_success: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.disposition not in OPPORTUNITY_MATCH_DISPOSITIONS:
            raise ValidationError(
                f"invalid opportunity match disposition: {self.disposition}"
            )
        if tuple(item.dimension for item in self.dimensions) != OPPORTUNITY_MATCH_DIMENSIONS:
            raise ValidationError(
                "opportunity match assessment must contain all canonical dimensions"
            )


@dataclass(frozen=True)
class OpportunityReviewBand:
    disposition: str
    assessments: tuple[OpportunityMatchAssessment, ...]

    def __post_init__(self) -> None:
        if self.disposition not in OPPORTUNITY_MATCH_DISPOSITIONS:
            raise ValidationError(
                f"invalid opportunity review disposition: {self.disposition}"
            )
        if any(item.disposition != self.disposition for item in self.assessments):
            raise ValidationError(
                "opportunity review band contains a mismatched assessment"
            )


class OpportunityMatchingService:
    """Pure evidence-bound Opportunity evaluation and review grouping.

    This service never writes EvidenceMemory, mutates Opportunity objects, calls
    providers, or grants consequential authority. It converts explicit assessment
    criteria into provisional, source-bound advice while preserving uncertainty.
    """

    def __init__(
        self,
        store: LineageStore,
        evidence: EvidenceMemory,
        opportunities: BusinessOpportunityService,
    ):
        if not isinstance(store, LineageStore):
            raise TypeError(
                "OpportunityMatchingService requires canonical LineageStore"
            )
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError(
                "OpportunityMatchingService requires EvidenceMemory on the same LineageStore"
            )
        if (
            not isinstance(opportunities, BusinessOpportunityService)
            or opportunities.store is not store
        ):
            raise TypeError(
                "OpportunityMatchingService requires BusinessOpportunityService on the same LineageStore"
            )
        self.store = store
        self.evidence = evidence
        self.opportunities = opportunities

    def _claim_is_active(self, claim: EvidenceClaim) -> bool:
        return any(
            active.id == claim.id
            for active in self.evidence.active_claims(
                claim.scope_kind,
                claim.scope_id,
                claim.key,
            )
        )

    def _claim_matches_opportunity(
        self,
        claim: EvidenceClaim,
        opportunity: BusinessOpportunity,
    ) -> bool:
        if claim.scope_kind == "PROFILE":
            return claim.scope_id == self.store.profile_id
        if claim.scope_kind == "ARTIST":
            return claim.scope_id == self.store.primary_artist_id
        if claim.scope_kind == "SONG":
            return (
                opportunity.song_id is not None
                and claim.scope_id == opportunity.song_id
            )
        if claim.scope_kind == "VERSION":
            if opportunity.song_id is None:
                return False
            version = self.store.get_version(claim.scope_id)
            return version is not None and version.song_id == opportunity.song_id
        return False

    def _basis(
        self,
        opportunity: BusinessOpportunity,
        criterion: OpportunityMatchCriterion,
    ) -> tuple[OpportunityMatchBasis, ...]:
        rows: list[OpportunityMatchBasis] = []
        claims: list[EvidenceClaim] = []
        for claim_id in criterion.basis_claim_ids:
            claim = self.evidence.get_claim(claim_id)
            if claim is None:
                raise NotFoundError(
                    f"opportunity match evidence claim not found: {claim_id}"
                )
            if claim.key.startswith(OPPORTUNITY_KEY_PREFIX) or claim.key.startswith(
                CAPTURE_OPPORTUNITY_KEY_PREFIX
            ):
                raise ValidationError(
                    "opportunity representations cannot recursively become matching evidence"
                )
            if claim.source_kind in PROVENANCE_REQUIRED and not (
                isinstance(claim.source_ref, str) and claim.source_ref.strip()
            ):
                raise ValidationError(
                    f"opportunity match {claim.source_kind} evidence requires source_ref provenance"
                )
            if not self._claim_matches_opportunity(claim, opportunity):
                raise ValidationError(
                    "opportunity match evidence scope does not match the Opportunity context"
                )
            current = self._claim_is_active(claim)
            claims.append(claim)
            rows.append(
                OpportunityMatchBasis(
                    claim_id=claim.id,
                    scope_kind=claim.scope_kind,
                    scope_id=claim.scope_id,
                    key=claim.key,
                    source_kind=claim.source_kind,
                    source_ref=claim.source_ref,
                    current=current,
                )
            )
        if criterion.state == "CONFLICT":
            if len(claims) < 2:
                raise ValidationError(
                    "CONFLICT opportunity match state requires at least two basis claims"
                )
            fingerprints = {_claim_value_fingerprint(claim) for claim in claims}
            if len(fingerprints) < 2:
                raise ValidationError(
                    "CONFLICT opportunity match state requires materially different evidence values"
                )
        return tuple(rows)

    @staticmethod
    def _resolved_state(
        criterion: OpportunityMatchCriterion,
        basis: tuple[OpportunityMatchBasis, ...],
    ) -> str:
        if any(not item.current for item in basis):
            return "NEEDS_REVALIDATION"
        return criterion.state

    @staticmethod
    def _next_action(
        dimension: str,
        state: str,
        requested: str | None,
    ) -> str | None:
        if state in {"SATISFIED", "NOT_APPLICABLE"}:
            return None
        if requested is not None:
            return requested
        if state == "NEEDS_REVALIDATION":
            return (
                f"Revalidate the evidence used for {dimension.lower()} before relying on this judgment."
            )
        if state == "CONFLICT":
            return (
                f"Resolve the conflicting {dimension.lower()} evidence before relying on this judgment."
            )
        return _DEFAULT_NEXT_ACTION[dimension]

    def _dimension_result(
        self,
        opportunity: BusinessOpportunity,
        criterion: OpportunityMatchCriterion,
    ) -> OpportunityMatchDimensionResult:
        basis = self._basis(opportunity, criterion)
        state = self._resolved_state(criterion, basis)
        return OpportunityMatchDimensionResult(
            dimension=criterion.dimension,
            state=state,
            reason=criterion.reason,
            basis=basis,
            next_action=self._next_action(
                criterion.dimension,
                state,
                criterion.next_action,
            ),
        )

    @staticmethod
    def _missing_dimension(dimension: str) -> OpportunityMatchDimensionResult:
        return OpportunityMatchDimensionResult(
            dimension=dimension,
            state="UNKNOWN",
            reason="No explicit assessment evidence was supplied for this dimension.",
            basis=(),
            next_action=_DEFAULT_NEXT_ACTION[dimension],
        )

    @staticmethod
    def _disposition(
        opportunity: BusinessOpportunity,
        dimensions: tuple[OpportunityMatchDimensionResult, ...],
    ) -> str:
        if not opportunity.source_current:
            return "NEEDS_REVALIDATION"
        states = {item.state for item in dimensions}
        if "BLOCKED" in states:
            return "BLOCKED"
        if "CONFLICT" in states:
            return "REVIEW_CONFLICT"
        if "NEEDS_REVALIDATION" in states:
            return "NEEDS_REVALIDATION"
        if "GAP" in states:
            return "CLOSE_GAPS"
        if "UNKNOWN" in states:
            return "INSUFFICIENT_EVIDENCE"
        return "READY_TO_DECIDE"

    def assess(
        self,
        opportunity_id: str,
        criteria: Iterable[OpportunityMatchCriterion],
    ) -> OpportunityMatchAssessment:
        if not isinstance(opportunity_id, str) or not opportunity_id.strip():
            raise ValidationError("opportunity_id must be non-empty text")
        opportunity = self.opportunities.get(opportunity_id.strip())
        if opportunity is None:
            raise NotFoundError(
                f"business opportunity not found: {opportunity_id}"
            )
        if isinstance(criteria, (str, bytes)):
            raise ValidationError(
                "opportunity match criteria must be an iterable of criteria"
            )
        try:
            supplied = tuple(criteria)
        except TypeError as exc:
            raise ValidationError(
                "opportunity match criteria must be iterable"
            ) from exc
        if any(not isinstance(item, OpportunityMatchCriterion) for item in supplied):
            raise ValidationError(
                "opportunity match criteria contain an invalid item"
            )
        by_dimension: dict[str, OpportunityMatchCriterion] = {}
        for criterion in supplied:
            if criterion.dimension in by_dimension:
                raise ValidationError(
                    f"duplicate opportunity match criterion for {criterion.dimension}"
                )
            by_dimension[criterion.dimension] = criterion

        dimensions = tuple(
            self._dimension_result(opportunity, by_dimension[dimension])
            if dimension in by_dimension
            else self._missing_dimension(dimension)
            for dimension in OPPORTUNITY_MATCH_DIMENSIONS
        )
        disposition = self._disposition(opportunity, dimensions)
        actionable = tuple(
            item.next_action
            for item in dimensions
            if item.next_action is not None
        )
        if not opportunity.source_current:
            source_action = (
                "Revalidate the Opportunity source itself before relying on any match assessment."
            )
            actionable = (source_action,) + tuple(
                item for item in actionable if item != source_action
            )
        return OpportunityMatchAssessment(
            opportunity_id=opportunity.id,
            opportunity_kind=opportunity.kind,
            song_id=opportunity.song_id,
            person_id=opportunity.person_id,
            source_current=opportunity.source_current,
            disposition=disposition,
            dimensions=dimensions,
            actionable_gaps=actionable,
        )

    @staticmethod
    def group_for_review(
        assessments: Iterable[OpportunityMatchAssessment],
    ) -> tuple[OpportunityReviewBand, ...]:
        if isinstance(assessments, (str, bytes)):
            raise ValidationError(
                "opportunity assessments must be an iterable of assessments"
            )
        try:
            items = tuple(assessments)
        except TypeError as exc:
            raise ValidationError("opportunity assessments must be iterable") from exc
        if any(not isinstance(item, OpportunityMatchAssessment) for item in items):
            raise ValidationError(
                "opportunity assessments contain an invalid item"
            )
        ids = [item.opportunity_id for item in items]
        if len(ids) != len(set(ids)):
            raise ValidationError("duplicate Opportunity assessment identity")
        bands: list[OpportunityReviewBand] = []
        for disposition in _REVIEW_ORDER:
            members = tuple(
                sorted(
                    (
                        item
                        for item in items
                        if item.disposition == disposition
                    ),
                    key=lambda item: item.opportunity_id,
                )
            )
            if members:
                bands.append(
                    OpportunityReviewBand(
                        disposition=disposition,
                        assessments=members,
                    )
                )
        return tuple(bands)
