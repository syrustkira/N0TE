from __future__ import annotations

import pytest

from n0te.lineage import ValidationError
from n0te.territory import TerritoryContext


def test_known_source_without_any_context_value_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match="known territory context needs at least one context value"
    ):
        TerritoryContext(
            source_kind="USER_DECLARED",
            source_ref="profile:empty-territory-context",
        )


def test_jurisdiction_may_supply_territory_without_duplicate_country_field() -> None:
    context = TerritoryContext(
        jurisdiction_code="US-WI",
        source_kind="OBSERVED_PROFILE",
        source_ref="profile:jurisdiction",
    )

    assert context.territory_code is None
    assert context.jurisdiction_code == "US-WI"
    assert context.effective_territory_code == "US"
    assert context.territory_known is True


def test_country_only_code_cannot_masquerade_as_jurisdiction() -> None:
    with pytest.raises(
        ValidationError, match="territory-prefixed subdivision code"
    ):
        TerritoryContext(
            jurisdiction_code="US",
            source_kind="USER_DECLARED",
            source_ref="profile:not-a-jurisdiction",
        )
