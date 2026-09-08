from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from n0te.lineage import ValidationError
from n0te.professional_ethics import (
    ProfessionalEthicsAssessment,
    ProfessionalEthicsContext,
    ProfessionalEthicsSignal,
    assess_professional_ethics,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _context(**overrides: object) -> ProfessionalEthicsContext:
    values: dict[str, object] = {
        "relationship_ref": "relationship:artist-manager",
        "action_ref": "action:deal-review",
        "actor_ref": "person:manager",
        "actor_role": "Manager",
        "affected_party_ref": "person:artist",
        "purpose": "deal_review",
        "requested_audiences": (),
        "required_checks": (),
        "impact_class": "ROUTINE",
        "independent_review_domains": (),
    }
    values.update(overrides)
    return ProfessionalEthicsContext(**values)  # type: ignore[arg-type]


def _signal(
    signal_id: str,
    kind: str,
    state: str,
    *,
    subject_ref: str | None = None,
    observed_at: datetime = NOW,
    **overrides: object,
) -> ProfessionalEthicsSignal:
    if subject_ref is None:
        subject_ref = "person:manager" if kind == "CONFLICT_OF_INTEREST" else "person:artist"
    values: dict[str, object] = {
        "id": signal_id,
        "relationship_ref": "relationship:artist-manager",
        "kind": kind,
        "state": state,
        "subject_ref": subject_ref,
        "statement": f"Explicit {kind} evidence",
        "source_kind": "USER_DECLARED",
        "source_ref": f"source:{signal_id}",
        "observed_at": observed_at,
        "permitted_purposes": (),
        "permitted_audiences": (),
        "voluntary_confirmed": False,
    }
    values.update(overrides)
    return ProfessionalEthicsSignal(**values)  # type: ignore[arg-type]


def test_required_checks_can_clear_without_granting_authority() -> None:
    context = _context(required_checks=("CONFLICT_OF_INTEREST", "INFORMED_CONSENT"))
    assessment = assess_professional_ethics(
        context,
        (
            _signal("conflict-clear", "CONFLICT_OF_INTEREST", "ABSENT"),
            _signal(
                "consent-granted",
                "INFORMED_CONSENT",
                "GRANTED",
                voluntary_confirmed=True,
            ),
        ),
        as_of=NOW,
    )

    assert assessment.state == "CLEAR"
    assert assessment.unresolved_checks == ()
    assert assessment.consent_satisfied is True
    assert assessment.action_authority_granted is False
    assert assessment.external_action_authorized is False
    assert assessment.mutation_authorized is False
    assert assessment.role_grants_authority is False
    assert assessment.authority_effect == "UNCHANGED"
    assert assessment.persistence_effect == "NONE"
    assert assessment.reputation_score is None


def test_manager_self_dealing_material_conflict_requires_disclosure_and_independent_review() -> None:
    context = _context(
        required_checks=("CONFLICT_OF_INTEREST",),
        impact_class="MATERIAL",
    )
    assessment = assess_professional_ethics(
        context,
        (_signal("self-dealing", "CONFLICT_OF_INTEREST", "PRESENT"),),
        as_of=NOW,
    )

    assert assessment.state == "PAUSE"
    assert assessment.disclosure_required is True
    assert assessment.independent_review_required is True
    assert "CONFLICT_OF_INTEREST" in assessment.concern_kinds
    assert any("unconflicted" in step.lower() for step in assessment.required_steps)


def test_confidential_client_material_is_limited_to_exact_purpose_and_audience() -> None:
    context = _context(
        purpose="mix_delivery",
        requested_audiences=("person:mix-engineer",),
        required_checks=("CONFIDENTIALITY_PERMISSION",),
    )
    permission = _signal(
        "client-permission",
        "CONFIDENTIALITY_PERMISSION",
        "GRANTED",
        permitted_purposes=("mix_delivery",),
        permitted_audiences=("person:mix-engineer", "person:producer"),
    )

    assessment = assess_professional_ethics(context, (permission,), as_of=NOW)

    assert assessment.state == "CLEAR"
    assert assessment.confidentiality_satisfied is True
    assert assessment.permitted_disclosure_audiences == ("person:mix-engineer",)


def test_confidential_permission_cannot_be_broadened_to_unapproved_audience() -> None:
    context = _context(
        purpose="mix_delivery",
        requested_audiences=("person:mix-engineer", "person:publicist"),
        required_checks=("CONFIDENTIALITY_PERMISSION",),
    )
    permission = _signal(
        "narrow-permission",
        "CONFIDENTIALITY_PERMISSION",
        "GRANTED",
        permitted_purposes=("mix_delivery",),
        permitted_audiences=("person:mix-engineer",),
    )

    assessment = assess_professional_ethics(context, (permission,), as_of=NOW)

    assert assessment.state == "PAUSE"
    assert assessment.confidentiality_satisfied is False
    assert assessment.permitted_disclosure_audiences == ()
    assert any("minimum necessary audience" in step for step in assessment.required_steps)


def test_power_asymmetry_requires_explicit_voluntary_consent_confirmation() -> None:
    context = _context(required_checks=("INFORMED_CONSENT",))
    consent = _signal("consent", "INFORMED_CONSENT", "GRANTED")
    power = _signal("gatekeeper", "POWER_ASYMMETRY", "PRESENT")

    assessment = assess_professional_ethics(context, (consent, power), as_of=NOW)

    assert assessment.state == "PAUSE"
    assert assessment.consent_satisfied is False
    assert "POWER_ASYMMETRY" in assessment.concern_kinds
    assert any("without pressure" in step for step in assessment.required_steps)


def test_coerced_consent_never_satisfies_consent_gate() -> None:
    context = _context(required_checks=("INFORMED_CONSENT",))
    assessment = assess_professional_ethics(
        context,
        (_signal("coerced", "INFORMED_CONSENT", "COERCED"),),
        as_of=NOW,
    )

    assert assessment.state == "PAUSE"
    assert assessment.consent_satisfied is False


def test_attribution_consent_does_not_establish_authorship_or_ownership() -> None:
    context = _context(required_checks=("ATTRIBUTION_CONSENT",))
    assessment = assess_professional_ethics(
        context,
        (_signal("credit-ok", "ATTRIBUTION_CONSENT", "GRANTED"),),
        as_of=NOW,
    )

    assert assessment.state == "CLEAR"
    assert assessment.attribution_consent_satisfied is True
    assert assessment.establishes_authorship_or_ownership is False


def test_withdrawn_attribution_consent_pauses_public_reuse() -> None:
    context = _context(required_checks=("ATTRIBUTION_CONSENT",))
    assessment = assess_professional_ethics(
        context,
        (_signal("credit-withdrawn", "ATTRIBUTION_CONSENT", "WITHDRAWN"),),
        as_of=NOW,
    )

    assert assessment.state == "PAUSE"
    assert assessment.attribution_consent_satisfied is False
    assert any("attribution" in step.lower() for step in assessment.required_steps)


def test_safeguarding_concern_interrupts_quiet_convenience_and_escalates() -> None:
    context = _context(required_checks=("SAFEGUARDING_CONCERN",))
    assessment = assess_professional_ethics(
        context,
        (_signal("safeguarding", "SAFEGUARDING_CONCERN", "PRESENT"),),
        as_of=NOW,
    )

    assert assessment.state == "ESCALATE"
    assert assessment.safeguarding_escalation is True
    assert assessment.independent_review_required is True
    assert assessment.required_steps[0].startswith("Pause the affected workflow")


def test_legal_and_clinical_questions_route_outside_n0te_without_certification() -> None:
    context = _context(independent_review_domains=("LEGAL", "CLINICAL"))
    assessment = assess_professional_ethics(context, (), as_of=NOW)

    assert assessment.state == "PAUSE"
    assert assessment.independent_review_required is True
    assert assessment.legal_compliance_certified is False
    assert assessment.clinical_compliance_certified is False
    assert any("does not certify compliance" in step for step in assessment.required_steps)


def test_missing_or_disputed_required_evidence_fails_closed() -> None:
    missing = assess_professional_ethics(
        _context(required_checks=("CONFLICT_OF_INTEREST",)),
        (),
        as_of=NOW,
    )
    disputed = assess_professional_ethics(
        _context(required_checks=("CONFLICT_OF_INTEREST",)),
        (_signal("disputed", "CONFLICT_OF_INTEREST", "DISPUTED"),),
        as_of=NOW,
    )

    assert missing.state == "PAUSE"
    assert missing.unresolved_checks == ("CONFLICT_OF_INTEREST",)
    assert disputed.state == "PAUSE"
    assert disputed.unresolved_checks == ("CONFLICT_OF_INTEREST",)


def test_latest_source_bound_signal_supersedes_older_relationship_evidence() -> None:
    context = _context(required_checks=("INFORMED_CONSENT",))
    earlier = _signal(
        "old-withheld",
        "INFORMED_CONSENT",
        "WITHHELD",
        observed_at=NOW - timedelta(hours=1),
    )
    latest = _signal(
        "new-granted",
        "INFORMED_CONSENT",
        "GRANTED",
        observed_at=NOW,
        voluntary_confirmed=True,
    )

    assessment = assess_professional_ethics(context, (earlier, latest), as_of=NOW)

    assert assessment.state == "CLEAR"
    assert assessment.consent_satisfied is True
    assert assessment.evidence_ids == ("old-withheld", "new-granted")


def test_cross_relationship_wrong_subject_future_and_tied_evidence_fail_closed() -> None:
    context = _context(required_checks=("INFORMED_CONSENT",))

    with pytest.raises(ValidationError, match="another relationship"):
        assess_professional_ethics(
            context,
            (
                _signal(
                    "foreign",
                    "INFORMED_CONSENT",
                    "GRANTED",
                    relationship_ref="relationship:other",
                    voluntary_confirmed=True,
                ),
            ),
            as_of=NOW,
        )

    with pytest.raises(ValidationError, match="wrong subject"):
        assess_professional_ethics(
            context,
            (
                _signal(
                    "wrong-subject",
                    "INFORMED_CONSENT",
                    "GRANTED",
                    subject_ref="person:someone-else",
                    voluntary_confirmed=True,
                ),
            ),
            as_of=NOW,
        )

    with pytest.raises(ValidationError, match="after as_of"):
        assess_professional_ethics(
            context,
            (
                _signal(
                    "future",
                    "INFORMED_CONSENT",
                    "GRANTED",
                    observed_at=NOW + timedelta(seconds=1),
                    voluntary_confirmed=True,
                ),
            ),
            as_of=NOW,
        )

    with pytest.raises(ValidationError, match="latest timestamp"):
        assess_professional_ethics(
            context,
            (
                _signal("tie-a", "INFORMED_CONSENT", "WITHHELD"),
                _signal(
                    "tie-b",
                    "INFORMED_CONSENT",
                    "GRANTED",
                    voluntary_confirmed=True,
                ),
            ),
            as_of=NOW,
        )


def test_malformed_permission_scope_and_boolean_inputs_are_rejected() -> None:
    with pytest.raises(ValidationError, match="requires purposes and audiences"):
        _signal("bad-permission", "CONFIDENTIALITY_PERMISSION", "GRANTED")

    with pytest.raises(ValidationError, match="permission scope is only valid"):
        _signal(
            "bad-scope",
            "INFORMED_CONSENT",
            "GRANTED",
            permitted_purposes=("deal_review",),
        )

    with pytest.raises(ValidationError, match="must be boolean"):
        _signal(
            "bool-coercion",
            "INFORMED_CONSENT",
            "GRANTED",
            voluntary_confirmed=1,
        )


def test_authority_and_certification_fields_cannot_be_forged_or_mutated() -> None:
    with pytest.raises(TypeError):
        ProfessionalEthicsAssessment(
            relationship_ref="relationship:artist-manager",
            action_ref="action:test",
            state="CLEAR",
            evidence_ids=(),
            unresolved_checks=(),
            concern_kinds=(),
            required_steps=(),
            disclosure_required=False,
            independent_review_required=False,
            consent_satisfied=True,
            confidentiality_satisfied=True,
            attribution_consent_satisfied=True,
            safeguarding_escalation=False,
            permitted_disclosure_audiences=(),
            reason="clear",
            action_authority_granted=True,
        )

    assessment = assess_professional_ethics(_context(), (), as_of=NOW)
    with pytest.raises(FrozenInstanceError):
        assessment.external_action_authorized = True  # type: ignore[misc]
