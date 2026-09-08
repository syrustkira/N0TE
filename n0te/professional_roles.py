from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .career_roles import CORE_ROLE_DEFINITIONS, RoleDefinition
from .lineage import ValidationError
from .professional_handoffs import CORE_PRODUCTION_HANDOFF_SPECS, HandoffSpec

PROFESSIONAL_ROLE_ONTOLOGY_VERSION = 1
PROFESSIONAL_ROLE_SOURCE = "N0TE_PRODUCT_DB/MUSIC_PROFESSIONAL_MAP"
ROLE_SCOPE_TIERS = {"CORE", "REPRESENTATIVE"}
LENS_APPLICABILITY = {"PRIMARY", "SECONDARY", "NOT_APPLICABLE"}
LENS_IDS = tuple(f"L{index:02d}" for index in range(1, 71))
_LENS_ID_SET = frozenset(LENS_IDS)
_ROLE_ID_RE = re.compile(r"^R[0-9]{2}$")


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text")
    text = " ".join(value.split())
    if not text:
        raise ValidationError(f"{field_name} must not be empty")
    return text


def _text_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, tuple):
        raise ValidationError(f"{field_name} must be a tuple")
    cleaned = tuple(_require_text(value, field_name) for value in values)
    if not allow_empty and not cleaned:
        raise ValidationError(f"{field_name} must not be empty")
    if len(cleaned) != len(set(cleaned)):
        raise ValidationError(f"{field_name} must not contain duplicates")
    return cleaned


def _lens_ids(*specs: str) -> tuple[str, ...]:
    ids: list[str] = []
    for raw in specs:
        spec = _require_text(raw, "lens range").upper()
        if "-" not in spec:
            if spec not in _LENS_ID_SET:
                raise ValidationError(f"unknown professional lens id: {spec}")
            ids.append(spec)
            continue
        start_raw, end_raw = spec.split("-", 1)
        if start_raw not in _LENS_ID_SET or end_raw not in _LENS_ID_SET:
            raise ValidationError(f"unknown professional lens range: {spec}")
        start = int(start_raw[1:])
        end = int(end_raw[1:])
        if end < start:
            raise ValidationError(f"professional lens range is reversed: {spec}")
        ids.extend(f"L{index:02d}" for index in range(start, end + 1))
    canonical = tuple(dict.fromkeys(ids))
    if len(canonical) != len(ids):
        raise ValidationError("professional lens ranges overlap within one applicability set")
    return canonical


def _role_key(value: str) -> str:
    text = _require_text(value, "professional role")
    return " ".join(text.casefold().split())


def _lens_key(value: str) -> str:
    text = _require_text(value, "professional lens").upper()
    if not re.fullmatch(r"L[0-9]{2}", text) or text not in _LENS_ID_SET:
        raise ValidationError(f"unknown professional lens id: {value}")
    return text


@dataclass(frozen=True)
class ProfessionalRole:
    id: str
    scope_tier: str
    family: str
    label: str
    aliases: tuple[str, ...]
    primary_outcome: str
    lifecycle_jobs: tuple[str, ...]
    primary_lens_ids: tuple[str, ...]
    secondary_lens_ids: tuple[str, ...]
    key_inputs: tuple[str, ...]
    key_deliverables: tuple[str, ...]
    rights_economics_note: str
    health_risk_note: str
    handoff_summary: str
    career_role_id: str | None = None
    runtime_handoff_ids: tuple[str, ...] = ()
    grants_identity_authority: bool = field(default=False, init=False)
    grants_action_authority: bool = field(default=False, init=False)
    grants_execution_authority: bool = field(default=False, init=False)
    grants_external_action_authority: bool = field(default=False, init=False)
    grants_legal_authority: bool = field(default=False, init=False)
    grants_spend_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise ValidationError("professional role id must be text")
        role_id = self.id.strip().upper()
        if not _ROLE_ID_RE.fullmatch(role_id):
            raise ValidationError("professional role id must look like R01")
        object.__setattr__(self, "id", role_id)

        if not isinstance(self.scope_tier, str):
            raise ValidationError("professional role scope tier must be text")
        scope_tier = self.scope_tier.strip().upper()
        if scope_tier not in ROLE_SCOPE_TIERS:
            raise ValidationError(f"unsupported professional role scope tier: {scope_tier}")
        object.__setattr__(self, "scope_tier", scope_tier)

        for field_name in (
            "family",
            "label",
            "primary_outcome",
            "rights_economics_note",
            "health_risk_note",
            "handoff_summary",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )

        aliases = _text_tuple(self.aliases, "aliases")
        alias_keys = tuple(_role_key(alias) for alias in aliases)
        if len(alias_keys) != len(set(alias_keys)):
            raise ValidationError("professional role aliases must be unique ignoring case")
        object.__setattr__(self, "aliases", aliases)

        for field_name in ("lifecycle_jobs", "key_inputs", "key_deliverables"):
            object.__setattr__(
                self,
                field_name,
                _text_tuple(getattr(self, field_name), field_name),
            )

        for field_name in ("primary_lens_ids", "secondary_lens_ids"):
            values = getattr(self, field_name)
            if isinstance(values, (str, bytes)) or not isinstance(values, tuple):
                raise ValidationError(f"{field_name} must be a tuple")
            canonical = tuple(_lens_key(value) for value in values)
            if len(canonical) != len(set(canonical)):
                raise ValidationError(f"{field_name} must not contain duplicates")
            object.__setattr__(self, field_name, canonical)

        overlap = set(self.primary_lens_ids) & set(self.secondary_lens_ids)
        if overlap:
            raise ValidationError(
                "professional role primary and secondary lens sets must be disjoint"
            )

        if self.career_role_id is not None:
            if not isinstance(self.career_role_id, str):
                raise ValidationError("career_role_id must be text or None")
            career_role_id = self.career_role_id.strip().upper()
            if career_role_id not in CORE_ROLE_DEFINITIONS:
                raise ValidationError(
                    f"professional role links unknown career role: {career_role_id}"
                )
            object.__setattr__(self, "career_role_id", career_role_id)

        runtime_handoff_ids = _text_tuple(
            self.runtime_handoff_ids,
            "runtime_handoff_ids",
            allow_empty=True,
        )
        normalized_handoff_ids = tuple(value.upper() for value in runtime_handoff_ids)
        if len(normalized_handoff_ids) != len(set(normalized_handoff_ids)):
            raise ValidationError("runtime_handoff_ids must not contain duplicates")
        unknown_handoffs = tuple(
            value
            for value in normalized_handoff_ids
            if value not in CORE_PRODUCTION_HANDOFF_SPECS
        )
        if unknown_handoffs:
            raise ValidationError(
                "professional role links unknown runtime handoff ids: "
                + ", ".join(unknown_handoffs)
            )
        object.__setattr__(self, "runtime_handoff_ids", normalized_handoff_ids)

    @property
    def source(self) -> str:
        return PROFESSIONAL_ROLE_SOURCE

    @property
    def grants_any_authority(self) -> bool:
        return False

    def lens_applicability(self, lens_id: str) -> str:
        lens = _lens_key(lens_id)
        if lens in self.primary_lens_ids:
            return "PRIMARY"
        if lens in self.secondary_lens_ids:
            return "SECONDARY"
        return "NOT_APPLICABLE"


PROFESSIONAL_ROLES: Mapping[str, ProfessionalRole] = MappingProxyType({
    "R01": ProfessionalRole(
        id="R01",
        scope_tier="CORE",
        family="Creator/Artist",
        label="Artist / Featured Artist / Band",
        aliases=("Artist", "Featured Artist", "Band"),
        primary_outcome=(
            "Create, embody, finish, release and sustain distinctive music and a "
            "durable audience/career."
        ),
        lifecycle_jobs=(
            "purpose/identity",
            "write/perform",
            "produce/record",
            "finish",
            "release",
            "audience",
            "business",
            "learn",
        ),
        primary_lens_ids=_lens_ids("L01-L16", "L21-L25", "L31-L44", "L50-L70"),
        secondary_lens_ids=_lens_ids("L17-L20", "L26-L30", "L45-L49"),
        key_inputs=("creative intent", "songs", "performances", "team/evidence"),
        key_deliverables=(
            "approved recordings",
            "performances",
            "catalog",
            "public identity",
            "fan relationship",
        ),
        rights_economics_note=(
            "Composition/master interests, artist royalties, deals and "
            "expenses/revenue remain separately evidenced."
        ),
        health_risk_note=(
            "Voice, body, hearing, mental health, public exposure and touring fatigue "
            "remain separate safety evidence."
        ),
        handoff_summary="Connects to all creator, engineering, team, release and live roles.",
        career_role_id="ARTIST",
        runtime_handoff_ids=("H09",),
    ),
    "R02": ProfessionalRole(
        id="R02",
        scope_tier="CORE",
        family="Creation/Production",
        label="Producer / Co-producer / Record Producer",
        aliases=("Producer", "Co-producer", "Record Producer"),
        primary_outcome=(
            "Turn artist/song intent into a coherent finished record while leading "
            "creative, technical and project decisions."
        ),
        lifecycle_jobs=(
            "brief/pre-pro",
            "song/arrangement",
            "people/session",
            "record/edit",
            "mix/master supervision",
            "delivery/credits",
            "career",
        ),
        primary_lens_ids=_lens_ids(
            "L03-L18", "L21-L25", "L29-L30", "L41-L65", "L68-L70"
        ),
        secondary_lens_ids=_lens_ids(
            "L01-L02", "L19-L20", "L26-L28", "L31-L40", "L66-L67"
        ),
        key_inputs=(
            "artist intent",
            "song",
            "references",
            "budget",
            "contributors",
            "studio/tool context",
        ),
        key_deliverables=(
            "production plan",
            "session direction",
            "approved production",
            "credits",
            "delivery package",
        ),
        rights_economics_note=(
            "Fee/advance, publishing where applicable, producer points/backend, "
            "expenses and LOD/participations require separate agreement evidence."
        ),
        health_risk_note=(
            "Hearing, fatigue, session safety, power/duty-of-care and confidentiality "
            "remain explicit risks."
        ),
        handoff_summary=(
            "Artist to writers/musicians/recording/mix/master/manager/label and back."
        ),
        career_role_id="PRODUCER",
        runtime_handoff_ids=("H07", "H09"),
    ),
    "R03": ProfessionalRole(
        id="R03",
        scope_tier="CORE",
        family="Engineering",
        label="Mix Engineer",
        aliases=("Mix Engineer",),
        primary_outcome=(
            "Transform approved multitracks/stems into an emotionally and technically "
            "coherent mix that translates and is recallable."
        ),
        lifecycle_jobs=(
            "intake",
            "brief/reference",
            "session QC",
            "mix",
            "revision",
            "approval",
            "deliver/archive",
            "credit/payment/referral",
        ),
        primary_lens_ids=_lens_ids(
            "L03-L05",
            "L08",
            "L12",
            "L19",
            "L21-L25",
            "L29-L30",
            "L42-L65",
            "L68-L70",
        ),
        secondary_lens_ids=_lens_ids(
            "L01-L02",
            "L06-L07",
            "L09-L11",
            "L16-L18",
            "L20",
            "L26-L28",
            "L31-L41",
            "L66-L67",
        ),
        key_inputs=(
            "approved multitracks",
            "rough/reference",
            "notes",
            "tempo",
            "metadata",
            "delivery spec",
        ),
        key_deliverables=(
            "approved mix",
            "alternates/stems as agreed",
            "recall/archive",
            "notes/credits",
        ),
        rights_economics_note=(
            "Mix fee, revisions, credit and any royalty participation require "
            "separate agreement evidence."
        ),
        health_risk_note=(
            "Hearing/fatigue, ergonomics, confidentiality, client pressure and data "
            "loss remain explicit risks."
        ),
        handoff_summary="Producer/artist/editor to mix, then mix to mastering/artist/label.",
        career_role_id="MIX_ENGINEER",
        runtime_handoff_ids=("H07", "H08"),
    ),
    "R04": ProfessionalRole(
        id="R04",
        scope_tier="CORE",
        family="Engineering",
        label="Mastering Engineer",
        aliases=("Mastering Engineer",),
        primary_outcome=(
            "Create authoritative release-ready masters/sequence that translate "
            "across formats while preserving intent."
        ),
        lifecycle_jobs=(
            "intake/QC",
            "reference/sequence",
            "master",
            "compare",
            "revision",
            "approval",
            "formats/metadata",
            "delivery/archive",
        ),
        primary_lens_ids=_lens_ids(
            "L03-L05",
            "L08",
            "L12",
            "L20-L25",
            "L29-L30",
            "L42-L65",
            "L68-L70",
        ),
        secondary_lens_ids=_lens_ids(
            "L01-L02",
            "L06-L07",
            "L09-L11",
            "L16-L19",
            "L26-L28",
            "L31-L41",
            "L66-L67",
        ),
        key_inputs=(
            "approved mixes",
            "sequence",
            "metadata",
            "references",
            "release/delivery specs",
        ),
        key_deliverables=(
            "approved masters",
            "sequence",
            "alternates/formats",
            "QC/report/archive",
        ),
        rights_economics_note=(
            "Mastering fee, revisions, credit and any participation require separate "
            "agreement evidence."
        ),
        health_risk_note=(
            "Hearing/fatigue, monitoring calibration, confidentiality and "
            "irreversible-release risk remain explicit."
        ),
        handoff_summary=(
            "Mix/producer/artist to mastering, then mastering to label/distributor/"
            "manufacturing."
        ),
        career_role_id=None,
        runtime_handoff_ids=("H08", "H09"),
    ),
    "R05": ProfessionalRole(
        id="R05",
        scope_tier="CORE",
        family="Creation/Writing",
        label="Songwriter / Composer / Lyricist / Topliner",
        aliases=("Songwriter", "Composer", "Lyricist", "Topliner"),
        primary_outcome=(
            "Create protectable musical works and lyrics that serve artist/project "
            "intent and can be documented, pitched and developed."
        ),
        lifecycle_jobs=(
            "brief/idea",
            "write",
            "demo",
            "revision",
            "split/metadata",
            "pitch/record",
            "registration/collection",
            "catalog learning",
        ),
        primary_lens_ids=_lens_ids(
            "L01-L15", "L21-L30", "L31-L44", "L50-L52", "L59-L70"
        ),
        secondary_lens_ids=_lens_ids("L16-L20", "L45-L49", "L53-L58"),
        key_inputs=(
            "brief",
            "artist voice",
            "references",
            "collaborators",
            "lyrical/musical ideas",
        ),
        key_deliverables=(
            "composition/lyrics",
            "demo",
            "split/credit data",
            "work registration package",
        ),
        rights_economics_note=(
            "Writer share, publisher/admin, mechanical/performance/sync income and "
            "fees remain separate rights/economic evidence."
        ),
        health_risk_note=(
            "Voice/ergonomics/mental strain, authorship disputes and cultural/context "
            "risk remain explicit."
        ),
        handoff_summary="Artist/producer/publisher/A&R/sync/rights handoffs.",
        career_role_id="SONGWRITER",
        runtime_handoff_ids=(),
    ),
    "R06": ProfessionalRole(
        id="R06",
        scope_tier="CORE",
        family="Performance",
        label="Session Musician / Session Singer / Background Vocalist",
        aliases=("Session Musician", "Session Singer", "Background Vocalist"),
        primary_outcome=(
            "Deliver reliable, musically appropriate performances on time with "
            "correct session/credit/usage documentation."
        ),
        lifecycle_jobs=(
            "inquiry",
            "brief/chart/demo",
            "terms",
            "prepare",
            "perform/record",
            "revisions",
            "delivery",
            "credit/payment",
            "referral",
        ),
        primary_lens_ids=_lens_ids(
            "L04-L05",
            "L07-L08",
            "L13-L15",
            "L17-L18",
            "L21-L25",
            "L29-L30",
            "L41-L57",
            "L62-L70",
        ),
        secondary_lens_ids=_lens_ids(
            "L01-L03",
            "L06",
            "L09-L12",
            "L16",
            "L19-L20",
            "L26-L28",
            "L31-L40",
            "L58-L61",
        ),
        key_inputs=(
            "brief",
            "charts/demo",
            "session date",
            "technical requirements",
            "usage/terms",
        ),
        key_deliverables=(
            "performances/takes",
            "stems/files if remote",
            "session notes",
            "credit/usage evidence",
        ),
        rights_economics_note=(
            "Session fee, overtime/cartage/doubling and reuse/new-use/union terms "
            "apply only when separately evidenced."
        ),
        health_risk_note=(
            "Hearing, repetitive strain, voice, travel, session safety and fatigue "
            "remain separate safety evidence."
        ),
        handoff_summary=(
            "Producer/artist/contractor to session musician, then musician to "
            "producer/editor/rights."
        ),
        career_role_id=None,
        runtime_handoff_ids=(),
    ),
    "R07": ProfessionalRole(
        id="R07",
        scope_tier="CORE",
        family="Management",
        label="Artist Manager",
        aliases=("Artist Manager",),
        primary_outcome=(
            "Protect and advance the artist's whole career by coordinating strategy, "
            "people, obligations, opportunities and decisions with transparency and "
            "duty of care."
        ),
        lifecycle_jobs=(
            "mandate/agreement",
            "strategy",
            "priorities",
            "team",
            "opportunities",
            "negotiation coordination",
            "campaign/tour/business oversight",
            "review",
        ),
        primary_lens_ids=_lens_ids(
            "L01-L10", "L21-L25", "L29-L30", "L31-L44", "L47-L70"
        ),
        secondary_lens_ids=_lens_ids("L11-L20", "L26-L28", "L45-L46"),
        key_inputs=(
            "artist goals",
            "catalog",
            "finances",
            "obligations",
            "relationships",
            "opportunities",
            "evidence",
        ),
        key_deliverables=(
            "priorities",
            "plans",
            "team coordination",
            "decisions",
            "opportunities",
            "follow-up",
            "career records",
        ),
        rights_economics_note=(
            "Management commission, term and expenses remain governed by separate "
            "agreement and accounting evidence."
        ),
        health_risk_note=(
            "Burnout/duty-of-care, power asymmetry, conflicts, confidentiality and "
            "crisis/touring risk remain explicit."
        ),
        handoff_summary="Artist to all team, business, live and rights roles and back.",
        career_role_id="MANAGER",
        runtime_handoff_ids=(),
    ),
    "R08": ProfessionalRole(
        id="R08",
        scope_tier="CORE",
        family="A&R/Development",
        label="A&R / Artist Development",
        aliases=("A&R", "Artist Development"),
        primary_outcome=(
            "Discover/evaluate artists and songs, develop repertoire/fit, connect "
            "resources and make bounded investment/release recommendations."
        ),
        lifecycle_jobs=(
            "discover",
            "evaluate",
            "relationship",
            "repertoire/development",
            "deal input",
            "record/project support",
            "catalog/release review",
        ),
        primary_lens_ids=_lens_ids(
            "L03-L05",
            "L11-L16",
            "L21-L25",
            "L31-L44",
            "L53-L65",
            "L68-L70",
        ),
        secondary_lens_ids=_lens_ids(
            "L01-L02",
            "L06-L10",
            "L17-L20",
            "L26-L30",
            "L45-L52",
            "L66-L67",
        ),
        key_inputs=(
            "songs/catalog",
            "artist identity",
            "audience evidence",
            "budgets",
            "market/context",
            "team feedback",
        ),
        key_deliverables=(
            "evaluation",
            "development notes",
            "repertoire decisions",
            "introductions",
            "project/release recommendations",
        ),
        rights_economics_note=(
            "Salary/consulting and label/publisher economics do not resolve deal "
            "conflicts; conflicts require separate disclosure evidence."
        ),
        health_risk_note=(
            "Bias/power asymmetry, confidentiality, mental load and speculative "
            "certainty risk remain explicit."
        ),
        handoff_summary=(
            "Artist/manager/writer/producer to and from label, publisher and marketing."
        ),
        career_role_id=None,
        runtime_handoff_ids=(),
    ),
    "R19": ProfessionalRole(
        id="R19",
        scope_tier="REPRESENTATIVE",
        family="Management/Finance",
        label="Business Manager / Artist Accountant-facing Operator",
        aliases=("Business Manager", "Artist Accountant-facing Operator"),
        primary_outcome=(
            "Maintain truthful financial visibility, obligations, budgets and "
            "professional coordination with licensed specialists where required."
        ),
        lifecycle_jobs=(
            "accounts/data",
            "cash/budget",
            "payables/receivables",
            "royalty/tour/project review",
            "tax/accountant handoff",
            "report",
        ),
        primary_lens_ids=_lens_ids(
            "L29-L30", "L41-L43", "L48-L59", "L62-L70"
        ),
        secondary_lens_ids=_lens_ids(
            "L01-L10", "L21-L28", "L31-L40", "L44-L47", "L60-L61"
        ),
        key_inputs=(
            "posted financial evidence",
            "contracts",
            "statements",
            "budgets",
            "obligations",
        ),
        key_deliverables=(
            "reports",
            "budgets",
            "reconciliations",
            "payment/tax/accountant handoff evidence",
        ),
        rights_economics_note=(
            "Fees/commission, financial controls and tax/accounting boundaries require "
            "separate evidence and licensed-specialist review where applicable."
        ),
        health_risk_note=(
            "Fraud/security, conflicts, fiduciary/duty concerns and incomplete data "
            "remain explicit risks."
        ),
        handoff_summary=(
            "Artist/manager to and from bookkeeper, accountant, attorney and providers."
        ),
        career_role_id=None,
        runtime_handoff_ids=(),
    ),
    "R21": ProfessionalRole(
        id="R21",
        scope_tier="REPRESENTATIVE",
        family="Rights/Publishing",
        label="Music Publisher / Publishing Administrator",
        aliases=("Music Publisher", "Publishing Administrator"),
        primary_outcome=(
            "Administer musical works, registrations, licensing, royalty collection "
            "and songwriter/catalog opportunities."
        ),
        lifecycle_jobs=(
            "mandate",
            "work/split ingestion",
            "registration",
            "license/sync",
            "royalty/claims",
            "reconcile",
            "catalog strategy",
        ),
        primary_lens_ids=_lens_ids(
            "L25-L30", "L41-L43", "L50-L55", "L57-L70"
        ),
        secondary_lens_ids=_lens_ids(
            "L01-L24", "L31-L40", "L44-L49", "L56"
        ),
        key_inputs=(
            "works/splits",
            "agreements",
            "identifiers",
            "territory/provider data",
        ),
        key_deliverables=(
            "registrations",
            "licenses",
            "statements/reconciliations",
            "catalog/opportunity records",
        ),
        rights_economics_note=(
            "Publisher/admin share, royalties, commissions and territory mandates "
            "remain separate rights/economic evidence."
        ),
        health_risk_note=(
            "Rights errors, conflicts, privacy/security and jurisdiction volatility "
            "remain explicit risks."
        ),
        handoff_summary=(
            "Writer/manager to publisher/admin, then to PRO/MLC/CMO/licensees/sync."
        ),
        career_role_id=None,
        runtime_handoff_ids=(),
    ),
    "R29": ProfessionalRole(
        id="R29",
        scope_tier="REPRESENTATIVE",
        family="Live/Booking",
        label="Booking Agent",
        aliases=("Booking Agent",),
        primary_outcome=(
            "Secure appropriate live opportunities and negotiate routing/terms while "
            "managing promoter/artist relationships."
        ),
        lifecycle_jobs=(
            "strategy",
            "availability",
            "pitch",
            "offer",
            "negotiate",
            "contract",
            "advance handoff",
            "settlement follow-up",
        ),
        primary_lens_ids=_lens_ids("L29", "L31", "L41-L59", "L62-L70"),
        secondary_lens_ids=_lens_ids(
            "L01-L28", "L30", "L32-L40", "L60-L61"
        ),
        key_inputs=(
            "artist live proof",
            "availability",
            "draw/geo evidence",
            "contacts",
            "fee floor",
        ),
        key_deliverables=(
            "offers/contracts",
            "routing",
            "confirmed dates",
            "promoter relationship history",
        ),
        rights_economics_note=(
            "Commission, guarantees/door/backend, expenses and territory/agency rules "
            "require separate current evidence."
        ),
        health_risk_note=(
            "Travel/schedule load, conflicts, fraud and safety/reputation risk remain "
            "explicit."
        ),
        handoff_summary="Artist/manager to agent, then agent to promoter/tour manager.",
        career_role_id=None,
        runtime_handoff_ids=(),
    ),
})

CORE_PROFESSIONAL_ROLE_IDS = tuple(
    role.id for role in PROFESSIONAL_ROLES.values() if role.scope_tier == "CORE"
)
REPRESENTATIVE_PROFESSIONAL_ROLE_IDS = tuple(
    role.id
    for role in PROFESSIONAL_ROLES.values()
    if role.scope_tier == "REPRESENTATIVE"
)

_ALIAS_INDEX: dict[str, str] = {}
for _role in PROFESSIONAL_ROLES.values():
    for _candidate in (_role.id, _role.label, *_role.aliases):
        _key = _role_key(_candidate)
        existing = _ALIAS_INDEX.get(_key)
        if existing is not None and existing != _role.id:
            raise RuntimeError(
                f"professional role alias {_candidate!r} maps to both "
                f"{existing} and {_role.id}"
            )
        _ALIAS_INDEX[_key] = _role.id


def get_professional_role(role: str) -> ProfessionalRole:
    key = _role_key(role)
    role_id = _ALIAS_INDEX.get(key)
    if role_id is None:
        raise ValidationError(f"unknown professional role: {role}")
    return PROFESSIONAL_ROLES[role_id]


def list_professional_roles(*, scope_tier: str | None = None) -> tuple[ProfessionalRole, ...]:
    roles = tuple(PROFESSIONAL_ROLES.values())
    if scope_tier is None:
        return roles
    if not isinstance(scope_tier, str):
        raise ValidationError("professional role scope tier must be text")
    tier = scope_tier.strip().upper()
    if tier not in ROLE_SCOPE_TIERS:
        raise ValidationError(f"unsupported professional role scope tier: {tier}")
    return tuple(role for role in roles if role.scope_tier == tier)


def professional_lens_applicability(role: str, lens_id: str) -> str:
    return get_professional_role(role).lens_applicability(lens_id)


def linked_career_role(role: str) -> RoleDefinition | None:
    professional_role = get_professional_role(role)
    if professional_role.career_role_id is None:
        return None
    return CORE_ROLE_DEFINITIONS[professional_role.career_role_id]


def linked_runtime_handoffs(role: str) -> tuple[HandoffSpec, ...]:
    professional_role = get_professional_role(role)
    return tuple(
        CORE_PRODUCTION_HANDOFF_SPECS[handoff_id]
        for handoff_id in professional_role.runtime_handoff_ids
    )
