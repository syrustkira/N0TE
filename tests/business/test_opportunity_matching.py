from __future__ import annotations

from pathlib import Path

import pytest

from n0te.lineage import NotFoundError, ValidationError
from n0te.memory import HeadquartersMemory
from n0te.opportunities import BusinessOpportunityService
from n0te.opportunity_matching import (
    OPPORTUNITY_MATCH_DIMENSIONS,
    OpportunityMatchAssessment,
    OpportunityMatchCriterion,
    OpportunityMatchingService,
)


def _opportunities(hq: HeadquartersMemory) -> BusinessOpportunityService:
    return BusinessOpportunityService(hq.store, hq.evidence, hq.people)


def _matching(hq: HeadquartersMemory) -> OpportunityMatchingService:
    return OpportunityMatchingService(hq.store, hq.evidence, _opportunities(hq))


def _claim(
    hq: HeadquartersMemory,
    *,
    key: str,
    value: object,
    scope_kind: str = "ARTIST",
    scope_id: str | None = None,
    source_kind: str = "USER_DECLARED",
    source_ref: str | None = None,
    supersedes: tuple[str, ...] = (),
):
    if scope_id is None:
        if scope_kind == "PROFILE":
            scope_id = hq.store.profile_id
        elif scope_kind == "ARTIST":
            scope_id = hq.store.primary_artist_id
        else:
            raise AssertionError("explicit scope_id required for Song/Version evidence")
    return hq.evidence.record_claim(
        scope_kind=scope_kind,
        scope_id=scope_id,
        key=key,
        value=value,
        source_kind=source_kind,
        source_ref=source_ref,
        twin_domain="UNSPECIFIED",
        supersedes=supersedes,
    )


def _opportunity(
    hq: HeadquartersMemory,
    *,
    song_id: str | None = None,
    summary: str = "Consider this opportunity",
):
    source = _claim(
        hq,
        key="business.source.match-test",
        value={"listing": summary},
        scope_kind="SONG" if song_id is not None else "ARTIST",
        scope_id=song_id,
    )
    opportunity = _opportunities(hq).create(
        kind="PITCH",
        summary=summary,
        source_claim_id=source.id,
        song_id=song_id,
        deadline_on="2026-10-15",
    )
    return source, opportunity


def _dimension_claims(hq: HeadquartersMemory, *, song_id: str | None = None):
    claims = {}
    for dimension in OPPORTUNITY_MATCH_DIMENSIONS:
        claims[dimension] = _claim(
            hq,
            key=f"business.match.{dimension.lower()}",
            value={"dimension": dimension, "state": "supported"},
            scope_kind="SONG" if song_id is not None else "ARTIST",
            scope_id=song_id,
        )
    return claims


def _satisfied_criteria(claims) -> tuple[OpportunityMatchCriterion, ...]:
    return tuple(
        OpportunityMatchCriterion(
            dimension=dimension,
            state="SATISFIED",
            reason=f"Current evidence supports {dimension.lower()} for this review.",
            basis_claim_ids=(claims[dimension].id,),
        )
        for dimension in OPPORTUNITY_MATCH_DIMENSIONS
    )


def test_complete_current_evidence_is_ready_to_decide_without_scores_or_authority(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Match Artist")
    try:
        _, opportunity = _opportunity(hq)
        claims = _dimension_claims(hq)

        assessment = _matching(hq).assess(
            opportunity.id,
            _satisfied_criteria(claims),
        )

        assert assessment.disposition == "READY_TO_DECIDE"
        assert assessment.assessment_truth_kind == "INFERRED"
        assert assessment.authority == "ADVISE_ONLY"
        assert tuple(item.dimension for item in assessment.dimensions) == OPPORTUNITY_MATCH_DIMENSIONS
        assert all(item.state == "SATISFIED" for item in assessment.dimensions)
        assert all(item.assessment_truth_kind == "INFERRED" for item in assessment.dimensions)
        assert all(item.basis[0].source_kind == "USER_DECLARED" for item in assessment.dimensions)
        assert assessment.actionable_gaps == ()
        assert assessment.fit_score is None
        assert assessment.readiness_score is None
        assert assessment.value_score is None
        assert assessment.cost_score is None
        assert assessment.priority_score is None
        assert assessment.predicted_success is None
        assert assessment.application_authority_granted is False
        assert assessment.messaging_authority_granted is False
        assert assessment.acceptance_authority_granted is False
        assert assessment.contract_authority_granted is False
        assert assessment.payment_authority_granted is False
        assert assessment.scheduling_authority_granted is False
        assert assessment.publication_authority_granted is False
        assert assessment.provider_authority_granted is False
        assert assessment.external_action_authority_granted is False
    finally:
        hq.close()


def test_missing_dimensions_stay_unknown_and_become_actionable_gaps_not_zeroes(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Unknown Artist")
    try:
        _, opportunity = _opportunity(hq)
        fit = _claim(
            hq,
            key="business.match.fit",
            value={"fit": "artist says yes"},
        )

        assessment = _matching(hq).assess(
            opportunity.id,
            (
                OpportunityMatchCriterion(
                    dimension="FIT",
                    state="SATISFIED",
                    reason="Artist-declared fit is available.",
                    basis_claim_ids=(fit.id,),
                ),
            ),
        )

        states = {item.dimension: item.state for item in assessment.dimensions}
        assert states["FIT"] == "SATISFIED"
        assert all(states[item] == "UNKNOWN" for item in OPPORTUNITY_MATCH_DIMENSIONS[1:])
        assert assessment.disposition == "INSUFFICIENT_EVIDENCE"
        assert len(assessment.actionable_gaps) == 5
        assert any("money, time, effort" in item for item in assessment.actionable_gaps)
        assert assessment.priority_score is None
        assert assessment.predicted_success is None
    finally:
        hq.close()


def test_known_gap_returns_close_gaps_with_explicit_next_action(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Gap Artist")
    try:
        _, opportunity = _opportunity(hq)
        claims = _dimension_claims(hq)
        criteria = list(_satisfied_criteria(claims))
        readiness = claims["READINESS"]
        criteria[1] = OpportunityMatchCriterion(
            dimension="READINESS",
            state="GAP",
            reason="The requested instrumental deliverable is not ready yet.",
            basis_claim_ids=(readiness.id,),
            next_action="Prepare and verify the instrumental deliverable before deciding.",
        )

        assessment = _matching(hq).assess(opportunity.id, criteria)

        assert assessment.disposition == "CLOSE_GAPS"
        result = assessment.dimensions[1]
        assert result.dimension == "READINESS"
        assert result.state == "GAP"
        assert result.next_action == "Prepare and verify the instrumental deliverable before deciding."
        assert assessment.actionable_gaps == (result.next_action,)
    finally:
        hq.close()


def test_current_explicit_blocker_outranks_other_conflict_without_granting_action(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Blocked Artist")
    try:
        _, opportunity = _opportunity(hq)
        claims = _dimension_claims(hq)
        criteria = list(_satisfied_criteria(claims))
        conflict_other = _claim(
            hq,
            key="business.match.value.other",
            value={"value": "unclear"},
        )
        criteria[2] = OpportunityMatchCriterion(
            dimension="VALUE",
            state="CONFLICT",
            reason="Two current inputs disagree about the value.",
            basis_claim_ids=(claims["VALUE"].id, conflict_other.id),
        )
        criteria[3] = OpportunityMatchCriterion(
            dimension="COST",
            state="BLOCKED",
            reason="The required cost exceeds the currently authorized budget.",
            basis_claim_ids=(claims["COST"].id,),
            next_action="Do not spend; revisit only if the budget context changes.",
        )

        assessment = _matching(hq).assess(opportunity.id, criteria)

        assert assessment.disposition == "BLOCKED"
        assert assessment.external_action_authority_granted is False
        assert "Do not spend" in assessment.actionable_gaps[-1]
    finally:
        hq.close()


def test_not_applicable_cannot_be_asserted_without_basis_evidence() -> None:
    with pytest.raises(ValidationError, match="NOT_APPLICABLE.*requires basis evidence"):
        OpportunityMatchCriterion(
            dimension="COST",
            state="NOT_APPLICABLE",
            reason="Assume there is no cost.",
        )


def test_stale_basis_taints_only_that_dimension_and_requires_revalidation(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Stale Basis Artist")
    try:
        _, opportunity = _opportunity(hq)
        claims = _dimension_claims(hq)
        stale = claims["VALUE"]
        replacement = _claim(
            hq,
            key=stale.key,
            value={"dimension": "VALUE", "state": "newer"},
            supersedes=(stale.id,),
        )
        assert replacement.id != stale.id

        assessment = _matching(hq).assess(
            opportunity.id,
            _satisfied_criteria(claims),
        )

        states = {item.dimension: item.state for item in assessment.dimensions}
        assert states["VALUE"] == "NEEDS_REVALIDATION"
        assert all(
            states[dimension] == "SATISFIED"
            for dimension in OPPORTUNITY_MATCH_DIMENSIONS
            if dimension != "VALUE"
        )
        assert assessment.disposition == "NEEDS_REVALIDATION"
        assert any("value" in item.lower() and "revalidate" in item.lower() for item in assessment.actionable_gaps)
    finally:
        hq.close()


def test_stale_opportunity_source_taints_whole_assessment_even_with_fresh_match_basis(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Stale Opportunity Artist")
    try:
        source, opportunity = _opportunity(hq)
        claims = _dimension_claims(hq)
        _claim(
            hq,
            key=source.key,
            value={"listing": "replacement source"},
            supersedes=(source.id,),
        )

        assessment = _matching(hq).assess(
            opportunity.id,
            _satisfied_criteria(claims),
        )

        assert assessment.source_current is False
        assert assessment.disposition == "NEEDS_REVALIDATION"
        assert assessment.actionable_gaps[0].startswith("Revalidate the Opportunity source itself")
        assert all(item.state == "SATISFIED" for item in assessment.dimensions)
    finally:
        hq.close()


def test_conflict_requires_distinct_current_evidence_values(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Conflict Artist")
    try:
        _, opportunity = _opportunity(hq)
        first = _claim(hq, key="business.match.relationship.a", value={"access": "direct"})
        same = _claim(hq, key="business.match.relationship.b", value={"access": "direct"})
        different = _claim(hq, key="business.match.relationship.c", value={"access": "indirect"})
        service = _matching(hq)

        with pytest.raises(ValidationError, match="materially different evidence values"):
            service.assess(
                opportunity.id,
                (
                    OpportunityMatchCriterion(
                        dimension="RELATIONSHIP",
                        state="CONFLICT",
                        reason="These two inputs are claimed to disagree.",
                        basis_claim_ids=(first.id, same.id),
                    ),
                ),
            )

        assessment = service.assess(
            opportunity.id,
            (
                OpportunityMatchCriterion(
                    dimension="RELATIONSHIP",
                    state="CONFLICT",
                    reason="Current access evidence disagrees.",
                    basis_claim_ids=(first.id, different.id),
                ),
            ),
        )
        assert assessment.disposition == "REVIEW_CONFLICT"
        relationship = assessment.dimensions[-1]
        assert relationship.state == "CONFLICT"
        assert relationship.next_action is not None
        assert "Resolve the conflicting relationship evidence" in relationship.next_action
    finally:
        hq.close()


def test_song_scoped_basis_cannot_cross_opportunity_context(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Song Scope Artist")
    try:
        song_a = hq.store.create_song("Song A")
        song_b = hq.store.create_song("Song B")
        _, opportunity = _opportunity(hq, song_id=song_a.id)
        foreign = _claim(
            hq,
            key="business.match.fit.foreign",
            value={"fit": True},
            scope_kind="SONG",
            scope_id=song_b.id,
        )

        with pytest.raises(ValidationError, match="scope does not match"):
            _matching(hq).assess(
                opportunity.id,
                (
                    OpportunityMatchCriterion(
                        dimension="FIT",
                        state="SATISFIED",
                        reason="Wrong Song evidence must not cross over.",
                        basis_claim_ids=(foreign.id,),
                    ),
                ),
            )
    finally:
        hq.close()


def test_observed_match_basis_requires_provenance(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Provenance Match Artist")
    try:
        _, opportunity = _opportunity(hq)
        observed = _claim(
            hq,
            key="business.match.deadline.observed",
            value={"deadline": "2026-10-15"},
            source_kind="OBSERVED",
        )

        with pytest.raises(ValidationError, match="OBSERVED evidence requires source_ref provenance"):
            _matching(hq).assess(
                opportunity.id,
                (
                    OpportunityMatchCriterion(
                        dimension="DEADLINE",
                        state="SATISFIED",
                        reason="Observed timing needs provenance.",
                        basis_claim_ids=(observed.id,),
                    ),
                ),
            )
    finally:
        hq.close()


def test_opportunity_representation_cannot_recursively_become_match_basis(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Recursive Match Artist")
    try:
        _, opportunity = _opportunity(hq)

        with pytest.raises(ValidationError, match="cannot recursively become matching evidence"):
            _matching(hq).assess(
                opportunity.id,
                (
                    OpportunityMatchCriterion(
                        dimension="FIT",
                        state="SATISFIED",
                        reason="An Opportunity representation is not independent fit evidence.",
                        basis_claim_ids=(opportunity.representation_claim_id,),
                    ),
                ),
            )
    finally:
        hq.close()


def test_assessment_is_read_only_and_does_not_create_evidence_or_work_items(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Read Only Match Artist")
    try:
        _, opportunity = _opportunity(hq)
        claims = _dimension_claims(hq)
        before_evidence = int(
            hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0]
        )
        before_followups = hq.people.followups()

        assessment = _matching(hq).assess(
            opportunity.id,
            _satisfied_criteria(claims),
        )

        after_evidence = int(
            hq.store._conn.execute("SELECT COUNT(*) FROM evidence_claims").fetchone()[0]
        )
        assert assessment.disposition == "READY_TO_DECIDE"
        assert after_evidence == before_evidence
        assert hq.people.followups() == before_followups
        assert _opportunities(hq).get(opportunity.id) == opportunity
    finally:
        hq.close()


def test_review_grouping_is_qualitative_and_same_band_order_is_serialization_only(
    tmp_path: Path,
) -> None:
    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Review Band Artist")
    try:
        _, first = _opportunity(hq, summary="First listing")
        source = _claim(
            hq,
            key="business.source.match-test.second",
            value={"listing": "Second listing"},
        )
        second = _opportunities(hq).create(
            kind="GRANT",
            summary="Second listing",
            source_claim_id=source.id,
        )
        service = _matching(hq)
        first_assessment = service.assess(first.id, ())
        second_assessment = service.assess(second.id, ())

        bands = service.group_for_review((second_assessment, first_assessment))

        assert len(bands) == 1
        assert bands[0].disposition == "INSUFFICIENT_EVIDENCE"
        assert tuple(item.opportunity_id for item in bands[0].assessments) == tuple(
            sorted((first.id, second.id))
        )
        assert all(item.priority_score is None for item in bands[0].assessments)
        assert all(item.predicted_success is None for item in bands[0].assessments)
    finally:
        hq.close()


def test_input_boundaries_and_duplicate_dimensions_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="dimension must be text"):
        OpportunityMatchCriterion(  # type: ignore[arg-type]
            dimension=True,
            state="UNKNOWN",
            reason="bad",
        )
    with pytest.raises(ValidationError, match="state must be text"):
        OpportunityMatchCriterion(  # type: ignore[arg-type]
            dimension="FIT",
            state=True,
            reason="bad",
        )
    with pytest.raises(ValidationError, match="tuple of evidence claim ids"):
        OpportunityMatchCriterion(  # type: ignore[arg-type]
            dimension="FIT",
            state="SATISFIED",
            reason="bad",
            basis_claim_ids=["claim"],
        )

    hq = HeadquartersMemory.create((tmp_path / "data").resolve(), "Boundary Match Artist")
    try:
        _, opportunity = _opportunity(hq)
        claim = _claim(hq, key="business.match.fit.boundary", value={"fit": True})
        criterion = OpportunityMatchCriterion(
            dimension="FIT",
            state="SATISFIED",
            reason="one",
            basis_claim_ids=(claim.id,),
        )
        with pytest.raises(ValidationError, match="duplicate opportunity match criterion"):
            _matching(hq).assess(opportunity.id, (criterion, criterion))
        with pytest.raises(ValidationError, match="criteria contain an invalid item"):
            _matching(hq).assess(opportunity.id, (criterion, "bad"))  # type: ignore[arg-type]
        with pytest.raises(NotFoundError, match="business opportunity not found"):
            _matching(hq).assess("opportunity_missing", ())
        with pytest.raises(ValidationError, match="assessments contain an invalid item"):
            _matching(hq).group_for_review(("bad",))  # type: ignore[arg-type]
    finally:
        hq.close()


def test_authority_fields_cannot_be_forged_through_constructor() -> None:
    with pytest.raises(TypeError):
        OpportunityMatchAssessment(  # type: ignore[call-arg]
            opportunity_id="opportunity_x",
            opportunity_kind="PITCH",
            song_id=None,
            person_id=None,
            source_current=True,
            disposition="INSUFFICIENT_EVIDENCE",
            dimensions=(),
            actionable_gaps=(),
            external_action_authority_granted=True,
        )
