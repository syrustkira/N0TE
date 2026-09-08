from __future__ import annotations

import pytest

from n0te.role_economics import (
    ExternalEconomicFact,
    MoneyAmount,
    ParticipationTerm,
    ProfitabilityScenario,
    RevisionTerms,
    RoleEconomicsError,
    RoleOffer,
    ROLE_REVIEW_DIMENSIONS,
    review_role_offer,
)


def _fresh_fact(*, evidence_kind: str = "PROVIDER_VERIFIED") -> ExternalEconomicFact:
    return ExternalEconomicFact(
        kind="UNION_TERM",
        value="Current published session term",
        territory="US",
        source_ref="source://union/current",
        evidence_kind=evidence_kind,
        observed_on="2026-09-01",
        valid_through="2026-12-31",
    )


def _producer_offer(*, external_fact: ExternalEconomicFact | None = None) -> RoleOffer:
    return RoleOffer(
        role="producer",
        fee=MoneyAmount("usd", 200_000),
        deposit=MoneyAmount("USD", 100_000),
        revisions=RevisionTerms(2, "Additional revisions require a separately approved change order."),
        cancellation_terms="Cancellation terms are proposed and require agreement review.",
        participations=(ParticipationTerm("points", 300, "Defined royalty-accounting basis"),),
        expense_terms="Pre-approved studio expenses only.",
        licensing_terms="Any license must be separately evidenced.",
        exclusivity_terms="No exclusivity beyond the stated project unless separately agreed.",
        external_facts=((external_fact or _fresh_fact()),),
        profitability=ProfitabilityScenario(
            expected_revenue=MoneyAmount("USD", 200_000),
            expected_cost=MoneyAmount("USD", 75_000),
            note="Planning scenario only; not booked profit.",
        ),
    )


def test_money_normalizes_currency_without_implying_payment() -> None:
    amount = MoneyAmount(" usd ", 125_000)
    assert amount.currency == "USD"
    assert amount.minor_units == 125_000


def test_money_rejects_invalid_currency_and_negative_amounts() -> None:
    with pytest.raises(RoleEconomicsError):
        MoneyAmount("US", 100)
    with pytest.raises(RoleEconomicsError):
        MoneyAmount("USD", -1)


def test_revision_terms_never_imply_change_order_approval() -> None:
    revisions = RevisionTerms(3, "Extra revisions billed only after approval.")
    assert revisions.included_revisions == 3
    assert revisions.change_order_approved is False


def test_participation_does_not_verify_ownership_or_royalty_entitlement() -> None:
    term = ParticipationTerm("backend", 500, "Defined backend receipts")
    assert term.rate_basis_points == 500
    assert term.ownership_verified is False
    assert term.royalty_entitlement_verified is False


def test_external_fact_requires_explicit_territory_and_freshness() -> None:
    fact = _fresh_fact()
    assert fact.applicability(as_of="2026-09-04", territory="US") == "APPLICABLE"
    assert fact.applicability(as_of="2026-09-04", territory="GB") == "OUT_OF_SCOPE"
    assert fact.applicability(as_of="2027-01-01", territory="US") == "STALE"


def test_user_declared_external_fact_is_not_promoted_to_verified() -> None:
    fact = _fresh_fact(evidence_kind="USER_DECLARED")
    assert fact.applicability(as_of="2026-09-04", territory="US") == "UNVERIFIED"


def test_future_external_evidence_is_not_treated_as_current() -> None:
    fact = ExternalEconomicFact(
        kind="TAX_TERM",
        value="Future rule",
        territory="US",
        source_ref="source://tax/future",
        evidence_kind="PROVIDER_VERIFIED",
        observed_on="2026-10-01",
        valid_through="2027-01-01",
    )
    assert fact.applicability(as_of="2026-09-04", territory="US") == "FUTURE_EVIDENCE"


def test_external_fact_rejects_invalid_window_and_territory() -> None:
    with pytest.raises(RoleEconomicsError):
        ExternalEconomicFact(
            kind="MARKET_RATE",
            value="Example",
            territory="USA",
            source_ref="source://market",
            evidence_kind="OBSERVED",
            observed_on="2026-09-01",
            valid_through="2026-09-30",
        )
    with pytest.raises(RoleEconomicsError):
        ExternalEconomicFact(
            kind="MARKET_RATE",
            value="Example",
            territory="US",
            source_ref="source://market",
            evidence_kind="OBSERVED",
            observed_on="2026-09-30",
            valid_through="2026-09-01",
        )


def test_profitability_is_explicitly_a_scenario_not_actual_profit() -> None:
    scenario = ProfitabilityScenario(
        expected_revenue=MoneyAmount("USD", 150_000),
        expected_cost=MoneyAmount("USD", 90_000),
        note="Scenario before final expenses.",
    )
    assert scenario.projected_margin_minor_units == 60_000
    assert scenario.is_actual_profit is False


def test_profitability_refuses_cross_currency_fake_margin() -> None:
    with pytest.raises(RoleEconomicsError):
        ProfitabilityScenario(
            expected_revenue=MoneyAmount("USD", 100_000),
            expected_cost=MoneyAmount("EUR", 50_000),
            note="No implicit FX conversion.",
        )


def test_offer_rejects_deposit_above_fee_or_mixed_currency() -> None:
    with pytest.raises(RoleEconomicsError):
        RoleOffer(
            role="mix engineer",
            fee=MoneyAmount("USD", 50_000),
            deposit=MoneyAmount("USD", 60_000),
        )
    with pytest.raises(RoleEconomicsError):
        RoleOffer(
            role="mix engineer",
            fee=MoneyAmount("USD", 50_000),
            deposit=MoneyAmount("EUR", 25_000),
        )


def test_commission_basis_must_be_explicit_and_bounded() -> None:
    with pytest.raises(RoleEconomicsError):
        RoleOffer(role="manager", commission_basis="gross artist income")
    with pytest.raises(RoleEconomicsError):
        RoleOffer(
            role="manager",
            commission_basis_points=10_001,
            commission_basis="gross artist income",
        )


def test_populated_dimension_cannot_also_be_marked_not_applicable() -> None:
    with pytest.raises(RoleEconomicsError):
        RoleOffer(
            role="session musician",
            fee=MoneyAmount("USD", 50_000),
            not_applicable=frozenset({"FEE"}),
        )


def test_role_review_dimensions_are_materially_different() -> None:
    assert "REVISIONS" in ROLE_REVIEW_DIMENSIONS["PRODUCER"]
    assert "USAGE" in ROLE_REVIEW_DIMENSIONS["SESSION_MUSICIAN"]
    assert "COMMISSION" in ROLE_REVIEW_DIMENSIONS["MANAGER"]
    assert "COMMISSION" not in ROLE_REVIEW_DIMENSIONS["MASTERING_ENGINEER"]
    assert ROLE_REVIEW_DIMENSIONS["PRODUCER"] != ROLE_REVIEW_DIMENSIONS["MANAGER"]


def test_complete_producer_terms_are_reviewable_with_current_external_evidence() -> None:
    assessment = review_role_offer(
        _producer_offer(), as_of="2026-09-04", territory="US"
    )
    assert assessment.role == "PRODUCER"
    assert assessment.unaddressed_dimensions == ()
    assert assessment.external_fact_states == (
        ("UNION_TERM", "source://union/current", "APPLICABLE"),
    )
    assert assessment.state == "REVIEWABLE"


def test_incomplete_role_terms_surface_questions_instead_of_inventing_defaults() -> None:
    assessment = review_role_offer(
        RoleOffer(role="mastering engineer", fee=MoneyAmount("USD", 50_000)),
        as_of="2026-09-04",
    )
    assert assessment.state == "NEEDS_TERMS"
    assert "DEPOSIT" in assessment.unaddressed_dimensions
    assert assessment.questions
    assert all("Clarify whether" in question for question in assessment.questions)


def test_external_terms_require_territory_and_revalidation() -> None:
    offer = _producer_offer()
    missing_territory = review_role_offer(offer, as_of="2026-09-04")
    assert missing_territory.state == "NEEDS_EXTERNAL_EVIDENCE"
    assert missing_territory.external_fact_states[0][2] == "NEEDS_TERRITORY"

    stale = review_role_offer(offer, as_of="2027-01-01", territory="US")
    assert stale.state == "NEEDS_EXTERNAL_EVIDENCE"
    assert stale.external_fact_states[0][2] == "STALE"


def test_not_applicable_can_close_irrelevant_dimensions_without_fabricating_terms() -> None:
    offer = RoleOffer(
        role="manager",
        commission_basis_points=1_500,
        commission_basis="defined commissionable artist income",
        not_applicable=frozenset(
            {"CANCELLATION", "EXPENSES", "PARTICIPATION", "EXTERNAL_TERMS", "PROFITABILITY"}
        ),
    )
    assessment = review_role_offer(offer, as_of="2026-09-04")
    assert assessment.state == "REVIEWABLE"
    assert assessment.unaddressed_dimensions == ()


def test_review_never_grants_payment_signature_rights_or_external_authority() -> None:
    assessment = review_role_offer(
        _producer_offer(), as_of="2026-09-04", territory="US"
    )
    assert assessment.market_rate_verified is False
    assert assessment.payment_received_verified is False
    assert assessment.legal_enforceability_verified is False
    assert assessment.rights_verified is False
    assert assessment.union_applicability_verified is False
    assert assessment.tax_treatment_verified is False
    assert assessment.signature_authority_granted is False
    assert assessment.payment_authority_granted is False
    assert assessment.spend_authority_granted is False
    assert assessment.obligation_created is False
    assert assessment.transaction_created is False
    assert assessment.external_action_authorized is False
