from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .evidence import SOURCE_KINDS
from .learning import LearningMemory
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

FRICTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FrictionObservation:
    sequence: int
    id: str
    episode_id: str
    friction_key: str
    description: str
    source_kind: str
    source_ref: str
    confidence: float
    prevention_hint: str | None
    song_id: str
    session_id: str


@dataclass(frozen=True)
class FrictionPattern:
    friction_key: str
    occurrences: tuple[FrictionObservation, ...]
    occurrence_count: int
    session_count: int
    session_ids: tuple[str, ...]
    prevention_hints: tuple[str, ...]


class FrictionMemory:
    """Explicit repeated-friction evidence across real Sessions.

    Friction is never inferred from prose, mood, decision type, personality, health,
    or consequence semantics. A recurring pattern exists only when the same explicit
    friction_key is recorded across the configured number of distinct Session IDs.
    """

    _TRIGGER_NAMES = {
        "friction_observations_immutable_update",
        "friction_observations_immutable_delete",
        "friction_observation_activity",
    }

    def __init__(self, store: LineageStore, learning: LearningMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("FrictionMemory requires the canonical LineageStore")
        if not isinstance(learning, LearningMemory) or learning.store is not store:
            raise TypeError(
                "FrictionMemory requires LearningMemory for the same LineageStore"
            )
        self.store = store
        self.learning = learning
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
        exists = self._table_exists("friction_observations")
        version = self._metadata_value("friction_schema_version")
        if exists or version is not None:
            if not exists or version != str(FRICTION_SCHEMA_VERSION):
                raise LineageCorruptionError("Friction schema metadata/table mismatch")
            return
        if not self._table_exists("learning_episodes") or not self._table_exists(
            "activity_events"
        ):
            raise LineageCorruptionError(
                "FrictionMemory requires canonical Learning and Activity first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE friction_observations (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        episode_id TEXT NOT NULL REFERENCES learning_episodes(id),
                        friction_key TEXT NOT NULL CHECK(length(trim(friction_key)) > 0),
                        description TEXT NOT NULL CHECK(length(trim(description)) > 0),
                        source_kind TEXT NOT NULL CHECK(source_kind IN (
                            'USER_DECLARED','OBSERVED','MEASURED','PROVIDER_VERIFIED',
                            'REMEMBERED','INFERRED'
                        )),
                        source_ref TEXT NOT NULL CHECK(length(trim(source_ref)) > 0),
                        confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
                        prevention_hint TEXT NULL,
                        UNIQUE(episode_id, friction_key)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX friction_key_history "
                    "ON friction_observations(friction_key,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('friction_schema_version',?)",
                    (str(FRICTION_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Friction memory") from exc

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER friction_observations_immutable_update
            BEFORE UPDATE ON friction_observations BEGIN
                SELECT RAISE(ABORT, 'Friction observations are append-only');
            END""",
            """CREATE TRIGGER friction_observations_immutable_delete
            BEFORE DELETE ON friction_observations BEGIN
                SELECT RAISE(ABORT, 'Friction observations are append-only');
            END""",
            """CREATE TRIGGER friction_observation_activity
            AFTER INSERT ON friction_observations
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'FRICTION_OBSERVED',
                    e.artist_id,e.song_id,e.version_id,
                    'FRICTION_OBSERVATION',NEW.id,'{}'
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

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def _observation(self, row: sqlite3.Row) -> FrictionObservation:
        episode = self.learning.get_episode(str(row["episode_id"]))
        if episode is None:
            raise LineageCorruptionError("Friction observation lost Learning episode")
        return FrictionObservation(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            episode_id=str(row["episode_id"]),
            friction_key=str(row["friction_key"]),
            description=str(row["description"]),
            source_kind=str(row["source_kind"]),
            source_ref=str(row["source_ref"]),
            confidence=float(row["confidence"]),
            prevention_hint=(
                None
                if row["prevention_hint"] is None
                else str(row["prevention_hint"])
            ),
            song_id=episode.song_id,
            session_id=episode.session_id,
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("friction_schema_version") != str(
                FRICTION_SCHEMA_VERSION
            ):
                raise LineageCorruptionError("unsupported Friction schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'friction_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"Friction integrity hooks are incomplete: {sorted(missing)}"
                )
            for row in self._conn.execute(
                "SELECT seq,id,episode_id,friction_key,description,source_kind,"
                "source_ref,confidence,prevention_hint "
                "FROM friction_observations ORDER BY seq"
            ):
                observation = self._observation(row)
                self._text(observation.friction_key, "friction_key")
                self._text(observation.description, "description")
                self._text(observation.source_ref, "source_ref")
                if observation.source_kind not in SOURCE_KINDS:
                    raise LineageCorruptionError(
                        "Friction observation has invalid source"
                    )
                if not 0.0 <= observation.confidence <= 1.0:
                    raise LineageCorruptionError(
                        "Friction observation has invalid confidence"
                    )
                if observation.prevention_hint is not None:
                    self._text(observation.prevention_hint, "prevention_hint")
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            raise LineageCorruptionError(
                "Friction memory is unreadable or corrupt"
            ) from exc

    def record(
        self,
        *,
        episode_id: str,
        friction_key: str,
        description: str,
        source_kind: str,
        source_ref: str,
        confidence: float = 1.0,
        prevention_hint: str | None = None,
    ) -> FrictionObservation:
        episode = self.learning.get_episode(episode_id)
        if episode is None:
            raise NotFoundError(
                f"Learning episode not found in profile {self.store.profile_id}: "
                f"{episode_id}"
            )
        friction_key = self._text(friction_key, "friction_key")
        description = self._text(description, "description")
        source_kind = str(source_kind).strip().upper()
        if source_kind not in SOURCE_KINDS:
            raise ValidationError(f"unsupported Friction source: {source_kind}")
        source_ref = self._text(source_ref, "source_ref")
        confidence = self._unit(confidence, "confidence")
        prevention_hint = self._optional_text(prevention_hint)
        observation_id = f"fric_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO friction_observations("
                    "id,episode_id,friction_key,description,source_kind,source_ref,"
                    "confidence,prevention_hint) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        episode.id,
                        friction_key,
                        description,
                        source_kind,
                        source_ref,
                        confidence,
                        prevention_hint,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                f"cannot record Friction observation: {exc}"
            ) from exc
        row = self._conn.execute(
            "SELECT seq,id,episode_id,friction_key,description,source_kind,"
            "source_ref,confidence,prevention_hint "
            "FROM friction_observations WHERE id=?",
            (observation_id,),
        ).fetchone()
        assert row is not None
        return self._observation(row)

    def observations(
        self,
        *,
        friction_key: str | None = None,
        song_id: str | None = None,
    ) -> tuple[FrictionObservation, ...]:
        if song_id is not None and self.store.get_song(song_id) is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        if friction_key is not None:
            friction_key = self._text(friction_key, "friction_key")
        sql = (
            "SELECT f.seq,f.id,f.episode_id,f.friction_key,f.description,"
            "f.source_kind,f.source_ref,f.confidence,f.prevention_hint "
            "FROM friction_observations f "
            "JOIN learning_episodes e ON e.id=f.episode_id WHERE 1=1"
        )
        params: list[str] = []
        if friction_key is not None:
            sql += " AND f.friction_key=?"
            params.append(friction_key)
        if song_id is not None:
            sql += " AND e.song_id=?"
            params.append(song_id)
        sql += " ORDER BY f.seq"
        return tuple(self._observation(row) for row in self._conn.execute(sql, params))

    def recurring_patterns(
        self,
        *,
        min_sessions: int = 2,
        song_id: str | None = None,
    ) -> tuple[FrictionPattern, ...]:
        try:
            min_sessions = int(min_sessions)
        except (TypeError, ValueError) as exc:
            raise ValidationError("min_sessions must be >= 2") from exc
        if min_sessions < 2:
            raise ValidationError("min_sessions must be >= 2")

        grouped: dict[str, list[FrictionObservation]] = {}
        for observation in self.observations(song_id=song_id):
            grouped.setdefault(observation.friction_key, []).append(observation)

        patterns: list[FrictionPattern] = []
        for friction_key, occurrences in grouped.items():
            session_ids = tuple(dict.fromkeys(item.session_id for item in occurrences))
            if len(session_ids) < min_sessions:
                continue
            hints = tuple(
                dict.fromkeys(
                    item.prevention_hint
                    for item in occurrences
                    if item.prevention_hint is not None
                )
            )
            patterns.append(
                FrictionPattern(
                    friction_key=friction_key,
                    occurrences=tuple(occurrences),
                    occurrence_count=len(occurrences),
                    session_count=len(session_ids),
                    session_ids=session_ids,
                    prevention_hints=hints,
                )
            )
        patterns.sort(key=lambda item: item.friction_key)
        return tuple(patterns)
