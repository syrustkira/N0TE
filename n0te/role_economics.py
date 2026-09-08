from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

ROLE_ECONOMICS_SCHEMA_VERSION = 1

ROLE_KEYS = {
    "PRODUCER",
    "MIX_ENGINEER",
    "MASTERING_ENGINEER",
    "SESSION_MUSICIAN",
    "MANAGER",
    "LIVE_PERFORMER",
}

DIMENSIONS = {
    "FEE",
    "DEPOSIT",
    "REVISIONS",
    "CANCELLATION",
    "PARTICIPATION",
    "COMMISSION",
    "EXPENSES",
    "USAGE",
    "LICENSING",
    "EXCLUSIVITY",
    "EXTERNAL_TERMS",
    "PROFITABILITY",
}

ROLE_REVIEW_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "PRODUCER": (
        "FEE",
        "DEPOSIT",
        "REVISIONS",
        "CANCELLATION",
        "PARTICIPATION",
        "EXPENSES",
        "LICENSING",
        "EXCLUSIVITY",
        "EXTERNAL_TERMS",
        "PROFITABILITY",
    ),
    "MIX_ENGINEER": (
        "FEE",
        "DEPOSIT",
        "REVISIONS",
        "CANCELLATION",
        "PARTICIPATION",
        "EXPENSES",
        "EXTERNAL_TERMS",
        "PROFITABILITY",
    ),
    "MASTERING_ENGINEER": (
        "FEE",
        "DEPOSIT",
        "REVISIONS",
        "CANCELLATION",
        "EXPENSES",
        "EXTERNAL_TERMS",
        "PROFITABILITY",
    ),
    "SESSION_MUSICIAN": (
        "FEE",
        "DEPOSIT",
        "CANCELLATION",
        "EXPENSES",
        "USAGE",
        "LICENSING",
        "EXCLUSIVITY",
        "EXTERNAL_TERMS",
        "PROFITABILITY",
    ),
    "MANAGER": (
        "COMMISSION",
        "CANCELLATION",
        "EXPENSES",
        "PARTICIPATION",
        "EXTERNAL_TERMS",
        "PROFITABILITY",
    ),
    "LIVE_PERFORMER": (
        "FEE",
        "DEPOSIT",
        "CANCELLATION",
        "EXPENSES",
        "USAGE",
        "EXCLUSIVITY",
        "EXTERNAL_TERMS",
        "PROFITABILITY",
    ),
}

EXTERNAL_FACT_KINDS = {
    "MARKET_RATE",
    "UNION_TERM",
    "TAX_TERM",
    "TERRITORY_TERM",
    "PROVIDER_TERM",
}
EXTERNAL_EVIDENCE_KINDS = {"USER_DECLARED", "OBSERVED", "PROVIDER_VERIFIED"}
PARTICIPATION_KINDS = {"POINTS", "BACKEND", "ROYALTY_PARTICIPATION", "OTHER"}

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_TERRITORY_RE = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,8})?$")


class RoleEconomicsError(RuntimeError):
    """Role-specific economic terms cannot be represented truthfully."""


def _text(value: str, field: str, maximum: int = 2_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    text = " ".join(value.split())
    if not text:
        raise RoleEconomicsError(f"{field} must not be empty")
    if len(text) > maximum:
        raise RoleEconomicsError(f"{field} is too long")
    return text


def _optional_text(value: str | None, field: str, maximum: int = 2_000) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum)


def _nonnegative_int(value: int, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise RoleEconomicsError(f"{field} must be non-negative")
    return value


def _basis_points(value: int, field: str) -> int:
    value = _nonnegative_int(value, field)
    if value > 10_000:
        raise RoleEconomicsError(f"{field} cannot exceed 10000 basis points")
    return value


def _iso_date(value: date | str, field: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a date or ISO date text")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise RoleEconomicsError(f"{field} must be an ISO calendar date") from exc


@dataclass(frozen=True)
class MoneyAmount:
    currency: str
    minor_units: int

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str):
            raise TypeError("currency must be text")
        currency = self.currency.strip().upper()
        if not _CURRENCY_RE.fullmatch(currency):
            raise RoleEconomicsError("currency must be a three-letter code")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "minor_units", _nonnegative_int(self.minor_units, "minor_units"))


@dataclass(frozen=True)
class RevisionTerms:
    included_revisions: int
    extra_revision_terms: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "included_revisions",
            _nonnegative_int(self.included_revisions, "included_revisions"),
        )
        object.__setattr__(
            self,
            "extra_revision_terms",
            _text(self.extra_revision_terms, "extra_revision_terms"),
        )

    @property
    def change_order_approved(self) -> bool:
        return False


@dataclass(frozen=True)
class ParticipationTerm:
    kind: str
    rate_basis_points: int
    basis: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise TypeError("participation kind must be text")
        kind = self.kind.strip().upper()
        if kind not in PARTICIPATION_KINDS:
            raise RoleEconomicsError(f"unsupported participation kind: {kind}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "rate_basis_points",
            _basis_points(self.rate_basis_points, "rate_basis_points"),
        )
        object.__setattr__(self, "basis", _text(self.basis, "participation basis"))

    @property
    def ownership_verified(self) -> bool:
        return False

    @property
    def royalty_entitlement_verified(self) -> bool:
        return False


@dataclass(frozen=True)
class ExternalEconomicFact:
    kind: str
    value: str
    territory: str
    source_ref: str
    evidence_kind: str
    observed_on: str
    valid_through: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise TypeError("external fact kind must be text")
        kind = self.kind.strip().upper()
        if kind not in EXTERNAL_FACT_KINDS:
            raise RoleEconomicsError(f"unsupported external fact kind: {kind}")
        object.__setattr__(self, "kind", kind)

        object.__setattr__(self, "value", _text(self.value, "external fact value"))
        object.__setattr__(self, "source_ref", _text(self.source_ref, "source_ref"))

        if not isinstance(self.territory, str):
            raise TypeError("territory must be text")
        territory = self.territory.strip().upper()
        if not _TERRITORY_RE.fullmatch(territory):
            raise RoleEconomicsError("territory must be an explicit country or subdivision code")
        object.__setattr__(self, "territory", territory)

        if not isinstance(self.evidence_kind, str):
            raise TypeError("evidence_kind must be text")
        evidence_kind = self.evidence_kind.strip().upper()
        if evidence_kind not in EXTERNAL_EVIDENCE_KINDS:
            raise RoleEconomicsError(f"unsupported evidence_kind: {evidence_kind}")
        object.__setattr__(self, "evidence_kind", evidence_kind)

        observed = _iso_date(self.observed_on, "observed_on")
        valid_through = _iso_date(self.valid_through, "valid_through")
        if valid_through < observed:
            raise RoleEconomicsError("valid_through cannot precede observed_on")
        object.__setattr__(self, "observed_on", observed.isoformat())
        object.__setattr__(self, "valid_through", valid_through.isoformat())

    def applicability(self, *, as_of: date | str, territory: str) -> str:
        current = _iso_date(as_of, "as_of")
        if not isinstance(territory, str):
            raise TypeError("territory must be text")
        requested_territory = territory.strip().upper()
        if not _TERRITORY_RE.fullmatch(requested_territory):
            raise RoleEconomicsError("territory must be an explicit country or subdivision code")
        observed = date.fromisoformat(self.observed_on)
        valid_through = date.fromisoformat(self.valid_through)
        if observed > current:
            return "FUTURE_EVIDENCE"
        if self.territory != requested_territory:
            return "OUT_OF_SCOPE"
        if self.evidence_kind == "USER_DECLARED":
            return "UNVERIFIED"
        if current > valid_through:
            return "STALE"
        return "APPLICABLE"


@dataclass(frozen=True)
class ProfitabilityScenario:
    expected_revenue: MoneyAmount
    expected_cost: MoneyAmount
    note: str

    def __post_init__(self) -> None:
        if not isinstance(self.expected_revenue, MoneyAmount):
            raise TypeError("expected_revenue must be MoneyAmount")
        if not isinstance(self.expected_cost, MoneyAmount):
            raise TypeError("expected_cost must be MoneyAmount")
        if self.expected_revenue.currency != self.expected_cost.currency:
            raise RoleEconomicsError("profitability scenario amounts must use one currency")
        object.__setattr__(self, "note", _text(self.note, "profitability note"))

    @property
    def projected_margin_minor_units(self) -> int:
        return self.expected_revenue.minor_units - self.expected_cost.minor_units

    @property
    def is_actual_profit(self) -> bool:
        return False


@dataclass(frozen=True)
class RoleOffer:
    role: str
    fee: MoneyAmount | None = None
    deposit: MoneyAmount | None = None
    revisions: RevisionTerms | None = None
    cancellation_terms: str | None = None
    participations: tuple[ParticipationTerm, ...] = ()
    commission_basis_points: int | None = None
    commission_basis: str | None = None
    expense_terms: str | None = None
    usage_terms: str | None = None
    licensing_terms: str | None = None
    exclusivity_terms: str | None = None
    external_facts: tuple[ExternalEconomicFact, ...] = ()
    profitability: ProfitabilityScenario | None = None
    not_applicable: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.role, str):
            raise TypeError("role must be text")
        role = self.role.strip().upper().replace(" ", "_").replace("-", "_")
        if role not in ROLE_KEYS:
            raise RoleEconomicsError(f"unsupported role: {role}")
        object.__setattr__(self, "role", role)

        for field in ("fee", "deposit"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, MoneyAmount):
                raise TypeError(f"{field} must be MoneyAmount or None")

        if self.fee is not None and self.deposit is not None and self.fee.currency != self.deposit.currency:
            raise RoleEconomicsError("fee and deposit must use the same currency")
        if self.fee is not None and self.deposit is not None and self.deposit.minor_units > self.fee.minor_units:
            raise RoleEconomicsError("deposit cannot exceed the stated fee")

        if self.revisions is not None and not isinstance(self.revisions, RevisionTerms):
            raise TypeError("revisions must be RevisionTerms or None")

        object.__setattr__(
            self,
            "cancellation_terms",
            _optional_text(self.cancellation_terms, "cancellation_terms"),
        )
        object.__setattr__(
            self,
            "expense_terms",
            _optional_text(self.expense_terms, "expense_terms"),
        )
        object.__setattr__(
            self,
            "usage_terms",
            _optional_text(self.usage_terms, "usage_terms"),
        )
        object.__setattr__(
            self,
            "licensing_terms",
            _optional_text(self.licensing_terms, "licensing_terms"),
        )
        object.__setattr__(
            self,
            "exclusivity_terms",
            _optional_text(self.exclusivity_terms, "exclusivity_terms"),
        )

        participations = tuple(self.participations)
        if not all(isinstance(term, ParticipationTerm) for term in participations):
            raise TypeError("participations must contain ParticipationTerm values")
        object.__setattr__(self, "participations", participations)

        if self.commission_basis_points is None:
            if self.commission_basis is not None:
                raise RoleEconomicsError("commission_basis requires commission_basis_points")
        else:
            object.__setattr__(
                self,
                "commission_basis_points",
                _basis_points(self.commission_basis_points, "commission_basis_points"),
            )
            object.__setattr__(
                self,
                "commission_basis",
                _text(self.commission_basis, "commission_basis"),
            )

        facts = tuple(self.external_facts)
        if not all(isinstance(fact, ExternalEconomicFact) for fact in facts):
            raise TypeError("external_facts must contain ExternalEconomicFact values")
        object.__setattr__(self, "external_facts", facts)

        if self.profitability is not None and not isinstance(self.profitability, ProfitabilityScenario):
            raise TypeError("profitability must be ProfitabilityScenario or None")

        if isinstance(self.not_applicable, (str, bytes)):
            raise TypeError("not_applicable must be a collection of dimensions")
        na: set[str] = set()
        for raw in self.not_applicable:
            if not isinstance(raw, str):
                raise TypeError("not_applicable dimensions must be text")
            dimension = raw.strip().upper()
            if dimension not in DIMENSIONS:
                raise RoleEconomicsError(f"unknown economic dimension: {dimension}")
            na.add(dimension)
        object.__setattr__(self, "not_applicable", frozenset(na))

        populated = self.populated_dimensions
        conflict = populated.intersection(self.not_applicable)
        if conflict:
            raise RoleEconomicsError(
                f"dimensions cannot be both populated and not applicable: {sorted(conflict)}"
            )

    @property
    def populated_dimensions(self) -> frozenset[str]:
        values: set[str] = set()
        if self.fee is not None:
            values.add("FEE")
        if self.deposit is not None:
            values.add("DEPOSIT")
        if self.revisions is not None:
            values.add("REVISIONS")
        if self.cancellation_terms is not None:
            values.add("CANCELLATION")
        if self.participations:
            values.add("PARTICIPATION")
        if self.commission_basis_points is not None:
            values.add("COMMISSION")
        if self.expense_terms is not None:
            values.add("EXPENSES")
        if self.usage_terms is not None:
            values.add("USAGE")
        if self.licensing_terms is not None:
            values.add("LICENSING")
        if self.exclusivity_terms is not None:
            values.add("EXCLUSIVITY")
        if self.external_facts:
            values.add("EXTERNAL_TERMS")
        if self.profitability is not None:
            values.add("PROFITABILITY")
        return frozenset(values)

    @property
    def deposit_received_verified(self) -> bool:
        return False

    @property
    def cancellation_enforceability_verified(self) -> bool:
        return False


@dataclass(frozen=True)
class RoleEconomicsAssessment:
    role: str
    review_dimensions: tuple[str, ...]
    addressed_dimensions: tuple[str, ...]
    unaddressed_dimensions: tuple[str, ...]
    external_fact_states: tuple[tuple[str, str, str], ...]
    questions: tuple[str, ...]

    @property
    def state(self) -> str:
        if self.unaddressed_dimensions:
            return "NEEDS_TERMS"
        if any(state != "APPLICABLE" for _, _, state in self.external_fact_states):
            return "NEEDS_EXTERNAL_EVIDENCE"
        return "REVIEWABLE"

    @property
    def market_rate_verified(self) -> bool:
        return False

    @property
    def payment_received_verified(self) -> bool:
        return False

    @property
    def legal_enforceability_verified(self) -> bool:
        return False

    @property
    def rights_verified(self) -> bool:
        return False

    @property
    def union_applicability_verified(self) -> bool:
        return False

    @property
    def tax_treatment_verified(self) -> bool:
        return False

    @property
    def signature_authority_granted(self) -> bool:
        return False

    @property
    def payment_authority_granted(self) -> bool:
        return False

    @property
    def spend_authority_granted(self) -> bool:
        return False

    @property
    def obligation_created(self) -> bool:
        return False

    @property
    def transaction_created(self) -> bool:
        return False

    @property
    def external_action_authorized(self) -> bool:
        return False


def review_role_offer(
    offer: RoleOffer,
    *,
    as_of: date | str,
    territory: str | None = None,
) -> RoleEconomicsAssessment:
    if not isinstance(offer, RoleOffer):
        raise TypeError("offer must be RoleOffer")

    dimensions = ROLE_REVIEW_DIMENSIONS[offer.role]
    addressed = offer.populated_dimensions.union(offer.not_applicable)
    unaddressed = tuple(dimension for dimension in dimensions if dimension not in addressed)
    addressed_for_role = tuple(dimension for dimension in dimensions if dimension in addressed)

    if offer.external_facts:
        if territory is None:
            external_states = tuple(
                (fact.kind, fact.source_ref, "NEEDS_TERRITORY") for fact in offer.external_facts
            )
        else:
            external_states = tuple(
                (
                    fact.kind,
                    fact.source_ref,
                    fact.applicability(as_of=as_of, territory=territory),
                )
                for fact in offer.external_facts
            )
    else:
        external_states = ()

    questions: list[str] = []
    for dimension in unaddressed:
        questions.append(f"Clarify whether {dimension.lower().replace('_', ' ')} applies and record the proposed term or mark it not applicable.")
    for kind, _source_ref, state in external_states:
        if state != "APPLICABLE":
            questions.append(
                f"Revalidate {kind.lower().replace('_', ' ')} evidence ({state.lower().replace('_', ' ')})."
            )

    return RoleEconomicsAssessment(
        role=offer.role,
        review_dimensions=dimensions,
        addressed_dimensions=addressed_for_role,
        unaddressed_dimensions=unaddressed,
        external_fact_states=external_states,
        questions=tuple(questions),
    )
