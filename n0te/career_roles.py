from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .lineage import ValidationError
from .skills import SKILL_LEVELS, SkillState

ROLE_EVIDENCE_KINDS = {
    "WORK_SAMPLE",
    "PLANNING_ARTIFACT",
    "CREDIT",
    "COLLABORATOR_OUTCOME",
    "PUBLIC_DELIVERY",
    "REPEAT_DELIVERY",
    "RESPONSIBILITY",
    "MENTOR_FEEDBACK",
}
ROLE_EVIDENCE_SOURCE_KINDS = {
    "ARTIST_DECLARED",
    "N0TE_ASSESSED",
    "OBSERVED",
    "VERIFIED_EXTERNAL",
}
VERIFIED_ROLE_EVIDENCE_SOURCES = {"OBSERVED", "VERIFIED_EXTERNAL"}

_SKILL_RANK = {
    "UNKNOWN": 0,
    "INTRODUCED": 1,
    "PRACTICED": 2,
    "APPLIED": 3,
    "INDEPENDENT": 4,
}


@dataclass(frozen=True)
class SkillRequirement:
    skill_id: str
    minimum_level: str

    def __post_init__(self) -> None:
        if not str(self.skill_id).strip():
            raise ValidationError("role skill requirement needs a skill_id")
        if self.minimum_level not in SKILL_LEVELS:
            raise ValidationError(f"unsupported Skill level: {self.minimum_level}")


@dataclass(frozen=True)
class EvidenceRequirement:
    kind: str
    count: int = 1

    def __post_init__(self) -> None:
        if self.kind not in ROLE_EVIDENCE_KINDS:
            raise ValidationError(f"unsupported role evidence kind: {self.kind}")
        if self.count < 1:
            raise ValidationError("role evidence count must be positive")


@dataclass(frozen=True)
class RoleStageDefinition:
    id: str
    label: str
    skill_requirements: tuple[SkillRequirement, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]


@dataclass(frozen=True)
class RoleDefinition:
    id: str
    label: str
    responsibility: str
    stages: tuple[RoleStageDefinition, ...]


@dataclass(frozen=True)
class RoleEvidence:
    id: str
    role_ids: tuple[str, ...]
    kind: str
    source_kind: str
    source_ref: str
    note: str | None = None

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValidationError("role evidence needs an id")
        if not self.role_ids or any(not str(role).strip() for role in self.role_ids):
            raise ValidationError("role evidence must name at least one role")
        if self.kind not in ROLE_EVIDENCE_KINDS:
            raise ValidationError(f"unsupported role evidence kind: {self.kind}")
        if self.source_kind not in ROLE_EVIDENCE_SOURCE_KINDS:
            raise ValidationError(f"unsupported role evidence source: {self.source_kind}")
        if not str(self.source_ref).strip():
            raise ValidationError("role evidence needs a source_ref")

    @property
    def verified(self) -> bool:
        return self.source_kind in VERIFIED_ROLE_EVIDENCE_SOURCES


@dataclass(frozen=True)
class RoleAssessment:
    role_id: str
    attained_stage_ids: tuple[str, ...]
    current_stage_id: str | None
    next_stage_id: str | None
    missing_skills: tuple[SkillRequirement, ...]
    missing_evidence: tuple[EvidenceRequirement, ...]
    verified_evidence_ids: tuple[str, ...]
    declared_or_inferred_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RoleTransitionAssessment:
    source_role_id: str
    target_role_id: str
    source_stage_id: str | None
    target_stage_id: str | None
    preserved_skill_ids: tuple[str, ...]
    preserved_evidence_ids: tuple[str, ...]
    target_missing_skills: tuple[SkillRequirement, ...]
    target_missing_evidence: tuple[EvidenceRequirement, ...]
    smallest_next_evidence_step: str | None
    tradeoffs: tuple[str, ...]


def _req(skill_id: str, level: str) -> SkillRequirement:
    return SkillRequirement(skill_id, level)


def _ev(kind: str, count: int = 1) -> EvidenceRequirement:
    return EvidenceRequirement(kind, count)


CORE_ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    "ARTIST": RoleDefinition(
        id="ARTIST",
        label="Artist",
        responsibility="own artistic identity, finished work, catalog decisions and the relationship between intent and audience feedback",
        stages=(
            RoleStageDefinition(
                "ARTIST-DEVELOPING-WORK",
                "Developing finished artistic work",
                (_req("artistic_voice", "PRACTICED"), _req("song_or_performance_finishing", "PRACTICED")),
                (_ev("WORK_SAMPLE"),),
            ),
            RoleStageDefinition(
                "ARTIST-DELIVERING-WORK",
                "Delivering work into the world",
                (_req("artistic_voice", "APPLIED"), _req("release_delivery", "APPLIED"), _req("audience_learning", "PRACTICED")),
                (_ev("PUBLIC_DELIVERY"),),
            ),
            RoleStageDefinition(
                "ARTIST-OWNING-CATALOG",
                "Owning repeated catalog decisions",
                (_req("creative_direction", "INDEPENDENT"), _req("catalog_stewardship", "APPLIED")),
                (_ev("REPEAT_DELIVERY"), _ev("RESPONSIBILITY")),
            ),
        ),
    ),
    "PRODUCER": RoleDefinition(
        id="PRODUCER",
        label="Producer",
        responsibility="shape recordings and production decisions while aligning arrangement, sound, collaborators and delivery with the artist's intent",
        stages=(
            RoleStageDefinition(
                "PRODUCER-BUILDING-RECORDS",
                "Building coherent records",
                (_req("arrangement", "PRACTICED"), _req("sound_selection", "PRACTICED"), _req("daw_operation", "PRACTICED")),
                (_ev("WORK_SAMPLE"),),
            ),
            RoleStageDefinition(
                "PRODUCER-DELIVERING-WITH-OTHERS",
                "Delivering productions with collaborators",
                (_req("arrangement", "APPLIED"), _req("production_decision_making", "APPLIED"), _req("collaborator_communication", "APPLIED")),
                (_ev("CREDIT"), _ev("COLLABORATOR_OUTCOME")),
            ),
            RoleStageDefinition(
                "PRODUCER-OWNING-OUTCOMES",
                "Owning repeated production outcomes",
                (_req("creative_direction", "INDEPENDENT"), _req("production_delivery", "INDEPENDENT")),
                (_ev("REPEAT_DELIVERY"), _ev("RESPONSIBILITY")),
            ),
        ),
    ),
    "SONGWRITER": RoleDefinition(
        id="SONGWRITER",
        label="Songwriter",
        responsibility="develop songs, revise for artistic or brief constraints, collaborate clearly and preserve authorship and rights evidence",
        stages=(
            RoleStageDefinition(
                "SONGWRITER-DEVELOPING-SONGS",
                "Developing complete songs",
                (_req("lyric_or_concept_craft", "PRACTICED"), _req("melody_harmony_craft", "PRACTICED")),
                (_ev("WORK_SAMPLE"),),
            ),
            RoleStageDefinition(
                "SONGWRITER-DELIVERING-COLLABORATIONS",
                "Delivering collaborative writing",
                (_req("revision_for_brief", "APPLIED"), _req("collaborator_communication", "APPLIED")),
                (_ev("CREDIT"), _ev("COLLABORATOR_OUTCOME")),
            ),
            RoleStageDefinition(
                "SONGWRITER-OWNING-DELIVERY",
                "Owning repeated songwriting delivery",
                (_req("songwriting_delivery", "INDEPENDENT"), _req("rights_metadata_basics", "APPLIED")),
                (_ev("REPEAT_DELIVERY"), _ev("RESPONSIBILITY")),
            ),
        ),
    ),
    "RECORDING_ENGINEER": RoleDefinition(
        id="RECORDING_ENGINEER",
        label="Recording Engineer",
        responsibility="capture performances reliably while owning signal flow, troubleshooting, session safety and recorded-file delivery",
        stages=(
            RoleStageDefinition(
                "RECORDING-ENGINEER-CAPTURING-SESSIONS",
                "Capturing controlled sessions",
                (_req("signal_flow", "PRACTICED"), _req("session_capture", "PRACTICED")),
                (_ev("WORK_SAMPLE"),),
            ),
            RoleStageDefinition(
                "RECORDING-ENGINEER-DELIVERING-SESSIONS",
                "Delivering sessions for others",
                (_req("mic_technique", "APPLIED"), _req("session_troubleshooting", "APPLIED"), _req("file_delivery", "APPLIED")),
                (_ev("CREDIT"), _ev("COLLABORATOR_OUTCOME")),
            ),
            RoleStageDefinition(
                "RECORDING-ENGINEER-OWNING-SESSIONS",
                "Owning repeated session outcomes",
                (_req("session_capture", "INDEPENDENT"), _req("client_session_responsibility", "INDEPENDENT")),
                (_ev("REPEAT_DELIVERY"), _ev("RESPONSIBILITY")),
            ),
        ),
    ),
    "MIX_ENGINEER": RoleDefinition(
        id="MIX_ENGINEER",
        label="Mix Engineer",
        responsibility="turn multitrack material into translation-ready mixes while managing revisions, technical constraints and artist intent",
        stages=(
            RoleStageDefinition(
                "MIX-ENGINEER-BUILDING-MIXES",
                "Building controlled mixes",
                (_req("critical_listening", "PRACTICED"), _req("balance_eq_dynamics", "PRACTICED")),
                (_ev("WORK_SAMPLE"),),
            ),
            RoleStageDefinition(
                "MIX-ENGINEER-DELIVERING-MIXES",
                "Delivering mixes for others",
                (_req("translation_control", "APPLIED"), _req("revision_management", "APPLIED")),
                (_ev("CREDIT"), _ev("COLLABORATOR_OUTCOME")),
            ),
            RoleStageDefinition(
                "MIX-ENGINEER-OWNING-DELIVERY",
                "Owning repeated mix delivery",
                (_req("mix_delivery", "INDEPENDENT"), _req("client_communication", "APPLIED")),
                (_ev("REPEAT_DELIVERY"), _ev("RESPONSIBILITY")),
            ),
        ),
    ),
    "MANAGER": RoleDefinition(
        id="MANAGER",
        label="Manager",
        responsibility="coordinate career priorities, people, opportunities, rights/business context and follow-through without substituting prestige for evidence",
        stages=(
            RoleStageDefinition(
                "MANAGER-BUILDING-OPERATING-PRACTICE",
                "Building an operating practice",
                (_req("career_planning", "PRACTICED"), _req("stakeholder_communication", "PRACTICED")),
                (_ev("PLANNING_ARTIFACT"),),
            ),
            RoleStageDefinition(
                "MANAGER-COORDINATING-OUTCOMES",
                "Coordinating real artist outcomes",
                (_req("release_coordination", "APPLIED"), _req("rights_business_basics", "APPLIED"), _req("opportunity_tracking", "APPLIED")),
                (_ev("RESPONSIBILITY"), _ev("COLLABORATOR_OUTCOME")),
            ),
            RoleStageDefinition(
                "MANAGER-OWNING-REPEATABLE-OPERATIONS",
                "Owning repeatable artist operations",
                (_req("stakeholder_communication", "INDEPENDENT"), _req("business_coordination", "INDEPENDENT")),
                (_ev("REPEAT_DELIVERY"), _ev("RESPONSIBILITY", 2)),
            ),
        ),
    ),
}


def get_role_definition(role_id: str) -> RoleDefinition:
    key = str(role_id).strip().upper()
    try:
        return CORE_ROLE_DEFINITIONS[key]
    except KeyError as exc:
        raise ValidationError(f"unknown career role: {role_id}") from exc


def _skill_level(value: SkillState | str | None) -> str:
    if value is None:
        return "UNKNOWN"
    level = value.level if isinstance(value, SkillState) else str(value).strip().upper()
    if level not in _SKILL_RANK:
        raise ValidationError(f"unsupported Skill level: {level}")
    return level


def _missing_skills(
    stage: RoleStageDefinition,
    skill_states: Mapping[str, SkillState | str],
) -> tuple[SkillRequirement, ...]:
    missing = []
    for requirement in stage.skill_requirements:
        actual = _skill_level(skill_states.get(requirement.skill_id))
        if _SKILL_RANK[actual] < _SKILL_RANK[requirement.minimum_level]:
            missing.append(requirement)
    return tuple(missing)


def _verified_evidence_for_role(
    role_id: str,
    evidence: Iterable[RoleEvidence],
) -> tuple[RoleEvidence, ...]:
    return tuple(
        item for item in evidence if role_id in item.role_ids and item.verified
    )


def _missing_evidence(
    stage: RoleStageDefinition,
    verified: tuple[RoleEvidence, ...],
) -> tuple[EvidenceRequirement, ...]:
    sources = {kind: set() for kind in ROLE_EVIDENCE_KINDS}
    for item in verified:
        sources[item.kind].add(item.source_ref)
    return tuple(
        requirement
        for requirement in stage.evidence_requirements
        if len(sources[requirement.kind]) < requirement.count
    )


def assess_role(
    role_id: str,
    *,
    skill_states: Mapping[str, SkillState | str],
    evidence: Iterable[RoleEvidence] = (),
) -> RoleAssessment:
    definition = get_role_definition(role_id)
    evidence_rows = tuple(evidence)
    evidence_ids = tuple(item.id for item in evidence_rows)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValidationError("role evidence IDs must be unique")
    verified = _verified_evidence_for_role(definition.id, evidence_rows)
    declared_or_inferred = tuple(
        item
        for item in evidence_rows
        if definition.id in item.role_ids and not item.verified
    )

    attained: list[str] = []
    next_stage: RoleStageDefinition | None = None
    missing_skills: tuple[SkillRequirement, ...] = ()
    missing_evidence: tuple[EvidenceRequirement, ...] = ()
    for stage in definition.stages:
        stage_missing_skills = _missing_skills(stage, skill_states)
        stage_missing_evidence = _missing_evidence(stage, verified)
        if stage_missing_skills or stage_missing_evidence:
            next_stage = stage
            missing_skills = stage_missing_skills
            missing_evidence = stage_missing_evidence
            break
        attained.append(stage.id)

    return RoleAssessment(
        role_id=definition.id,
        attained_stage_ids=tuple(attained),
        current_stage_id=attained[-1] if attained else None,
        next_stage_id=None if next_stage is None else next_stage.id,
        missing_skills=missing_skills,
        missing_evidence=missing_evidence,
        verified_evidence_ids=tuple(item.id for item in verified),
        declared_or_inferred_evidence_ids=tuple(item.id for item in declared_or_inferred),
    )


def _target_skill_ids(definition: RoleDefinition) -> set[str]:
    return {
        requirement.skill_id
        for stage in definition.stages
        for requirement in stage.skill_requirements
    }


def _next_step(assessment: RoleAssessment) -> str | None:
    if assessment.next_stage_id is None:
        return None
    if assessment.missing_skills:
        requirement = assessment.missing_skills[0]
        return (
            f"Produce one bounded real-work artifact that demonstrates "
            f"{requirement.skill_id} at {requirement.minimum_level} level; "
            "record the evidence source and assistance used."
        )
    requirement = assessment.missing_evidence[0]
    return (
        f"Capture one verified {requirement.kind} evidence item toward "
        f"{assessment.next_stage_id}; keep artist declaration separate until "
        "the evidence is observed or externally verified."
    )


def assess_transition(
    source_role_id: str,
    target_role_id: str,
    *,
    skill_states: Mapping[str, SkillState | str],
    evidence: Iterable[RoleEvidence] = (),
) -> RoleTransitionAssessment:
    source = get_role_definition(source_role_id)
    target = get_role_definition(target_role_id)
    if source.id == target.id:
        raise ValidationError("career transition requires two different roles")

    evidence_rows = tuple(evidence)
    source_assessment = assess_role(
        source.id,
        skill_states=skill_states,
        evidence=evidence_rows,
    )
    target_assessment = assess_role(
        target.id,
        skill_states=skill_states,
        evidence=evidence_rows,
    )
    target_skills = _target_skill_ids(target)
    preserved_skills = tuple(
        sorted(
            skill_id
            for skill_id in target_skills
            if _SKILL_RANK[_skill_level(skill_states.get(skill_id))] >= _SKILL_RANK["PRACTICED"]
        )
    )
    preserved_evidence = tuple(
        item.id
        for item in evidence_rows
        if item.verified and target.id in item.role_ids
    )

    return RoleTransitionAssessment(
        source_role_id=source.id,
        target_role_id=target.id,
        source_stage_id=source_assessment.current_stage_id,
        target_stage_id=target_assessment.current_stage_id,
        preserved_skill_ids=preserved_skills,
        preserved_evidence_ids=preserved_evidence,
        target_missing_skills=target_assessment.missing_skills,
        target_missing_evidence=target_assessment.missing_evidence,
        smallest_next_evidence_step=_next_step(target_assessment),
        tradeoffs=(
            f"Moving toward {target.label} adds responsibility to {target.responsibility}.",
            f"Existing {source.label} evidence remains intact; this comparison does not downgrade or abandon the source role.",
            "Stage coverage is evidence of demonstrated work, not certification, seniority, popularity or guaranteed employability.",
        ),
    )
