from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import (
    LineageCorruptionError,
    LineageStore,
    NotFoundError,
    ValidationError,
)
from .session import SessionMemory

SKILL_SCHEMA_VERSION = 1
SKILL_LEVELS = {
    "UNKNOWN",
    "INTRODUCED",
    "PRACTICED",
    "APPLIED",
    "INDEPENDENT",
}
SKILL_SOURCE_KINDS = {
    "ARTIST_DECLARED",
    "ARTIST_CORRECTION",
    "N0TE_ASSESSED",
    "OBSERVED",
}
_NON_ARTIST_SOURCES = {"N0TE_ASSESSED", "OBSERVED"}
_REAL_WORK_LEVELS = {"PRACTICED", "APPLIED", "INDEPENDENT"}


@dataclass(frozen=True)
class SkillAssessment:
    sequence: int
    id: str
    skill_id: str
    level: str
    source_kind: str
    source_ref: str
    confidence: float
    assistance_level: float
    session_id: str | None
    song_id: str | None
    note: str | None
    evidence_claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class SkillState:
    skill_id: str
    level: str
    latest_assessment: SkillAssessment | None


class SkillMemory:
    """Append-only demonstrated skill evidence.

    Session scratch, notes, and EvidenceMemory rows never mutate skill state
    automatically. A skill level changes only when record_assessment() or
    correct_skill() explicitly appends an assessment. History is never rewritten.
    """

    _TRIGGER_NAMES = {
        "skill_assessments_immutable_update",
        "skill_assessments_immutable_delete",
        "skill_evidence_immutable_update",
        "skill_evidence_immutable_delete",
        "skill_session_song_consistent",
        "skill_nonartist_real_work_requires_closed_session",
        "skill_independent_requires_zero_assistance",
        "skill_unknown_requires_artist_correction",
        "skill_artist_correction_requires_reason",
        "skill_assessment_activity",
    }

    def __init__(
        self,
        store: LineageStore,
        evidence: EvidenceMemory,
        sessions: SessionMemory,
    ):
        if not isinstance(store, LineageStore):
            raise TypeError("SkillMemory requires the canonical LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError("SkillMemory requires EvidenceMemory for the same LineageStore")
        if not isinstance(sessions, SessionMemory) or sessions.store is not store:
            raise TypeError("SkillMemory requires SessionMemory for the same LineageStore")
        self.store = store
        self.evidence = evidence
        self.sessions = sessions
        self._conn = store._conn
        self._ensure_schema()
        self._validate_existing()

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone() is not None

    def _metadata_value(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?",
            (key,),
        ).fetchone()
        return None if row is None else str(row["value"])

    def _ensure_schema(self) -> None:
        assessments = self._table_exists("skill_assessments")
        links = self._table_exists("skill_assessment_evidence")
        version = self._metadata_value("skill_schema_version")
        if assessments or links or version is not None:
            if not assessments or not links or version != str(SKILL_SCHEMA_VERSION):
                raise LineageCorruptionError("Skill schema metadata/table mismatch")
            return
        if (
            not self._table_exists("sessions")
            or not self._table_exists("evidence_claims")
            or not self._table_exists("activity_events")
        ):
            raise LineageCorruptionError(
                "SkillMemory requires canonical Session, Evidence and Activity first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE skill_assessments (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        skill_id TEXT NOT NULL CHECK(length(trim(skill_id)) > 0),
                        level TEXT NOT NULL CHECK(level IN (
                            'UNKNOWN','INTRODUCED','PRACTICED','APPLIED','INDEPENDENT'
                        )),
                        source_kind TEXT NOT NULL CHECK(source_kind IN (
                            'ARTIST_DECLARED','ARTIST_CORRECTION','N0TE_ASSESSED','OBSERVED'
                        )),
                        source_ref TEXT NOT NULL CHECK(length(trim(source_ref)) > 0),
                        confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
                        assistance_level REAL NOT NULL
                            CHECK(assistance_level >= 0.0 AND assistance_level <= 1.0),
                        session_id TEXT NULL REFERENCES sessions(id),
                        song_id TEXT NULL REFERENCES songs(id),
                        note TEXT NULL
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX skill_assessment_history "
                    "ON skill_assessments(skill_id,seq)"
                )
                self._conn.execute(
                    """CREATE TABLE skill_assessment_evidence (
                        assessment_id TEXT NOT NULL REFERENCES skill_assessments(id),
                        claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
                        PRIMARY KEY(assessment_id,claim_id)
                    )"""
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('skill_schema_version',?)",
                    (str(SKILL_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Skill memory") from exc

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER skill_assessments_immutable_update
            BEFORE UPDATE ON skill_assessments BEGIN
                SELECT RAISE(ABORT, 'Skill assessments are append-only');
            END""",
            """CREATE TRIGGER skill_assessments_immutable_delete
            BEFORE DELETE ON skill_assessments BEGIN
                SELECT RAISE(ABORT, 'Skill assessments are append-only');
            END""",
            """CREATE TRIGGER skill_evidence_immutable_update
            BEFORE UPDATE ON skill_assessment_evidence BEGIN
                SELECT RAISE(ABORT, 'Skill evidence links are append-only');
            END""",
            """CREATE TRIGGER skill_evidence_immutable_delete
            BEFORE DELETE ON skill_assessment_evidence BEGIN
                SELECT RAISE(ABORT, 'Skill evidence links are append-only');
            END""",
            """CREATE TRIGGER skill_session_song_consistent
            BEFORE INSERT ON skill_assessments
            WHEN NEW.session_id IS NOT NULL AND (
                NEW.song_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM sessions s
                    WHERE s.id=NEW.session_id AND s.song_id=NEW.song_id
                )
            ) BEGIN
                SELECT RAISE(ABORT, 'Skill assessment Session/Song binding is invalid');
            END""",
            """CREATE TRIGGER skill_nonartist_real_work_requires_closed_session
            BEFORE INSERT ON skill_assessments
            WHEN NEW.source_kind IN ('N0TE_ASSESSED','OBSERVED')
             AND NEW.level IN ('PRACTICED','APPLIED','INDEPENDENT')
             AND (
                NEW.session_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM sessions s
                    WHERE s.id=NEW.session_id
                      AND s.song_id=NEW.song_id
                      AND s.state='CLOSED'
                )
             )
            BEGIN
                SELECT RAISE(ABORT, 'non-artist real-work Skill assessment requires a closed Session');
            END""",
            """CREATE TRIGGER skill_independent_requires_zero_assistance
            BEFORE INSERT ON skill_assessments
            WHEN NEW.level='INDEPENDENT' AND NEW.assistance_level<>0.0
            BEGIN
                SELECT RAISE(ABORT, 'INDEPENDENT requires zero assistance');
            END""",
            """CREATE TRIGGER skill_unknown_requires_artist_correction
            BEFORE INSERT ON skill_assessments
            WHEN NEW.level='UNKNOWN' AND NEW.source_kind<>'ARTIST_CORRECTION'
            BEGIN
                SELECT RAISE(ABORT, 'UNKNOWN may be written only as an artist correction');
            END""",
            """CREATE TRIGGER skill_artist_correction_requires_reason
            BEFORE INSERT ON skill_assessments
            WHEN NEW.source_kind='ARTIST_CORRECTION'
             AND (NEW.note IS NULL OR length(trim(NEW.note))=0)
            BEGIN
                SELECT RAISE(ABORT, 'artist Skill correction requires a reason');
            END""",
            """CREATE TRIGGER skill_assessment_activity
            AFTER INSERT ON skill_assessments
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'SKILL_ASSESSED',
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    NEW.song_id,
                    CASE
                        WHEN NEW.session_id IS NULL THEN NULL
                        ELSE (SELECT version_id FROM sessions WHERE id=NEW.session_id)
                    END,
                    'SKILL_ASSESSMENT',
                    NEW.id,
                    '{}'
                );
            END""",
        )

    @staticmethod
    def _text(value: str, field: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValidationError(f"{field} must not be empty")
        return value

    @staticmethod
    def _unit(value: float, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} must be between 0 and 1") from exc
        if not 0.0 <= number <= 1.0:
            raise ValidationError(f"{field} must be between 0 and 1")
        return number

    @staticmethod
    def _optional_note(value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _evidence_ids(self, assessment_id: str) -> tuple[str, ...]:
        return tuple(
            str(row["claim_id"])
            for row in self._conn.execute(
                "SELECT claim_id FROM skill_assessment_evidence "
                "WHERE assessment_id=? ORDER BY claim_id",
                (assessment_id,),
            )
        )

    def _assessment(self, row: sqlite3.Row) -> SkillAssessment:
        assessment_id = str(row["id"])
        return SkillAssessment(
            sequence=int(row["seq"]),
            id=assessment_id,
            skill_id=str(row["skill_id"]),
            level=str(row["level"]),
            source_kind=str(row["source_kind"]),
            source_ref=str(row["source_ref"]),
            confidence=float(row["confidence"]),
            assistance_level=float(row["assistance_level"]),
            session_id=None if row["session_id"] is None else str(row["session_id"]),
            song_id=None if row["song_id"] is None else str(row["song_id"]),
            note=None if row["note"] is None else str(row["note"]),
            evidence_claim_ids=self._evidence_ids(assessment_id),
        )

    def _assessment_rows(self, skill_id: str):
        return self._conn.execute(
            "SELECT seq,id,skill_id,level,source_kind,source_ref,confidence,"
            "assistance_level,session_id,song_id,note "
            "FROM skill_assessments WHERE skill_id=? ORDER BY seq",
            (skill_id,),
        ).fetchall()

    def _validate_claim_for_song(
        self,
        claim: EvidenceClaim,
        song_id: str | None,
        *,
        corruption: bool = False,
    ) -> None:
        if song_id is None or claim.scope_kind in {"PROFILE", "ARTIST"}:
            return
        error = LineageCorruptionError if corruption else ValidationError
        if claim.scope_kind == "SONG":
            if claim.scope_id != song_id:
                raise error("Skill evidence claim belongs to a different Song")
            return
        if claim.scope_kind == "VERSION":
            version = self.store.get_version(claim.scope_id)
            if version is None or version.song_id != song_id:
                raise error("Skill evidence Version belongs to a different Song")

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("skill_schema_version") != str(SKILL_SCHEMA_VERSION):
                raise LineageCorruptionError("unsupported Skill schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'skill_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"Skill integrity hooks are incomplete: {sorted(missing)}"
                )
            for row in self._conn.execute(
                "SELECT seq,id,skill_id,level,source_kind,source_ref,confidence,"
                "assistance_level,session_id,song_id,note "
                "FROM skill_assessments ORDER BY seq"
            ):
                assessment = self._assessment(row)
                if assessment.level not in SKILL_LEVELS:
                    raise LineageCorruptionError("Skill assessment has invalid level")
                if assessment.source_kind not in SKILL_SOURCE_KINDS:
                    raise LineageCorruptionError("Skill assessment has invalid source")
                if not 0.0 <= assessment.confidence <= 1.0:
                    raise LineageCorruptionError("Skill assessment has invalid confidence")
                if not 0.0 <= assessment.assistance_level <= 1.0:
                    raise LineageCorruptionError("Skill assessment has invalid assistance")
                if assessment.level == "INDEPENDENT" and assessment.assistance_level != 0.0:
                    raise LineageCorruptionError("INDEPENDENT Skill assessment has assistance")
                if assessment.level == "UNKNOWN" and assessment.source_kind != "ARTIST_CORRECTION":
                    raise LineageCorruptionError("UNKNOWN Skill assessment lacks artist correction")
                if assessment.source_kind == "ARTIST_CORRECTION" and not assessment.note:
                    raise LineageCorruptionError("artist Skill correction lacks reason")
                session = None
                if assessment.session_id is not None:
                    session = self.sessions.get_session(assessment.session_id)
                    if session is None or session.song_id != assessment.song_id:
                        raise LineageCorruptionError("Skill assessment Session binding is invalid")
                if (
                    assessment.source_kind in _NON_ARTIST_SOURCES
                    and assessment.level in _REAL_WORK_LEVELS
                    and (session is None or session.state != "CLOSED")
                ):
                    raise LineageCorruptionError(
                        "non-artist real-work Skill assessment lacks closed Session"
                    )
                for claim_id in assessment.evidence_claim_ids:
                    claim = self.evidence.get_claim(claim_id)
                    if claim is None:
                        raise LineageCorruptionError("Skill assessment lost evidence claim")
                    self._validate_claim_for_song(
                        claim,
                        assessment.song_id,
                        corruption=True,
                    )
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            raise LineageCorruptionError("Skill memory is unreadable or corrupt") from exc

    def history(self, skill_id: str) -> tuple[SkillAssessment, ...]:
        skill_id = self._text(skill_id, "skill_id")
        return tuple(self._assessment(row) for row in self._assessment_rows(skill_id))

    def state(self, skill_id: str) -> SkillState:
        skill_id = self._text(skill_id, "skill_id")
        rows = self._assessment_rows(skill_id)
        if not rows:
            return SkillState(skill_id=skill_id, level="UNKNOWN", latest_assessment=None)
        latest = self._assessment(rows[-1])
        return SkillState(
            skill_id=skill_id,
            level=latest.level,
            latest_assessment=latest,
        )

    def record_assessment(
        self,
        *,
        skill_id: str,
        level: str,
        source_kind: str,
        source_ref: str,
        confidence: float = 1.0,
        assistance_level: float = 1.0,
        session_id: str | None = None,
        evidence_claim_ids: tuple[str, ...] = (),
        note: str | None = None,
    ) -> SkillAssessment:
        skill_id = self._text(skill_id, "skill_id")
        level = str(level).strip().upper()
        source_kind = str(source_kind).strip().upper()
        source_ref = self._text(source_ref, "source_ref")
        confidence = self._unit(confidence, "confidence")
        assistance_level = self._unit(assistance_level, "assistance_level")
        note = self._optional_note(note)
        if level not in SKILL_LEVELS:
            raise ValidationError(f"unsupported Skill level: {level}")
        if source_kind not in SKILL_SOURCE_KINDS:
            raise ValidationError(f"unsupported Skill assessment source: {source_kind}")
        if level == "UNKNOWN" and source_kind != "ARTIST_CORRECTION":
            raise ValidationError("UNKNOWN may be written only as an artist correction")
        if source_kind == "ARTIST_CORRECTION" and not note:
            raise ValidationError("artist Skill correction requires a reason")
        if level == "INDEPENDENT" and assistance_level != 0.0:
            raise ValidationError("INDEPENDENT requires assistance_level=0")

        session = None
        song_id = None
        if session_id is not None:
            session_id = self._text(session_id, "session_id")
            session = self.sessions.get_session(session_id)
            if session is None:
                raise NotFoundError(
                    f"Session not found in profile {self.store.profile_id}: {session_id}"
                )
            song_id = session.song_id
        if (
            source_kind in _NON_ARTIST_SOURCES
            and level in _REAL_WORK_LEVELS
            and (session is None or session.state != "CLOSED")
        ):
            raise ValidationError(
                "non-artist PRACTICED/APPLIED/INDEPENDENT assessment "
                "requires a closed Session"
            )

        claim_ids = tuple(dict.fromkeys(str(item) for item in evidence_claim_ids))
        claims: list[EvidenceClaim] = []
        for claim_id in claim_ids:
            claim = self.evidence.get_claim(claim_id)
            if claim is None:
                raise NotFoundError(f"evidence claim not found: {claim_id}")
            self._validate_claim_for_song(claim, song_id)
            claims.append(claim)

        assessment_id = f"skillassess_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO skill_assessments("
                    "id,skill_id,level,source_kind,source_ref,confidence,"
                    "assistance_level,session_id,song_id,note"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        assessment_id,
                        skill_id,
                        level,
                        source_kind,
                        source_ref,
                        confidence,
                        assistance_level,
                        session_id,
                        song_id,
                        note,
                    ),
                )
                for claim in claims:
                    self._conn.execute(
                        "INSERT INTO skill_assessment_evidence(assessment_id,claim_id) "
                        "VALUES(?,?)",
                        (assessment_id, claim.id),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot record Skill assessment: {exc}") from exc

        row = self._conn.execute(
            "SELECT seq,id,skill_id,level,source_kind,source_ref,confidence,"
            "assistance_level,session_id,song_id,note "
            "FROM skill_assessments WHERE id=?",
            (assessment_id,),
        ).fetchone()
        assert row is not None
        return self._assessment(row)

    def correct_skill(
        self,
        *,
        skill_id: str,
        level: str,
        source_ref: str,
        reason: str,
        confidence: float = 1.0,
        assistance_level: float = 1.0,
        session_id: str | None = None,
        evidence_claim_ids: tuple[str, ...] = (),
    ) -> SkillAssessment:
        return self.record_assessment(
            skill_id=skill_id,
            level=level,
            source_kind="ARTIST_CORRECTION",
            source_ref=source_ref,
            confidence=confidence,
            assistance_level=assistance_level,
            session_id=session_id,
            evidence_claim_ids=evidence_claim_ids,
            note=self._text(reason, "reason"),
        )
