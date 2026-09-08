from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from n0te.lineage import ValidationError
from n0te.occupational_safety import (
    OccupationalSafetyAssessment,
    OccupationalSafetyContext,
    SafetyCue,
    SafetyGuidance,
    assess_occupational_safety,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_assessment_authority_fields_cannot_be_forged_through_constructor() -> None:
    with pytest.raises(TypeError):
        OccupationalSafetyAssessment(  # type: ignore[call-arg]
            context_ref="work:1",
            role="Producer",
            work_kind="STUDIO",
            state="MONITOR",
            cue_ids=(),
            domains=(),
            guidance_resolutions=(),
            suggested_actions=(),
            needs_fresh_guidance=False,
            must_interrupt=False,
            reason="fixture",
            external_action_authorized=True,
        )


def test_assessment_is_frozen_after_creation() -> None:
    context = OccupationalSafetyContext(
        role="Producer",
        work_kind="STUDIO",
        context_ref="work:1",
    )
    assessment = assess_occupational_safety(context, (), as_of=NOW)

    with pytest.raises(FrozenInstanceError):
        assessment.authority_effect = "GRANTED"  # type: ignore[misc]


def test_external_guidance_never_becomes_execution_or_medical_authority() -> None:
    context = OccupationalSafetyContext(
        role="Session Musician",
        work_kind="SESSION_PERFORMANCE",
        context_ref="session:1",
    )
    cue = SafetyCue(
        id="voice-load",
        domain="VOICE",
        kind="SAFE_LIMIT_QUESTION",
        statement="How much vocal load is safe?",
        source_kind="USER_DECLARED",
        source_ref="artist:question",
        observed_at=NOW,
        urgency="REVIEW",
    )
    guidance = SafetyGuidance(
        id="voice-guidance",
        domain="VOICE",
        statement="Fixture only. No threshold is embedded in product code.",
        source_kind="PROFESSIONAL_GUIDANCE",
        source_ref="professional:fixture",
        observed_at=NOW - timedelta(days=1),
        revalidate_after=NOW + timedelta(days=7),
        applies_to_roles=("Session Musician",),
        applies_to_work_kinds=("SESSION_PERFORMANCE",),
    )

    assessment = assess_occupational_safety(
        context,
        (cue,),
        guidance=(guidance,),
        as_of=NOW,
    )

    assert assessment.guidance_resolutions[0].state == "APPLICABLE"
    assert assessment.needs_fresh_guidance is False
    assert assessment.certifies_safe_exposure is False
    assert assessment.clinical_diagnosis is None
    assert assessment.external_action_authorized is False
    assert assessment.mutation_authorized is False
    assert assessment.authority_effect == "UNCHANGED"


def test_mental_wellbeing_cue_routes_support_without_diagnosis() -> None:
    context = OccupationalSafetyContext(
        role="Artist",
        work_kind="CREATIVE",
        context_ref="creative:1",
        quiet_requested=True,
    )
    cue = SafetyCue(
        id="wellbeing",
        domain="MENTAL_WELLBEING",
        kind="WELLBEING_CONCERN",
        statement="The artist explicitly requested human support.",
        source_kind="USER_DECLARED",
        source_ref="artist:request",
        observed_at=NOW,
        urgency="HUMAN_SUPPORT",
    )

    assessment = assess_occupational_safety(context, (cue,), as_of=NOW)

    assert assessment.state == "ESCALATE"
    assert assessment.must_interrupt is True
    assert assessment.clinical_diagnosis is None
    text = " ".join(assessment.suggested_actions).lower()
    assert "human/professional support" in text
    assert "diagnos" in text


def test_guidance_with_future_observation_is_not_current() -> None:
    context = OccupationalSafetyContext(
        role="Mix Engineer",
        work_kind="MIX_MASTER",
        context_ref="mix:1",
    )
    cue = SafetyCue(
        id="hearing",
        domain="HEARING",
        kind="SOUND_EXPOSURE",
        statement="Measured exposure exists.",
        source_kind="MEASURED",
        source_ref="meter:fixture",
        observed_at=NOW,
        urgency="REVIEW",
    )
    future = SafetyGuidance(
        id="future-guide",
        domain="HEARING",
        statement="Fixture future guidance.",
        source_kind="AUTHORITATIVE_EXTERNAL",
        source_ref="authority:fixture",
        observed_at=NOW + timedelta(hours=1),
        revalidate_after=NOW + timedelta(days=1),
    )

    assessment = assess_occupational_safety(
        context,
        (cue,),
        guidance=(future,),
        as_of=NOW,
    )

    assert assessment.guidance_resolutions[0].state == "NOT_YET_OBSERVED"
    assert assessment.needs_fresh_guidance is True


def test_guidance_scope_collections_reject_scalar_text_coercion() -> None:
    with pytest.raises(ValidationError, match="sequence of text values"):
        SafetyGuidance(
            id="scalar-role",
            domain="HEARING",
            statement="fixture",
            source_kind="AUTHORITATIVE_EXTERNAL",
            source_ref="authority:fixture",
            observed_at=NOW - timedelta(days=1),
            revalidate_after=NOW + timedelta(days=1),
            applies_to_roles="Artist",  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError, match="sequence of text values"):
        SafetyGuidance(
            id="scalar-work",
            domain="HEARING",
            statement="fixture",
            source_kind="AUTHORITATIVE_EXTERNAL",
            source_ref="authority:fixture",
            observed_at=NOW - timedelta(days=1),
            revalidate_after=NOW + timedelta(days=1),
            applies_to_work_kinds="STUDIO",  # type: ignore[arg-type]
        )


def test_invalid_guidance_window_and_unknown_semantics_fail_closed() -> None:
    with pytest.raises(ValidationError, match="revalidate_after must be after"):
        SafetyGuidance(
            id="bad-window",
            domain="HEARING",
            statement="fixture",
            source_kind="AUTHORITATIVE_EXTERNAL",
            source_ref="authority:fixture",
            observed_at=NOW,
            revalidate_after=NOW,
        )

    with pytest.raises(ValidationError, match="unsupported safety domain"):
        SafetyCue(
            id="score",
            domain="DIAGNOSIS_SCORE",
            kind="FATIGUE",
            statement="fixture",
            source_kind="OBSERVED",
            source_ref="observer:fixture",
            observed_at=NOW,
        )
