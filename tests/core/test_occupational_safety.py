from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from n0te.lineage import ValidationError
from n0te.occupational_safety import (
    OccupationalSafetyContext,
    SafetyCue,
    SafetyGuidance,
    assess_occupational_safety,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def context(
    *,
    role: str = "Mix Engineer",
    work_kind: str = "MIX_MASTER",
    quiet_requested: bool = False,
) -> OccupationalSafetyContext:
    return OccupationalSafetyContext(
        role=role,
        work_kind=work_kind,
        context_ref=f"work:{role}:{work_kind}",
        quiet_requested=quiet_requested,
    )


def cue(
    *,
    cue_id: str = "cue-1",
    domain: str = "HEARING",
    kind: str = "SOUND_EXPOSURE",
    source_kind: str = "USER_DECLARED",
    urgency: str = "REVIEW",
    observed_at: datetime = NOW,
) -> SafetyCue:
    return SafetyCue(
        id=cue_id,
        domain=domain,
        kind=kind,
        statement="Bounded fixture context; not a diagnosis.",
        source_kind=source_kind,
        source_ref=f"source:{cue_id}",
        observed_at=observed_at,
        urgency=urgency,
    )


def guidance(
    *,
    guidance_id: str = "guide-1",
    domain: str = "HEARING",
    source_kind: str = "AUTHORITATIVE_EXTERNAL",
    observed_at: datetime = NOW - timedelta(days=1),
    revalidate_after: datetime = NOW + timedelta(days=30),
    roles: tuple[str, ...] = (),
    work_kinds: tuple[str, ...] = (),
) -> SafetyGuidance:
    return SafetyGuidance(
        id=guidance_id,
        domain=domain,
        statement="Fixture external guidance; product tests never encode a numeric safe limit.",
        source_kind=source_kind,
        source_ref=f"external:{guidance_id}",
        observed_at=observed_at,
        revalidate_after=revalidate_after,
        applies_to_roles=roles,
        applies_to_work_kinds=work_kinds,
    )


def test_no_evidence_stays_unknown_and_never_certifies_safety() -> None:
    assessment = assess_occupational_safety(context(), (), as_of=NOW)

    assert assessment.state == "UNKNOWN"
    assert assessment.cue_ids == ()
    assert assessment.certifies_safe_exposure is False
    assert assessment.clinical_diagnosis is None
    assert assessment.external_action_authorized is False
    assert assessment.mutation_authorized is False
    assert assessment.authority_effect == "UNCHANGED"
    assert assessment.persistence_effect == "NONE"


def test_measured_hearing_context_needs_fresh_guidance_without_inventing_limit() -> None:
    assessment = assess_occupational_safety(
        context(),
        (cue(source_kind="MEASURED"),),
        as_of=NOW,
    )

    assert assessment.state == "REVIEW"
    assert assessment.needs_fresh_guidance is True
    assert assessment.certifies_safe_exposure is False
    copy = " ".join(assessment.suggested_actions).lower()
    assert "fresh authoritative guidance" in copy
    assert "universal safe limit" in copy
    assert "db" not in copy
    assert "minutes at" not in copy


def test_fresh_verified_guidance_is_usable_but_does_not_certify() -> None:
    assessment = assess_occupational_safety(
        context(),
        (cue(source_kind="MEASURED"),),
        guidance=(guidance(),),
        as_of=NOW,
    )

    assert assessment.needs_fresh_guidance is False
    assert assessment.guidance_resolutions[0].state == "APPLICABLE"
    assert assessment.guidance_resolutions[0].usable_as_current_guidance is True
    assert assessment.certifies_safe_exposure is False
    assert assessment.authority_effect == "UNCHANGED"


def test_stale_or_user_declared_guidance_cannot_become_current_rule() -> None:
    stale = guidance(
        guidance_id="stale",
        revalidate_after=NOW,
    )
    declared = guidance(
        guidance_id="declared",
        source_kind="USER_DECLARED",
    )
    assessment = assess_occupational_safety(
        context(),
        (cue(source_kind="MEASURED"),),
        guidance=(stale, declared),
        as_of=NOW,
    )

    states = {row.guidance_id: row.state for row in assessment.guidance_resolutions}
    assert states == {"stale": "STALE", "declared": "UNVERIFIED"}
    assert assessment.needs_fresh_guidance is True


def test_role_and_work_scope_prevent_unrelated_guidance_from_applying() -> None:
    assessment = assess_occupational_safety(
        context(role="Producer", work_kind="STUDIO"),
        (cue(source_kind="MEASURED"),),
        guidance=(
            guidance(
                roles=("Mix Engineer",),
                work_kinds=("MIX_MASTER",),
            ),
        ),
        as_of=NOW,
    )

    assert assessment.guidance_resolutions[0].state == "OUT_OF_SCOPE"
    assert assessment.needs_fresh_guidance is True


def test_fatigue_creates_break_recovery_plan_without_claiming_diagnosis() -> None:
    assessment = assess_occupational_safety(
        context(role="Producer", work_kind="STUDIO"),
        (
            cue(
                domain="FATIGUE",
                kind="FATIGUE",
                urgency="REVIEW",
            ),
        ),
        as_of=NOW,
    )

    assert assessment.state == "REVIEW"
    assert assessment.clinical_diagnosis is None
    assert any("break or recovery plan" in action for action in assessment.suggested_actions)


def test_accessibility_preference_is_respected_without_medical_inference() -> None:
    assessment = assess_occupational_safety(
        context(role="Session Musician", work_kind="SESSION_PERFORMANCE"),
        (
            cue(
                domain="ACCESSIBILITY",
                kind="ACCESSIBILITY_NEED",
                urgency="ROUTINE",
            ),
        ),
        as_of=NOW,
    )

    assert assessment.state == "MONITOR"
    assert assessment.must_interrupt is False
    assert assessment.clinical_diagnosis is None
    assert assessment.suggested_actions == (
        "Apply the stated accessibility preference without inferring a medical condition.",
    )


def test_incident_escalation_outranks_quiet_mode_convenience() -> None:
    assessment = assess_occupational_safety(
        context(role="Live Engineer", work_kind="LIVE", quiet_requested=True),
        (
            cue(
                domain="LIVE",
                kind="INCIDENT",
                urgency="HUMAN_SUPPORT",
            ),
        ),
        as_of=NOW,
    )

    assert assessment.state == "ESCALATE"
    assert assessment.must_interrupt is True
    assert "Pause the affected work" in assessment.suggested_actions[0]
    assert assessment.external_action_authorized is False


def test_future_cues_and_duplicate_ids_fail_closed() -> None:
    future = cue(observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValidationError, match="cannot be observed after as_of"):
        assess_occupational_safety(context(), (future,), as_of=NOW)

    row = cue()
    with pytest.raises(ValidationError, match="safety cue IDs must be unique"):
        assess_occupational_safety(context(), (row, row), as_of=NOW)


def test_malformed_timestamps_and_boolean_quiet_state_are_rejected() -> None:
    with pytest.raises(ValidationError, match="quiet_requested must be boolean"):
        OccupationalSafetyContext(
            role="Producer",
            work_kind="STUDIO",
            context_ref="work:1",
            quiet_requested=1,  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError, match="timezone-aware"):
        cue(observed_at=datetime(2026, 9, 4, 12, 0))
