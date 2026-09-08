from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Iterable

from .evidence import SOURCE_KINDS
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .session import SessionMemory

LEARNING_SCHEMA_VERSION = 1
DECISION_KINDS = {"KEEP", "REVERT", "REVISE", "INCONCLUSIVE"}


@dataclass(frozen=True)
class ConsequenceObservation:
    sequence: int
    id: str
    episode_id: str
    observation: str
    source_kind: str
    source_ref: str
    confidence: float
    conditions: tuple[str, ...]
    confounders: tuple[str, ...]


@dataclass(frozen=True)
class LearningDecision:
    id: str
    episode_id: str
    decision: str
    rationale: str
    confidence: float


@dataclass(frozen=True)
class LearningEpisode:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    version_id: str | None
    session_id: str
    domain: str
    subject_ref: str
    change_description: str
    consequences: tuple[ConsequenceObservation, ...]
    decision: LearningDecision | None


class LearningMemory:
    """Explicit change → observed consequence → decision memory.

    This ledger preserves temporal/decision evidence only. It never asserts that
    the represented change caused an observed consequence and never mutates
    SkillMemory, EvidenceMemory, taste/Twin state, friction rules, or success rules.
    """

    _TRIGGER_NAMES = {
        "learning_episodes_immutable_update",
        "learning_episodes_immutable_delete",
        "learning_consequences_open_only",
        "learning_consequences_immutable_update",
        "learning_consequences_immutable_delete",
        "learning_decisions_immutable_update",
        "learning_decisions_immutable_delete",
        "learning_episode_activity",
        "learning_consequence_activity",
        "learning_decision_activity",
    }

    def __init__(self, store: LineageStore, sessions: SessionMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("LearningMemory requires the canonical LineageStore")
        if not isinstance(sessions, SessionMemory) or sessions.store is not store:
            raise TypeError(
                "LearningMemory requires SessionMemory for the same LineageStore"
            )
        self.store = store
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
        names = ("learning_episodes", "learning_consequences", "learning_decisions")
        exists = {name: self._table_exists(name) for name in names}
        version = self._metadata_value("learning_schema_version")
        if any(exists.values()) or version is not None:
            if not all(exists.values()) or version != str(LEARNING_SCHEMA_VERSION):
                raise LineageCorruptionError("Learning schema metadata/table mismatch")
            return
        if not self._table_exists("sessions") or not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "LearningMemory requires canonical Session and Activity first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE learning_episodes (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        version_id TEXT NULL REFERENCES versions(id),
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        domain TEXT NOT NULL CHECK(length(trim(domain)) > 0),
                        subject_ref TEXT NOT NULL CHECK(length(trim(subject_ref)) > 0),
                        change_description TEXT NOT NULL
                            CHECK(length(trim(change_description)) > 0)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX learning_episode_song ON learning_episodes(song_id,seq)"
                )
                self._conn.execute(
                    """CREATE TABLE learning_consequences (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        episode_id TEXT NOT NULL REFERENCES learning_episodes(id),
                        observation TEXT NOT NULL CHECK(length(trim(observation)) > 0),
                        source_kind TEXT NOT NULL CHECK(source_kind IN (
                            'USER_DECLARED','OBSERVED','MEASURED','PROVIDER_VERIFIED',
                            'REMEMBERED','INFERRED'
                        )),
                        source_ref TEXT NOT NULL CHECK(length(trim(source_ref)) > 0),
                        confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
                        conditions_json TEXT NOT NULL,
                        confounders_json TEXT NOT NULL
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX learning_consequence_episode "
                    "ON learning_consequences(episode_id,seq)"
                )
                self._conn.execute(
                    """CREATE TABLE learning_decisions (
                        id TEXT PRIMARY KEY,
                        episode_id TEXT NOT NULL UNIQUE REFERENCES learning_episodes(id),
                        decision TEXT NOT NULL CHECK(decision IN (
                            'KEEP','REVERT','REVISE','INCONCLUSIVE'
                        )),
                        rationale TEXT NOT NULL CHECK(length(trim(rationale)) > 0),
                        confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0)
                    )"""
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('learning_schema_version',?)",
                    (str(LEARNING_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Learning memory") from exc

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER learning_episodes_immutable_update
            BEFORE UPDATE ON learning_episodes BEGIN
                SELECT RAISE(ABORT, 'Learning episodes are immutable');
            END""",
            """CREATE TRIGGER learning_episodes_immutable_delete
            BEFORE DELETE ON learning_episodes BEGIN
                SELECT RAISE(ABORT, 'Learning episodes are immutable');
            END""",
            """CREATE TRIGGER learning_consequences_open_only
            BEFORE INSERT ON learning_consequences
            WHEN EXISTS (
                SELECT 1 FROM learning_decisions d WHERE d.episode_id=NEW.episode_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'cannot append consequence after Learning decision');
            END""",
            """CREATE TRIGGER learning_consequences_immutable_update
            BEFORE UPDATE ON learning_consequences BEGIN
                SELECT RAISE(ABORT, 'Learning consequences are append-only');
            END""",
            """CREATE TRIGGER learning_consequences_immutable_delete
            BEFORE DELETE ON learning_consequences BEGIN
                SELECT RAISE(ABORT, 'Learning consequences are append-only');
            END""",
            """CREATE TRIGGER learning_decisions_immutable_update
            BEFORE UPDATE ON learning_decisions BEGIN
                SELECT RAISE(ABORT, 'Learning decisions are immutable');
            END""",
            """CREATE TRIGGER learning_decisions_immutable_delete
            BEFORE DELETE ON learning_decisions BEGIN
                SELECT RAISE(ABORT, 'Learning decisions are immutable');
            END""",
            """CREATE TRIGGER learning_episode_activity
            AFTER INSERT ON learning_episodes
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'LEARNING_EPISODE_STARTED',
                    NEW.artist_id,NEW.song_id,NEW.version_id,
                    'LEARNING_EPISODE',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER learning_consequence_activity
            AFTER INSERT ON learning_consequences
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'LEARNING_CONSEQUENCE_RECORDED',
                    e.artist_id,e.song_id,e.version_id,
                    'LEARNING_CONSEQUENCE',NEW.id,'{}'
                FROM learning_episodes e WHERE e.id=NEW.episode_id;
            END""",
            """CREATE TRIGGER learning_decision_activity
            AFTER INSERT ON learning_decisions
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'LEARNING_DECISION_RECORDED',
                    e.artist_id,e.song_id,e.version_id,
                    'LEARNING_DECISION',NEW.id,
                    '{"decision":"'||NEW.decision||'"}'
                FROM learning_episodes e WHERE e.id=NEW.episode_id;
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

    @classmethod
    def _text_tuple(cls, values: Iterable[str], field: str) -> tuple[str, ...]:
        out: list[str] = []
        for value in values:
            text = cls._text(value, field)
            if text not in out:
                out.append(text)
        return tuple(out)

    @staticmethod
    def _json_tuple(raw: str, field: str) -> tuple[str, ...]:
        value = json.loads(raw)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise LineageCorruptionError(
                f"{field} must be a JSON list of non-empty strings"
            )
        return tuple(value)

    def _decision_for(self, episode_id: str) -> LearningDecision | None:
        row = self._conn.execute(
            "SELECT id,episode_id,decision,rationale,confidence "
            "FROM learning_decisions WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        if row is None:
            return None
        return LearningDecision(
            id=str(row["id"]),
            episode_id=str(row["episode_id"]),
            decision=str(row["decision"]),
            rationale=str(row["rationale"]),
            confidence=float(row["confidence"]),
        )

    def _consequence(self, row: sqlite3.Row) -> ConsequenceObservation:
        return ConsequenceObservation(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            episode_id=str(row["episode_id"]),
            observation=str(row["observation"]),
            source_kind=str(row["source_kind"]),
            source_ref=str(row["source_ref"]),
            confidence=float(row["confidence"]),
            conditions=self._json_tuple(str(row["conditions_json"]), "conditions"),
            confounders=self._json_tuple(
                str(row["confounders_json"]), "confounders"
            ),
        )

    def consequences_for(self, episode_id: str) -> tuple[ConsequenceObservation, ...]:
        self._require_episode_row(episode_id)
        return tuple(
            self._consequence(row)
            for row in self._conn.execute(
                "SELECT seq,id,episode_id,observation,source_kind,source_ref,"
                "confidence,conditions_json,confounders_json "
                "FROM learning_consequences WHERE episode_id=? ORDER BY seq",
                (episode_id,),
            )
        )

    def _episode_from_row(self, row: sqlite3.Row) -> LearningEpisode:
        episode_id = str(row["id"])
        return LearningEpisode(
            sequence=int(row["seq"]),
            id=episode_id,
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            version_id=None if row["version_id"] is None else str(row["version_id"]),
            session_id=str(row["session_id"]),
            domain=str(row["domain"]),
            subject_ref=str(row["subject_ref"]),
            change_description=str(row["change_description"]),
            consequences=self.consequences_for(episode_id),
            decision=self._decision_for(episode_id),
        )

    def _require_episode_row(self, episode_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,version_id,session_id,domain,"
            "subject_ref,change_description FROM learning_episodes WHERE id=?",
            (str(episode_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Learning episode not found in profile {self.store.profile_id}: "
                f"{episode_id}"
            )
        return row

    def get_episode(self, episode_id: str) -> LearningEpisode | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,version_id,session_id,domain,"
            "subject_ref,change_description FROM learning_episodes WHERE id=?",
            (str(episode_id),),
        ).fetchone()
        return None if row is None else self._episode_from_row(row)

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("learning_schema_version") != str(
                LEARNING_SCHEMA_VERSION
            ):
                raise LineageCorruptionError("unsupported Learning schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'learning_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"Learning integrity hooks are incomplete: {sorted(missing)}"
                )
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,version_id,session_id,domain,"
                "subject_ref,change_description FROM learning_episodes ORDER BY seq"
            ):
                if str(row["artist_id"]) != self.store.primary_artist_id:
                    raise LineageCorruptionError("Learning episode artist crosses profile")
                session = self.sessions.get_session(str(row["session_id"]))
                if session is None or session.song_id != str(row["song_id"]):
                    raise LineageCorruptionError(
                        "Learning episode Session/Song binding is invalid"
                    )
                expected_version = (
                    None if row["version_id"] is None else str(row["version_id"])
                )
                if session.version_id != expected_version:
                    raise LineageCorruptionError(
                        "Learning episode Version diverges from Session"
                    )
                self._text(str(row["domain"]), "domain")
                self._text(str(row["subject_ref"]), "subject_ref")
                self._text(str(row["change_description"]), "change_description")
            for row in self._conn.execute(
                "SELECT seq,id,episode_id,observation,source_kind,source_ref,"
                "confidence,conditions_json,confounders_json "
                "FROM learning_consequences ORDER BY seq"
            ):
                consequence = self._consequence(row)
                if consequence.source_kind not in SOURCE_KINDS:
                    raise LineageCorruptionError(
                        "Learning consequence has invalid source"
                    )
                if not 0.0 <= consequence.confidence <= 1.0:
                    raise LineageCorruptionError(
                        "Learning consequence has invalid confidence"
                    )
                self._require_episode_row(consequence.episode_id)
            for row in self._conn.execute(
                "SELECT id,episode_id,decision,rationale,confidence "
                "FROM learning_decisions"
            ):
                if str(row["decision"]) not in DECISION_KINDS:
                    raise LineageCorruptionError("Learning decision has invalid kind")
                if not 0.0 <= float(row["confidence"]) <= 1.0:
                    raise LineageCorruptionError(
                        "Learning decision has invalid confidence"
                    )
                self._text(str(row["rationale"]), "rationale")
                self._require_episode_row(str(row["episode_id"]))
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LineageCorruptionError(
                "Learning memory is unreadable or corrupt"
            ) from exc

    def create_episode(
        self,
        *,
        session_id: str,
        domain: str,
        subject_ref: str,
        change_description: str,
    ) -> LearningEpisode:
        session_id = self._text(session_id, "session_id")
        domain = self._text(domain, "domain")
        subject_ref = self._text(subject_ref, "subject_ref")
        change_description = self._text(change_description, "change_description")
        session = self.sessions.get_session(session_id)
        if session is None:
            raise NotFoundError(
                f"Session not found in profile {self.store.profile_id}: {session_id}"
            )
        episode_id = f"learn_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO learning_episodes("
                    "id,artist_id,song_id,version_id,session_id,domain,subject_ref,"
                    "change_description) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        episode_id,
                        self.store.primary_artist_id,
                        session.song_id,
                        session.version_id,
                        session.id,
                        domain,
                        subject_ref,
                        change_description,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot create Learning episode: {exc}") from exc
        episode = self.get_episode(episode_id)
        assert episode is not None
        return episode

    def append_consequence(
        self,
        episode_id: str,
        *,
        observation: str,
        source_kind: str,
        source_ref: str,
        confidence: float = 1.0,
        conditions: Iterable[str] = (),
        confounders: Iterable[str] = (),
    ) -> ConsequenceObservation:
        episode = self.get_episode(episode_id)
        if episode is None:
            raise NotFoundError(
                f"Learning episode not found in profile {self.store.profile_id}: "
                f"{episode_id}"
            )
        if episode.decision is not None:
            raise ValidationError("cannot append consequence after Learning decision")
        observation = self._text(observation, "observation")
        source_kind = str(source_kind).strip().upper()
        if source_kind not in SOURCE_KINDS:
            raise ValidationError(f"unsupported consequence source: {source_kind}")
        source_ref = self._text(source_ref, "source_ref")
        confidence = self._unit(confidence, "confidence")
        conditions_tuple = self._text_tuple(conditions, "condition")
        confounders_tuple = self._text_tuple(confounders, "confounder")
        consequence_id = f"lobs_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO learning_consequences("
                    "id,episode_id,observation,source_kind,source_ref,confidence,"
                    "conditions_json,confounders_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        consequence_id,
                        episode.id,
                        observation,
                        source_kind,
                        source_ref,
                        confidence,
                        json.dumps(conditions_tuple, separators=(",", ":")),
                        json.dumps(confounders_tuple, separators=(",", ":")),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                f"cannot append Learning consequence: {exc}"
            ) from exc
        row = self._conn.execute(
            "SELECT seq,id,episode_id,observation,source_kind,source_ref,"
            "confidence,conditions_json,confounders_json "
            "FROM learning_consequences WHERE id=?",
            (consequence_id,),
        ).fetchone()
        assert row is not None
        return self._consequence(row)

    def decide(
        self,
        episode_id: str,
        *,
        decision: str,
        rationale: str,
        confidence: float = 1.0,
    ) -> LearningDecision:
        episode = self.get_episode(episode_id)
        if episode is None:
            raise NotFoundError(
                f"Learning episode not found in profile {self.store.profile_id}: "
                f"{episode_id}"
            )
        if episode.decision is not None:
            raise ValidationError("Learning episode already has a decision")
        if not episode.consequences:
            raise ValidationError(
                "Learning decision requires at least one observed consequence"
            )
        decision = str(decision).strip().upper()
        if decision not in DECISION_KINDS:
            raise ValidationError(f"unsupported Learning decision: {decision}")
        rationale = self._text(rationale, "rationale")
        confidence = self._unit(confidence, "confidence")
        decision_id = f"ldec_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO learning_decisions("
                    "id,episode_id,decision,rationale,confidence) VALUES(?,?,?,?,?)",
                    (decision_id, episode.id, decision, rationale, confidence),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot record Learning decision: {exc}") from exc
        result = self._decision_for(episode.id)
        assert result is not None
        return result

    def episodes_for_song(self, song_id: str) -> tuple[LearningEpisode, ...]:
        if self.store.get_song(song_id) is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        return tuple(
            self._episode_from_row(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,version_id,session_id,domain,"
                "subject_ref,change_description FROM learning_episodes "
                "WHERE song_id=? ORDER BY seq",
                (song_id,),
            )
        )
