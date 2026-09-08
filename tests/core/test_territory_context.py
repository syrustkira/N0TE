from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from n0te.lineage import ValidationError
from n0te.territory import (
    TerritoryContext,
    TerritoryFact,
    resolve_territory_fact,
    resolve_territory_facts,
)

UTC = timezone.utc
OBSERVED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
REVALIDATE = OBSERVED + timedelta(days=30)


def fact(
    *,
    fact_id: str = "fact-1",
    kind: str = "RIGHTS_RULE",
    territory: str = "US",
    jurisdiction: str | None = None,
    source_kind: str = "AUTHORITATIVE_EXTERNAL",
    source_ref: str = "source:fixture",
) -> TerritoryFact:
    return TerritoryFact(
        id=fact_id,
        kind=kind,
        territory_code=territory,
        jurisdiction_code=jurisdiction,
        statement="Fixture claim used only to test territorial scoping.",
        source_kind=source_kind,
        source_ref=source_ref,
        observed_at=OBSERVED,
        revalidate_after=REVALIDATE,
    )


def test_unknown_territory_never_defaults_to_us() -> None:
    context = TerritoryContext.unknown()
    resolution = resolve_territory_fact(
        context,
        fact(territory="US"),
        as_of=OBSERVED + timedelta(days=1),
    )

    assert context.territory_known is False
    assert context.effective_territory_code is None
    assert resolution.state == "NEEDS_TERRITORY"
    assert resolution.usable_as_current_external_evidence is False
    assert "cannot be applied by default" in resolution.reason


def test_same_fact_resolves_differently_across_two_territories() -> None:
    gb_fact = fact(
        fact_id="gb-rights",
        territory="GB",
        source_ref="source:gb-fixture",
    )
    us = TerritoryContext(
        territory_code="US",
        currency_code="USD",
        locale_tag="en-US",
        source_kind="USER_DECLARED",
        source_ref="profile:territory",
    )
    gb = TerritoryContext(
        territory_code="GB",
        currency_code="GBP",
        locale_tag="en-GB",
        source_kind="USER_DECLARED",
        source_ref="profile:territory",
    )

    us_resolution = resolve_territory_fact(
        us, gb_fact, as_of=OBSERVED + timedelta(days=1)
    )
    gb_resolution = resolve_territory_fact(
        gb, gb_fact, as_of=OBSERVED + timedelta(days=1)
    )

    assert us_resolution.state == "OUT_OF_SCOPE"
    assert gb_resolution.state == "APPLICABLE"
    assert gb_resolution.usable_as_current_external_evidence is True


def test_jurisdiction_specific_claim_requires_exact_jurisdiction() -> None:
    wi_fact = fact(
        fact_id="us-wi-business",
        kind="BUSINESS_RULE",
        territory="US",
        jurisdiction="US-WI",
    )
    country_only = TerritoryContext(
        territory_code="US",
        source_kind="USER_DECLARED",
        source_ref="profile:country",
    )
    wisconsin = TerritoryContext(
        territory_code="US",
        jurisdiction_code="US-WI",
        source_kind="USER_DECLARED",
        source_ref="profile:jurisdiction",
    )
    california = TerritoryContext(
        territory_code="US",
        jurisdiction_code="US-CA",
        source_kind="USER_DECLARED",
        source_ref="profile:jurisdiction",
    )

    assert resolve_territory_fact(
        country_only, wi_fact, as_of=OBSERVED + timedelta(days=1)
    ).state == "NEEDS_JURISDICTION"
    assert resolve_territory_fact(
        wisconsin, wi_fact, as_of=OBSERVED + timedelta(days=1)
    ).state == "APPLICABLE"
    assert resolve_territory_fact(
        california, wi_fact, as_of=OBSERVED + timedelta(days=1)
    ).state == "OUT_OF_SCOPE"


def test_authoritative_fact_becomes_stale_at_explicit_boundary() -> None:
    context = TerritoryContext(
        territory_code="CA",
        source_kind="IMPORTED",
        source_ref="settings:territory",
    )
    row = fact(territory="CA", kind="TAX_RULE")

    current = resolve_territory_fact(
        context, row, as_of=REVALIDATE - timedelta(seconds=1)
    )
    stale = resolve_territory_fact(context, row, as_of=REVALIDATE)

    assert current.state == "APPLICABLE"
    assert stale.state == "STALE"
    assert stale.usable_as_current_external_evidence is False
    assert stale.source_ref == row.source_ref
    assert stale.revalidate_after == REVALIDATE


def test_user_declared_external_rule_is_visible_but_unverified() -> None:
    context = TerritoryContext(
        territory_code="US",
        source_kind="USER_DECLARED",
        source_ref="profile:territory",
    )
    row = fact(
        territory="US",
        kind="UNION_GUILD",
        source_kind="USER_DECLARED",
        source_ref="artist:statement",
    )

    resolution = resolve_territory_fact(
        context, row, as_of=OBSERVED + timedelta(days=1)
    )

    assert resolution.state == "UNVERIFIED"
    assert resolution.source_kind == "USER_DECLARED"
    assert resolution.usable_as_current_external_evidence is False


def test_cultural_context_is_provenance_bound_not_a_stereotype_score() -> None:
    context = TerritoryContext(
        territory_code="JP",
        locale_tag="ja-JP",
        source_kind="USER_DECLARED",
        source_ref="profile:territory",
    )
    cultural = fact(
        fact_id="culture-1",
        territory="JP",
        kind="CULTURAL_CONTEXT",
        source_kind="OBSERVED_EXTERNAL",
        source_ref="research:bounded-context",
    )

    resolution = resolve_territory_fact(
        context, cultural, as_of=OBSERVED + timedelta(days=1)
    )

    assert resolution.state == "APPLICABLE"
    assert resolution.source_ref == "research:bounded-context"

    with pytest.raises(ValidationError, match="unsupported territory fact kind"):
        fact(kind="CULTURAL_SCORE", territory="JP")


def test_locale_and_currency_do_not_infer_country() -> None:
    context = TerritoryContext(
        locale_tag="fr-CA",
        currency_code="CAD",
        source_kind="USER_DECLARED",
        source_ref="profile:locale-currency",
    )

    assert context.locale_tag == "fr-CA"
    assert context.currency_code == "CAD"
    assert context.effective_territory_code is None


def test_unknown_source_cannot_carry_sourced_context_values() -> None:
    with pytest.raises(ValidationError, match="cannot carry sourced context values"):
        TerritoryContext(territory_code="US", source_kind="UNKNOWN")

    with pytest.raises(ValidationError, match="cannot carry sourced context values"):
        TerritoryContext(locale_tag="en-US", source_kind="UNKNOWN")


def test_territory_and_jurisdiction_conflicts_fail_closed() -> None:
    with pytest.raises(ValidationError, match="conflicts with territory_code"):
        TerritoryContext(
            territory_code="US",
            jurisdiction_code="CA-ON",
            source_kind="USER_DECLARED",
            source_ref="profile:territory",
        )

    with pytest.raises(ValidationError, match="jurisdiction conflicts with territory"):
        fact(territory="US", jurisdiction="GB-ENG")


def test_freshness_requires_timezone_aware_ordered_timestamps() -> None:
    with pytest.raises(ValidationError, match="observed_at must be timezone-aware"):
        TerritoryFact(
            id="naive",
            kind="PROVIDER_AVAILABILITY",
            territory_code="US",
            statement="fixture",
            source_kind="PROVIDER_EVIDENCE",
            source_ref="provider:fixture",
            observed_at=datetime(2026, 9, 1, 12, 0),
            revalidate_after=REVALIDATE,
        )

    with pytest.raises(ValidationError, match="revalidate_after must be after"):
        TerritoryFact(
            id="bad-window",
            kind="PROVIDER_AVAILABILITY",
            territory_code="US",
            statement="fixture",
            source_kind="PROVIDER_EVIDENCE",
            source_ref="provider:fixture",
            observed_at=OBSERVED,
            revalidate_after=OBSERVED,
        )


def test_future_observation_cannot_be_used_for_earlier_decision() -> None:
    context = TerritoryContext(
        territory_code="US",
        source_kind="OBSERVED_PROFILE",
        source_ref="profile:territory",
    )
    resolution = resolve_territory_fact(
        context, fact(), as_of=OBSERVED - timedelta(seconds=1)
    )

    assert resolution.state == "NOT_YET_OBSERVED"
    assert resolution.usable_as_current_external_evidence is False


def test_duplicate_fact_ids_fail_closed_in_projection() -> None:
    context = TerritoryContext(
        territory_code="US",
        source_kind="USER_DECLARED",
        source_ref="profile:territory",
    )
    row = fact()

    with pytest.raises(ValidationError, match="territory fact IDs must be unique"):
        resolve_territory_facts(
            context,
            (row, row),
            as_of=OBSERVED + timedelta(days=1),
        )
