from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .lineage import ValidationError

TERRITORY_FACT_KINDS = {
    "UNION_GUILD",
    "COLLECTING_SOCIETY",
    "TAX_RULE",
    "RIGHTS_RULE",
    "PROVIDER_AVAILABILITY",
    "BUSINESS_RULE",
    "CULTURAL_CONTEXT",
}
TERRITORY_FACT_SOURCE_KINDS = {
    "AUTHORITATIVE_EXTERNAL",
    "PROVIDER_EVIDENCE",
    "OBSERVED_EXTERNAL",
    "USER_DECLARED",
}
VERIFIED_TERRITORY_FACT_SOURCE_KINDS = {
    "AUTHORITATIVE_EXTERNAL",
    "PROVIDER_EVIDENCE",
    "OBSERVED_EXTERNAL",
}
TERRITORY_CONTEXT_SOURCE_KINDS = {
    "USER_DECLARED",
    "OBSERVED_PROFILE",
    "IMPORTED",
    "UNKNOWN",
}
TERRITORY_RESOLUTION_STATES = {
    "APPLICABLE",
    "NEEDS_TERRITORY",
    "NEEDS_JURISDICTION",
    "OUT_OF_SCOPE",
    "STALE",
    "UNVERIFIED",
    "NOT_YET_OBSERVED",
}

_TERRITORY_CODE = re.compile(r"^[A-Z]{2}$")
_JURISDICTION_CODE = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,8})+$")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
_LOCALE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")


def _normalize_territory(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not _TERRITORY_CODE.fullmatch(normalized):
        raise ValidationError(f"{field} must be a two-letter territory code")
    return normalized


def _normalize_jurisdiction(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not _JURISDICTION_CODE.fullmatch(normalized):
        raise ValidationError(
            "jurisdiction_code must be a territory-prefixed subdivision code"
        )
    return normalized


def _normalize_currency(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not _CURRENCY_CODE.fullmatch(normalized):
        raise ValidationError("currency_code must be a three-letter currency code")
    return normalized


def _normalize_locale(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not _LOCALE_TAG.fullmatch(normalized):
        raise ValidationError("locale_tag must be a bounded language/locale tag")
    return normalized


@dataclass(frozen=True)
class TerritoryContext:
    """Stable location/locale context, never a source of current legal rules."""

    territory_code: str | None = None
    jurisdiction_code: str | None = None
    currency_code: str | None = None
    locale_tag: str | None = None
    source_kind: str = "UNKNOWN"
    source_ref: str | None = None

    def __post_init__(self) -> None:
        territory = _normalize_territory(self.territory_code, field="territory_code")
        jurisdiction = _normalize_jurisdiction(self.jurisdiction_code)
        currency = _normalize_currency(self.currency_code)
        locale = _normalize_locale(self.locale_tag)
        if self.source_kind not in TERRITORY_CONTEXT_SOURCE_KINDS:
            raise ValidationError(f"unsupported territory context source: {self.source_kind}")
        has_context_value = any(
            value is not None for value in (territory, jurisdiction, currency, locale)
        )
        if self.source_kind == "UNKNOWN":
            if self.source_ref is not None:
                raise ValidationError("UNKNOWN territory context cannot claim a source_ref")
            if has_context_value:
                raise ValidationError(
                    "UNKNOWN territory context cannot carry sourced context values"
                )
        else:
            if not str(self.source_ref or "").strip():
                raise ValidationError("known territory context needs a source_ref")
            if not has_context_value:
                raise ValidationError(
                    "known territory context needs at least one context value"
                )
        if jurisdiction is not None:
            jurisdiction_territory = jurisdiction.split("-", 1)[0]
            if territory is not None and jurisdiction_territory != territory:
                raise ValidationError("jurisdiction_code conflicts with territory_code")
        object.__setattr__(self, "territory_code", territory)
        object.__setattr__(self, "jurisdiction_code", jurisdiction)
        object.__setattr__(self, "currency_code", currency)
        object.__setattr__(self, "locale_tag", locale)

    @property
    def effective_territory_code(self) -> str | None:
        if self.territory_code is not None:
            return self.territory_code
        if self.jurisdiction_code is not None:
            return self.jurisdiction_code.split("-", 1)[0]
        return None

    @property
    def territory_known(self) -> bool:
        return self.effective_territory_code is not None

    @classmethod
    def unknown(cls) -> "TerritoryContext":
        return cls(
            territory_code=None,
            jurisdiction_code=None,
            currency_code=None,
            locale_tag=None,
            source_kind="UNKNOWN",
            source_ref=None,
        )


@dataclass(frozen=True)
class TerritoryFact:
    """One volatile external claim scoped to an explicit territory/jurisdiction."""

    id: str
    kind: str
    territory_code: str
    statement: str
    source_kind: str
    source_ref: str
    observed_at: datetime
    revalidate_after: datetime
    jurisdiction_code: str | None = None

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValidationError("territory fact needs an id")
        if self.kind not in TERRITORY_FACT_KINDS:
            raise ValidationError(f"unsupported territory fact kind: {self.kind}")
        territory = _normalize_territory(self.territory_code, field="territory fact territory_code")
        jurisdiction = _normalize_jurisdiction(self.jurisdiction_code)
        if not str(self.statement).strip():
            raise ValidationError("territory fact needs a statement")
        if self.source_kind not in TERRITORY_FACT_SOURCE_KINDS:
            raise ValidationError(f"unsupported territory fact source: {self.source_kind}")
        if not str(self.source_ref).strip():
            raise ValidationError("territory fact needs a source_ref")
        _require_aware(self.observed_at, field="observed_at")
        _require_aware(self.revalidate_after, field="revalidate_after")
        if self.revalidate_after <= self.observed_at:
            raise ValidationError("revalidate_after must be after observed_at")
        if jurisdiction is not None and jurisdiction.split("-", 1)[0] != territory:
            raise ValidationError("territory fact jurisdiction conflicts with territory")
        object.__setattr__(self, "territory_code", territory)
        object.__setattr__(self, "jurisdiction_code", jurisdiction)

    @property
    def verified_source(self) -> bool:
        return self.source_kind in VERIFIED_TERRITORY_FACT_SOURCE_KINDS


@dataclass(frozen=True)
class TerritoryFactResolution:
    fact_id: str
    state: str
    context_territory_code: str | None
    context_jurisdiction_code: str | None
    fact_territory_code: str
    fact_jurisdiction_code: str | None
    source_kind: str
    source_ref: str
    observed_at: datetime
    revalidate_after: datetime
    reason: str

    def __post_init__(self) -> None:
        if self.state not in TERRITORY_RESOLUTION_STATES:
            raise ValidationError(f"unsupported territory resolution state: {self.state}")

    @property
    def usable_as_current_external_evidence(self) -> bool:
        return self.state == "APPLICABLE"


def resolve_territory_fact(
    context: TerritoryContext,
    fact: TerritoryFact,
    *,
    as_of: datetime,
) -> TerritoryFactResolution:
    """Evaluate scope + provenance + freshness without converting a claim into authority."""

    _require_aware(as_of, field="as_of")
    context_territory = context.effective_territory_code
    state: str
    reason: str

    if context_territory is None:
        state = "NEEDS_TERRITORY"
        reason = "Territory is unknown; the external claim cannot be applied by default."
    elif context_territory != fact.territory_code:
        state = "OUT_OF_SCOPE"
        reason = "The external claim belongs to a different territory."
    elif fact.jurisdiction_code is not None and context.jurisdiction_code is None:
        state = "NEEDS_JURISDICTION"
        reason = "The claim is jurisdiction-specific and the current jurisdiction is unknown."
    elif (
        fact.jurisdiction_code is not None
        and context.jurisdiction_code != fact.jurisdiction_code
    ):
        state = "OUT_OF_SCOPE"
        reason = "The external claim belongs to a different jurisdiction."
    elif as_of < fact.observed_at:
        state = "NOT_YET_OBSERVED"
        reason = "The requested evaluation time predates the evidence observation."
    elif as_of >= fact.revalidate_after:
        state = "STALE"
        reason = "The claim reached its explicit revalidation boundary."
    elif not fact.verified_source:
        state = "UNVERIFIED"
        reason = "The claim is user-declared and has not been independently verified."
    else:
        state = "APPLICABLE"
        reason = "The claim matches current scope and remains within its evidence freshness window."

    return TerritoryFactResolution(
        fact_id=fact.id,
        state=state,
        context_territory_code=context_territory,
        context_jurisdiction_code=context.jurisdiction_code,
        fact_territory_code=fact.territory_code,
        fact_jurisdiction_code=fact.jurisdiction_code,
        source_kind=fact.source_kind,
        source_ref=fact.source_ref,
        observed_at=fact.observed_at,
        revalidate_after=fact.revalidate_after,
        reason=reason,
    )


def resolve_territory_facts(
    context: TerritoryContext,
    facts: Iterable[TerritoryFact],
    *,
    as_of: datetime,
) -> tuple[TerritoryFactResolution, ...]:
    rows = tuple(facts)
    ids = tuple(row.id for row in rows)
    if len(ids) != len(set(ids)):
        raise ValidationError("territory fact IDs must be unique")
    return tuple(resolve_territory_fact(context, row, as_of=as_of) for row in rows)
