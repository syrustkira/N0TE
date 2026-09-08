from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from .professional_roles import ProfessionalRole, get_professional_role
from .relevance_broker import RelevanceContextBinding

CREATIVE_LENS_POLICY_VERSION = 1

FINDING_STANCES = ("SUPPORT", "CHALLENGE", "NEUTRAL", "INSUFFICIENT")
EVIDENCE_BASES = ("CANONICAL_EVIDENCE", "BOUNDED_INFERENCE", "INSUFFICIENT")
AUDIENCE_BASES = ("NOT_APPLICABLE", "SIMULATED", "OBSERVED")
ROOM_STATUSES = (
    "AGREEMENT",
    "DISAGREEMENT",
    "UNIQUE_CONCERN",
    "UNRESOLVED",
    "INSUFFICIENT_EVIDENCE",
)


class CreativePartnerLensError(ValueError):
    """Invalid Creative Partner lens input or unsafe synthesis state."""


class MixedLensContextError(CreativePartnerLensError):
    """Call-the-Room findings do not belong to one exact canonical context."""


class StaleLensContextError(CreativePartnerLensError):
    """Findings were produced for an older canonical context fingerprint."""


def _text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise CreativePartnerLensError(f"{field_name} must be text")
    normalized = " ".join(value.split())
    if not normalized:
        raise CreativePartnerLensError(f"{field_name} must not be empty")
    return normalized


def _token(value: object, field_name: str, allowed: Iterable[str]) -> str:
    token = _text(value, field_name).upper().replace("-", "_").replace(" ", "_")
    if token not in allowed:
        raise CreativePartnerLensError(f"unsupported {field_name}: {token}")
    return token


def _text_tuple(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise CreativePartnerLensError(f"{field_name} must be a tuple")
    normalized = tuple(_text(item, field_name) for item in value)
    if not allow_empty and not normalized:
        raise CreativePartnerLensError(f"{field_name} must not be empty")
    if len(normalized) != len(set(normalized)):
        raise CreativePartnerLensError(f"{field_name} must not contain duplicates")
    return normalized


@dataclass(frozen=True)
class CreativeLensDefinition:
    """One read/advice perspective over canonical Artist/Song truth.

    A lens changes questions, evidence emphasis, tradeoffs and explanation posture.
    It is not a professional identity, memory owner, agent or authority domain.
    """

    lens_id: str
    label: str
    purpose: str
    linked_professional_role_ids: tuple[str, ...]
    diagnostic_questions: tuple[str, ...]
    evidence_emphasis: tuple[str, ...]
    tradeoffs: tuple[str, ...]
    explanation_posture: str
    schema_version: int = CREATIVE_LENS_POLICY_VERSION
    grants_identity_authority: bool = field(default=False, init=False)
    grants_memory_authority: bool = field(default=False, init=False)
    grants_mutation_authority: bool = field(default=False, init=False)
    grants_execution_authority: bool = field(default=False, init=False)
    grants_external_action_authority: bool = field(default=False, init=False)
    grants_spend_authority: bool = field(default=False, init=False)
    grants_publication_authority: bool = field(default=False, init=False)
    grants_rights_authority: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        lens_id = _text(self.lens_id, "lens_id").upper().replace("-", "_").replace(" ", "_")
        if type(self.schema_version) is not int or self.schema_version != CREATIVE_LENS_POLICY_VERSION:
            raise CreativePartnerLensError(
                f"unsupported Creative Partner lens policy version: {self.schema_version}"
            )
        role_ids = tuple(
            role_id.upper()
            for role_id in _text_tuple(
                self.linked_professional_role_ids,
                "linked_professional_role_ids",
                allow_empty=True,
            )
        )
        if len(role_ids) != len(set(role_ids)):
            raise CreativePartnerLensError(
                "linked_professional_role_ids must remain unique after normalization"
            )
        for role_id in role_ids:
            role = get_professional_role(role_id)
            if role.grants_any_authority:
                raise CreativePartnerLensError(
                    f"linked professional role unexpectedly grants authority: {role_id}"
                )
        object.__setattr__(self, "lens_id", lens_id)
        object.__setattr__(self, "label", _text(self.label, "label"))
        object.__setattr__(self, "purpose", _text(self.purpose, "purpose"))
        object.__setattr__(self, "linked_professional_role_ids", role_ids)
        object.__setattr__(
            self,
            "diagnostic_questions",
            _text_tuple(self.diagnostic_questions, "diagnostic_questions"),
        )
        object.__setattr__(
            self,
            "evidence_emphasis",
            _text_tuple(self.evidence_emphasis, "evidence_emphasis"),
        )
        object.__setattr__(self, "tradeoffs", _text_tuple(self.tradeoffs, "tradeoffs"))
        object.__setattr__(
            self,
            "explanation_posture",
            _text(self.explanation_posture, "explanation_posture"),
        )

    @property
    def grants_any_authority(self) -> bool:
        return False

    @property
    def policy_signature(self) -> tuple[object, ...]:
        return (
            self.diagnostic_questions,
            self.evidence_emphasis,
            self.tradeoffs,
            self.explanation_posture,
        )

    def linked_professional_roles(self) -> tuple[ProfessionalRole, ...]:
        return tuple(
            get_professional_role(role_id)
            for role_id in self.linked_professional_role_ids
        )


def _lens(
    lens_id: str,
    label: str,
    purpose: str,
    linked_roles: tuple[str, ...],
    questions: tuple[str, ...],
    evidence: tuple[str, ...],
    tradeoffs: tuple[str, ...],
    posture: str,
) -> CreativeLensDefinition:
    return CreativeLensDefinition(
        lens_id=lens_id,
        label=label,
        purpose=purpose,
        linked_professional_role_ids=linked_roles,
        diagnostic_questions=questions,
        evidence_emphasis=evidence,
        tradeoffs=tradeoffs,
        explanation_posture=posture,
    )


CREATIVE_LENSES: Mapping[str, CreativeLensDefinition] = MappingProxyType(
    {
        lens.lens_id: lens
        for lens in (
            _lens(
                "PRODUCER_COPRODUCER",
                "Producer / Coproducer",
                "Protect the record's whole creative intent while turning the current Song into a coherent producible result.",
                ("R02",),
                (
                    "What is the record trying to become right now?",
                    "Which production choice most changes the emotional result?",
                    "What should stay untouched because it already serves the intent?",
                ),
                (
                    "artist intent and references",
                    "arrangement and production state",
                    "performance and version decisions",
                ),
                (
                    "distinctiveness versus coherence",
                    "ambition versus finishability",
                    "production density versus emotional focus",
                ),
                "Integrate musical, performance and production consequences before recommending the smallest useful move.",
            ),
            _lens(
                "SONGWRITER_COMPOSER",
                "Songwriter / Composer",
                "Evaluate the composition itself before production polish can hide or exaggerate its strengths.",
                ("R05",),
                (
                    "What is the song saying and where does that meaning peak?",
                    "Which melody, harmony, lyric or form choice carries the identity?",
                    "Would the composition still work with simpler production?",
                ),
                (
                    "lyrics, melody, harmony and form",
                    "motif and hook recurrence",
                    "composition lineage and writer intent",
                ),
                (
                    "clarity versus surprise",
                    "repetition versus development",
                    "craft convention versus authorial voice",
                ),
                "Reason from composition and meaning first, separating writing issues from production preferences.",
            ),
            _lens(
                "ARRANGER",
                "Arranger",
                "Examine how musical roles, density, transitions and section contrast unfold over time.",
                ("R02", "R05"),
                (
                    "Does every section have a distinct job?",
                    "What enters, leaves or changes to create forward motion?",
                    "Which role collision or empty space matters most?",
                ),
                (
                    "section map and transitions",
                    "instrument and register roles",
                    "density, contrast and pacing evidence",
                ),
                (
                    "continuity versus contrast",
                    "density versus space",
                    "predictability versus structural surprise",
                ),
                "Translate the Song's intent into time-based roles and transitions without treating one DAW layout as the arrangement.",
            ),
            _lens(
                "ENGINEER",
                "Engineer",
                "Separate technical translation and signal problems from taste while protecting intentional irregularity.",
                ("R03", "R04"),
                (
                    "What technical evidence could prevent the intended result from translating?",
                    "Which issue is measured or observed, and which is only preference?",
                    "Can the problem be verified before recommending a corrective move?",
                ),
                (
                    "engineering snapshots and monitoring context",
                    "references and translation evidence",
                    "exact version and delivery state",
                ),
                (
                    "technical margin versus intentional character",
                    "translation versus local excitement",
                    "correction versus preserving a chosen imperfection",
                ),
                "Lead with verifiable technical evidence, explicitly label uncertainty, and never turn engineering convention into taste law.",
            ),
            _lens(
                "TEACHER",
                "Teacher",
                "Help the artist understand and independently reproduce the relevant skill rather than merely receiving an answer.",
                (),
                (
                    "What does the artist already understand from real work?",
                    "What is the smallest concept or exercise that unlocks the next decision?",
                    "How can the artist prove the skill without hidden assistance?",
                ),
                (
                    "Skill Model evidence",
                    "current Song as learning context",
                    "prior attempts, assistance and corrections",
                ),
                (
                    "speed versus durable understanding",
                    "explanation depth versus creative flow",
                    "demonstration versus independent practice",
                ),
                "Explain only what changes the current decision, then return agency to the artist with an evidence-producing next step.",
            ),
            _lens(
                "A_AND_R_FINISH_ADVISOR",
                "A&R / Finish Advisor",
                "Assess bounded readiness, identity fit and the highest-leverage gap without pretending to predict hits.",
                ("R08",),
                (
                    "What is genuinely blocking a confident artist decision or release path?",
                    "Is this a finish problem, rewrite problem, positioning question or optional improvement?",
                    "Which gap has evidence and which is speculative market taste?",
                ),
                (
                    "catalog and readiness evidence",
                    "artist identity and references",
                    "audience or opportunity evidence when genuinely observed",
                ),
                (
                    "finish now versus keep developing",
                    "identity fit versus exploration",
                    "opportunity timing versus artistic readiness",
                ),
                "Make bounded repertoire and finish recommendations with explicit uncertainty and no hit-certainty score.",
            ),
            _lens(
                "CREATIVE_DIRECTOR",
                "Creative Director",
                "Test whether sound, visual language, narrative and presentation reinforce one intentional world.",
                (),
                (
                    "What single idea should the audience feel across sound and presentation?",
                    "Which element strengthens or dilutes the world?",
                    "What can remain flexible without weakening recognition?",
                ),
                (
                    "artist identity and current campaign context",
                    "Song mood, imagery and narrative",
                    "approved visual/content references",
                ),
                (
                    "consistency versus evolution",
                    "recognition versus novelty",
                    "concept strength versus production overhead",
                ),
                "Connect creative choices across media while keeping branding evidence softer than the artist's direct judgment.",
            ),
            _lens(
                "FIRST_LISTEN_AUDIENCE",
                "First-Listen / Audience",
                "Offer a bounded first-listen perspective while keeping simulation categorically separate from observed audience evidence.",
                (),
                (
                    "What is understandable or memorable on a first encounter?",
                    "Where might attention drift before context is learned?",
                    "Which conclusion is simulated and which is supported by real listener evidence?",
                ),
                (
                    "current audible/presented version",
                    "observed audience evidence only when source-bound",
                    "explicit simulated first-listen inference",
                ),
                (
                    "immediacy versus depth",
                    "clarity versus mystery",
                    "broad accessibility versus specific identity",
                ),
                "Label simulated reaction as inference, never as listener research, and preserve disagreement with real observed audience evidence.",
            ),
            _lens(
                "CHALLENGER",
                "Challenger",
                "Counter habitual or overly agreeable reasoning with one bounded alternative that protects artist authority.",
                (),
                (
                    "Which assumption is being treated as fixed without enough evidence?",
                    "What deliberate opposite or adjacent choice would reveal something useful?",
                    "What should not be challenged because evidence or artist intent already makes it non-negotiable?",
                ),
                (
                    "artist intent and explicit constraints",
                    "learned preference with confidence and counterexamples",
                    "prior experiments and rejected alternatives",
                ),
                (
                    "familiar strength versus discovery",
                    "coherence versus productive contradiction",
                    "personalization versus optionality",
                ),
                "Challenge one material assumption at a time, keep the experiment reversible, and never convert contrarianism into a command.",
            ),
            _lens(
                "PERFORMANCE_DIRECTOR",
                "Performance Director",
                "Judge whether timing, articulation, phrasing and delivery embody the intended emotion rather than merely conforming to a grid.",
                ("R06",),
                (
                    "What emotional job should this performance accomplish?",
                    "Which timing, articulation or phrasing choice sounds intentional?",
                    "Is an irregularity expressive, accidental or still unknown?",
                ),
                (
                    "takes and performance evidence",
                    "section intent and reference performances",
                    "feel, pocket, articulation and phrasing observations",
                ),
                (
                    "precision versus human feel",
                    "consistency versus expressive variation",
                    "technical cleanliness versus emotional urgency",
                ),
                "Describe performance consequences in musical language and avoid treating timing deviation as an automatic defect.",
            ),
            _lens(
                "RIGHTS_LINEAGE_STEWARD",
                "Rights / Lineage Steward",
                "Protect authorship, contribution, permissions, artifact lineage and evidence before creative work crosses consequential boundaries.",
                ("R21",),
                (
                    "What exact work, contribution or artifact is being relied on?",
                    "What is declared, confirmed, signed or provider-acknowledged?",
                    "Would this next use exceed the permission or evidence actually present?",
                ),
                (
                    "credits, splits and rights evidence",
                    "artifact/version provenance",
                    "permission, consent and provider receipts",
                ),
                (
                    "speed versus evidentiary completeness",
                    "creative reuse versus permission scope",
                    "latest artifact versus approved artifact",
                ),
                "Explain the strongest supported rights/lineage state without upgrading declarations into ownership or legal certainty.",
            ),
            _lens(
                "BUSINESS_MANAGER_LABEL_OPERATOR",
                "Business / Manager / Label Operator",
                "Evaluate the current decision as an obligation, opportunity and resource tradeoff without overriding the artist's creative authority.",
                ("R07", "R19"),
                (
                    "What obligation, dependency or opportunity changes the decision window?",
                    "What evidence supports the cost, value or readiness claim?",
                    "What is the smallest reversible action that preserves the artist's options?",
                ),
                (
                    "obligations and deadlines",
                    "opportunities and professional evidence",
                    "posted economics and relationship context",
                ),
                (
                    "creative focus versus business timing",
                    "option value versus commitment",
                    "resource cost versus strategic value",
                ),
                "Keep business advice evidence-bound and scenario-based, never converting a role title or economic estimate into authority.",
            ),
            _lens(
                "FUTURE_ARTIST_ARCHIVIST",
                "Future-Artist / Archivist",
                "Protect future readability, reversibility and creative lineage so today's decision remains understandable later.",
                ("R01",),
                (
                    "Will the future artist know what changed and why?",
                    "What source, version, dependency or decision would be painful to reconstruct?",
                    "Can this be preserved without freezing present-day experimentation?",
                ),
                (
                    "version and artifact lineage",
                    "decision history and source references",
                    "dependencies, recoverability and archival context",
                ),
                (
                    "creative speed versus future recoverability",
                    "cleanup versus preserving useful history",
                    "current convenience versus long-horizon portability",
                ),
                "Favor legible lineage and recoverable decisions without turning archival caution into resistance to experimentation.",
            ),
        )
    }
)

REQUIRED_BASE_LENS_IDS = tuple(CREATIVE_LENSES)


def get_creative_lens(lens_id: str) -> CreativeLensDefinition:
    key = _text(lens_id, "lens_id").upper().replace("-", "_").replace(" ", "_")
    try:
        return CREATIVE_LENSES[key]
    except KeyError as exc:
        raise CreativePartnerLensError(f"unknown Creative Partner lens: {lens_id}") from exc


@dataclass(frozen=True)
class CreativeLensInvocation:
    lens_id: str
    context_fingerprint: str
    schema_version: int = CREATIVE_LENS_POLICY_VERSION
    grants_any_authority: bool = field(default=False, init=False)
    mutates_canonical_truth: bool = field(default=False, init=False)
    owns_memory: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        lens = get_creative_lens(self.lens_id)
        if type(self.schema_version) is not int or self.schema_version != CREATIVE_LENS_POLICY_VERSION:
            raise CreativePartnerLensError(
                f"unsupported Creative Partner lens policy version: {self.schema_version}"
            )
        object.__setattr__(self, "lens_id", lens.lens_id)
        object.__setattr__(
            self,
            "context_fingerprint",
            _text(self.context_fingerprint, "context_fingerprint"),
        )

    @classmethod
    def bind(
        cls,
        lens_id: str,
        context: RelevanceContextBinding,
    ) -> "CreativeLensInvocation":
        if not isinstance(context, RelevanceContextBinding):
            raise TypeError("context must be RelevanceContextBinding")
        return cls(
            lens_id=get_creative_lens(lens_id).lens_id,
            context_fingerprint=context.fingerprint,
        )


@dataclass(frozen=True)
class PerspectiveFinding:
    """One lens's bounded claim about one proposition in one exact context."""

    lens_id: str
    context_fingerprint: str
    proposition_key: str
    stance: str
    claim: str
    rationale: str
    source_refs: tuple[str, ...]
    evidence_basis: str
    audience_basis: str = "NOT_APPLICABLE"
    audience_evidence_ref: str | None = None
    grants_any_authority: bool = field(default=False, init=False)
    mutates_canonical_truth: bool = field(default=False, init=False)
    records_artist_decision: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        lens = get_creative_lens(self.lens_id)
        stance = _token(self.stance, "stance", FINDING_STANCES)
        evidence_basis = _token(self.evidence_basis, "evidence_basis", EVIDENCE_BASES)
        audience_basis = _token(self.audience_basis, "audience_basis", AUDIENCE_BASES)
        source_refs = _text_tuple(
            self.source_refs,
            "source_refs",
            allow_empty=evidence_basis == "INSUFFICIENT",
        )
        audience_evidence_ref = (
            None
            if self.audience_evidence_ref is None
            else _text(self.audience_evidence_ref, "audience_evidence_ref")
        )

        if stance == "INSUFFICIENT" and evidence_basis != "INSUFFICIENT":
            raise CreativePartnerLensError(
                "INSUFFICIENT stance requires INSUFFICIENT evidence_basis"
            )
        if evidence_basis == "INSUFFICIENT" and stance != "INSUFFICIENT":
            raise CreativePartnerLensError(
                "INSUFFICIENT evidence_basis requires INSUFFICIENT stance"
            )
        if audience_basis == "SIMULATED":
            if lens.lens_id != "FIRST_LISTEN_AUDIENCE":
                raise CreativePartnerLensError(
                    "SIMULATED audience basis belongs only to FIRST_LISTEN_AUDIENCE"
                )
            if evidence_basis not in {"BOUNDED_INFERENCE", "INSUFFICIENT"}:
                raise CreativePartnerLensError(
                    "SIMULATED audience basis must remain BOUNDED_INFERENCE or INSUFFICIENT"
                )
            if audience_evidence_ref is not None:
                raise CreativePartnerLensError(
                    "SIMULATED audience basis cannot carry an observed audience evidence ref"
                )
        elif audience_basis == "OBSERVED":
            if evidence_basis != "CANONICAL_EVIDENCE":
                raise CreativePartnerLensError(
                    "OBSERVED audience basis requires CANONICAL_EVIDENCE"
                )
            if audience_evidence_ref is None:
                raise CreativePartnerLensError(
                    "OBSERVED audience basis requires audience_evidence_ref"
                )
            if audience_evidence_ref not in source_refs:
                raise CreativePartnerLensError(
                    "audience_evidence_ref must be present in source_refs"
                )
        elif audience_evidence_ref is not None:
            raise CreativePartnerLensError(
                "audience_evidence_ref requires OBSERVED audience basis"
            )

        if lens.lens_id == "FIRST_LISTEN_AUDIENCE" and audience_basis == "NOT_APPLICABLE":
            raise CreativePartnerLensError(
                "FIRST_LISTEN_AUDIENCE findings must declare SIMULATED or OBSERVED audience basis"
            )

        object.__setattr__(self, "lens_id", lens.lens_id)
        object.__setattr__(
            self,
            "context_fingerprint",
            _text(self.context_fingerprint, "context_fingerprint"),
        )
        object.__setattr__(
            self,
            "proposition_key",
            _text(self.proposition_key, "proposition_key"),
        )
        object.__setattr__(self, "stance", stance)
        object.__setattr__(self, "claim", _text(self.claim, "claim"))
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "evidence_basis", evidence_basis)
        object.__setattr__(self, "audience_basis", audience_basis)
        object.__setattr__(self, "audience_evidence_ref", audience_evidence_ref)


@dataclass(frozen=True)
class RoomTopic:
    proposition_key: str
    status: str
    findings: tuple[PerspectiveFinding, ...]
    support_lens_ids: tuple[str, ...]
    challenge_lens_ids: tuple[str, ...]
    neutral_lens_ids: tuple[str, ...]
    insufficient_lens_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    observed_audience_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proposition_key",
            _text(self.proposition_key, "proposition_key"),
        )
        object.__setattr__(self, "status", _token(self.status, "status", ROOM_STATUSES))


@dataclass(frozen=True)
class CallTheRoomSynthesis:
    context_fingerprint: str
    lens_ids: tuple[str, ...]
    topics: tuple[RoomTopic, ...]
    grants_any_authority: bool = field(default=False, init=False)
    mutates_canonical_truth: bool = field(default=False, init=False)
    records_artist_decision: bool = field(default=False, init=False)
    creates_memory: bool = field(default=False, init=False)

    @property
    def agreement_topics(self) -> tuple[RoomTopic, ...]:
        return tuple(topic for topic in self.topics if topic.status == "AGREEMENT")

    @property
    def disagreement_topics(self) -> tuple[RoomTopic, ...]:
        return tuple(topic for topic in self.topics if topic.status == "DISAGREEMENT")

    @property
    def unique_concerns(self) -> tuple[RoomTopic, ...]:
        return tuple(topic for topic in self.topics if topic.status == "UNIQUE_CONCERN")

    @property
    def unresolved_topics(self) -> tuple[RoomTopic, ...]:
        return tuple(
            topic
            for topic in self.topics
            if topic.status in {"UNRESOLVED", "INSUFFICIENT_EVIDENCE"}
        )


def _topic_status(findings: tuple[PerspectiveFinding, ...]) -> str:
    support = tuple(item for item in findings if item.stance == "SUPPORT")
    challenge = tuple(item for item in findings if item.stance == "CHALLENGE")
    neutral = tuple(item for item in findings if item.stance == "NEUTRAL")
    insufficient = tuple(item for item in findings if item.stance == "INSUFFICIENT")
    sufficient = support + challenge + neutral

    if not sufficient:
        return "INSUFFICIENT_EVIDENCE"
    if support and challenge:
        return "DISAGREEMENT"
    if len(challenge) == 1 and not support:
        return "UNIQUE_CONCERN"
    directional = support or challenge
    if len(directional) >= 2 and not neutral and not insufficient:
        return "AGREEMENT"
    return "UNRESOLVED"


def call_the_room(
    context: RelevanceContextBinding,
    findings: tuple[PerspectiveFinding, ...],
) -> CallTheRoomSynthesis:
    """Synthesize same-context findings without voting, scoring or deciding.

    Agreement is descriptive convergence among findings. It is never promoted to
    truth, approval or an artist decision.
    """

    if not isinstance(context, RelevanceContextBinding):
        raise TypeError("context must be RelevanceContextBinding")
    if not isinstance(findings, tuple) or not findings:
        raise CreativePartnerLensError("findings must be a non-empty tuple")
    if not all(isinstance(item, PerspectiveFinding) for item in findings):
        raise TypeError("all findings must be PerspectiveFinding")

    fingerprints = {item.context_fingerprint for item in findings}
    if len(fingerprints) != 1:
        raise MixedLensContextError(
            "Call the Room cannot synthesize findings from mixed canonical contexts"
        )
    (finding_fingerprint,) = tuple(fingerprints)
    if finding_fingerprint != context.fingerprint:
        raise StaleLensContextError(
            "Call the Room findings do not match the current canonical context"
        )

    seen: set[tuple[str, str]] = set()
    for item in findings:
        key = (item.lens_id, item.proposition_key)
        if key in seen:
            raise CreativePartnerLensError(
                "one lens may contribute at most one finding per proposition"
            )
        seen.add(key)

    by_proposition: dict[str, list[PerspectiveFinding]] = {}
    for item in findings:
        by_proposition.setdefault(item.proposition_key, []).append(item)

    topics: list[RoomTopic] = []
    for proposition_key, topic_findings_list in sorted(by_proposition.items()):
        topic_findings = tuple(
            sorted(topic_findings_list, key=lambda item: item.lens_id)
        )
        topics.append(
            RoomTopic(
                proposition_key=proposition_key,
                status=_topic_status(topic_findings),
                findings=topic_findings,
                support_lens_ids=tuple(
                    item.lens_id for item in topic_findings if item.stance == "SUPPORT"
                ),
                challenge_lens_ids=tuple(
                    item.lens_id for item in topic_findings if item.stance == "CHALLENGE"
                ),
                neutral_lens_ids=tuple(
                    item.lens_id for item in topic_findings if item.stance == "NEUTRAL"
                ),
                insufficient_lens_ids=tuple(
                    item.lens_id
                    for item in topic_findings
                    if item.stance == "INSUFFICIENT"
                ),
                source_refs=tuple(
                    sorted({ref for item in topic_findings for ref in item.source_refs})
                ),
                observed_audience_refs=tuple(
                    sorted(
                        {
                            item.audience_evidence_ref
                            for item in topic_findings
                            if item.audience_basis == "OBSERVED"
                            and item.audience_evidence_ref is not None
                        }
                    )
                ),
            )
        )

    return CallTheRoomSynthesis(
        context_fingerprint=context.fingerprint,
        lens_ids=tuple(sorted({item.lens_id for item in findings})),
        topics=tuple(topics),
    )
