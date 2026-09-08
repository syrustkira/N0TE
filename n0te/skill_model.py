from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .lineage import LineageCorruptionError, ValidationError
from .skills import SKILL_LEVELS, SkillAssessment, SkillMemory

_ARTIST_DECLARATION_LEVELS = {"INTRODUCED", "PRACTICED", "APPLIED", "INDEPENDENT"}
_ASSISTANCE_LEVELS = {"NONE": 0.0, "SOME": 0.5, "HIGH": 1.0}
_CONFIDENCE_LEVELS = {"LOW": 0.4, "MEDIUM": 0.7, "HIGH": 1.0}
_SOURCE_LABELS = {
    "ARTIST_DECLARED": "You told N0TE",
    "ARTIST_CORRECTION": "You corrected N0TE",
    "N0TE_ASSESSED": "N0TE assessment",
    "OBSERVED": "Observed in real work",
}


class SkillModelError(RuntimeError):
    """A consumer Skill Model operation cannot be applied safely."""


class StaleSkillModelError(SkillModelError):
    """The Skill state changed after the artist-facing action was rendered."""


@dataclass(frozen=True)
class SkillModelBinding:
    skill_id: str
    expected_latest_assessment_id: str


@dataclass(frozen=True)
class SkillModelView:
    skill_id: str
    level: str
    source_label: str
    confidence: float
    assistance_level: float
    assistance_label: str
    evidence_count: int
    correction_note: str | None
    latest_assessment_id: str


class SkillModelService:
    """Artist-facing authority over the canonical append-only Skill model.

    This service owns no Skill storage. It exposes current canonical state and
    permits only ARTIST_DECLARED and ARTIST_CORRECTION appends. Stale validation
    and correction append occur in one LineageStore transaction so an older page
    cannot overwrite a newer Skill assessment by racing between check and commit.
    """

    def __init__(self, skills: SkillMemory):
        if not isinstance(skills, SkillMemory):
            raise TypeError("SkillModelService requires canonical SkillMemory")
        self.skills = skills
        self.store = skills.store

    @staticmethod
    def _skill_name(value: str) -> str:
        name = " ".join(str(value).split())
        if not name:
            raise ValidationError("Skill name must not be empty")
        if len(name) > 120:
            raise ValidationError("Skill name is too long")
        return name

    @staticmethod
    def _level(value: str, *, correction: bool) -> str:
        level = str(value).strip().upper()
        allowed = SKILL_LEVELS if correction else _ARTIST_DECLARATION_LEVELS
        if level not in allowed:
            raise ValidationError(f"unsupported artist Skill level: {level}")
        return level

    @staticmethod
    def _assistance(value: str, level: str) -> float:
        if level == "UNKNOWN":
            return 1.0
        key = str(value).strip().upper()
        if key not in _ASSISTANCE_LEVELS:
            raise ValidationError("unsupported assistance level")
        assistance = _ASSISTANCE_LEVELS[key]
        if level == "INDEPENDENT" and assistance != 0.0:
            raise ValidationError("INDEPENDENT means no assistance")
        return assistance

    @staticmethod
    def _confidence(value: str) -> float:
        key = str(value).strip().upper()
        if key not in _CONFIDENCE_LEVELS:
            raise ValidationError("unsupported confidence level")
        return _CONFIDENCE_LEVELS[key]

    @staticmethod
    def _assistance_label(value: float) -> str:
        if value == 0.0:
            return "No assistance"
        if value <= 0.5:
            return "Some assistance"
        return "High assistance"

    def _latest_row(self, skill_id: str) -> sqlite3.Row | None:
        return self.store._conn.execute(
            "SELECT seq,id,skill_id,level,source_kind,source_ref,confidence,"
            "assistance_level,session_id,song_id,note FROM skill_assessments "
            "WHERE skill_id=? ORDER BY seq DESC LIMIT 1",
            (skill_id,),
        ).fetchone()

    def views(self) -> tuple[SkillModelView, ...]:
        rows = self.store._conn.execute(
            "SELECT DISTINCT skill_id FROM skill_assessments ORDER BY lower(skill_id), skill_id"
        ).fetchall()
        views: list[SkillModelView] = []
        for row in rows:
            state = self.skills.state(str(row["skill_id"]))
            assessment = state.latest_assessment
            if assessment is None:
                continue
            assistance_label = (
                "Assistance not established"
                if state.level == "UNKNOWN"
                else self._assistance_label(assessment.assistance_level)
            )
            views.append(
                SkillModelView(
                    skill_id=state.skill_id,
                    level=state.level,
                    source_label=_SOURCE_LABELS.get(assessment.source_kind, "Recorded assessment"),
                    confidence=assessment.confidence,
                    assistance_level=assessment.assistance_level,
                    assistance_label=assistance_label,
                    evidence_count=len(assessment.evidence_claim_ids),
                    correction_note=(
                        assessment.note if assessment.source_kind == "ARTIST_CORRECTION" else None
                    ),
                    latest_assessment_id=assessment.id,
                )
            )
        return tuple(views)

    def binding_for(self, skill_id: str) -> SkillModelBinding:
        skill_id = self._skill_name(skill_id)
        state = self.skills.state(skill_id)
        if state.latest_assessment is None:
            raise SkillModelError("Skill has no recorded assessment to correct")
        return SkillModelBinding(skill_id, state.latest_assessment.id)

    def declare(
        self,
        *,
        skill_id: str,
        level: str,
        assistance: str,
        confidence: str = "HIGH",
    ) -> SkillAssessment:
        skill_id = self._skill_name(skill_id)
        level = self._level(level, correction=False)
        assistance_level = self._assistance(assistance, level)
        confidence_value = self._confidence(confidence)
        assessment_id = f"skillassess_{uuid.uuid4().hex}"
        source_ref = f"consumer-skill-declaration:{uuid.uuid4().hex}"

        try:
            with self.store._tx():
                if self._latest_row(skill_id) is not None:
                    raise StaleSkillModelError(
                        "That Skill is already recorded. Reload and correct its current state instead."
                    )
                self.store._conn.execute(
                    "INSERT INTO skill_assessments("
                    "id,skill_id,level,source_kind,source_ref,confidence,assistance_level,"
                    "session_id,song_id,note) VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)",
                    (
                        assessment_id,
                        skill_id,
                        level,
                        "ARTIST_DECLARED",
                        source_ref,
                        confidence_value,
                        assistance_level,
                    ),
                )
        except StaleSkillModelError:
            raise
        except sqlite3.IntegrityError as exc:
            raise SkillModelError(f"Skill declaration was rejected safely: {exc}") from exc

        state = self.skills.state(skill_id)
        if state.latest_assessment is None or state.latest_assessment.id != assessment_id:
            raise LineageCorruptionError("declared Skill assessment did not become canonical latest state")
        return state.latest_assessment

    def correct(
        self,
        binding: SkillModelBinding,
        *,
        level: str,
        assistance: str,
        reason: str,
        confidence: str = "HIGH",
    ) -> SkillAssessment:
        if not isinstance(binding, SkillModelBinding):
            raise TypeError("binding must be SkillModelBinding")
        skill_id = self._skill_name(binding.skill_id)
        level = self._level(level, correction=True)
        assistance_level = self._assistance(assistance, level)
        confidence_value = self._confidence(confidence)
        reason = " ".join(str(reason).split())
        if not reason:
            raise ValidationError("Skill correction reason must not be empty")
        if len(reason) > 500:
            raise ValidationError("Skill correction reason is too long")

        assessment_id = f"skillassess_{uuid.uuid4().hex}"
        source_ref = f"consumer-skill-correction:{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                latest = self._latest_row(skill_id)
                if latest is None or str(latest["id"]) != binding.expected_latest_assessment_id:
                    raise StaleSkillModelError(
                        "That Skill changed after this page was prepared. Reload before correcting it."
                    )
                self.store._conn.execute(
                    "INSERT INTO skill_assessments("
                    "id,skill_id,level,source_kind,source_ref,confidence,assistance_level,"
                    "session_id,song_id,note) VALUES(?,?,?,?,?,?,?,NULL,NULL,?)",
                    (
                        assessment_id,
                        skill_id,
                        level,
                        "ARTIST_CORRECTION",
                        source_ref,
                        confidence_value,
                        assistance_level,
                        reason,
                    ),
                )
        except StaleSkillModelError:
            raise
        except sqlite3.IntegrityError as exc:
            raise SkillModelError(f"Skill correction was rejected safely: {exc}") from exc

        state = self.skills.state(skill_id)
        if state.latest_assessment is None or state.latest_assessment.id != assessment_id:
            raise LineageCorruptionError("corrected Skill assessment did not become canonical latest state")
        return state.latest_assessment
