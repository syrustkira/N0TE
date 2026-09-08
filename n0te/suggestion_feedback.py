from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field

from .creative_suggestions import (
    CREATIVE_DIMENSIONS,
    SUGGESTION_DISTANCES,
    CreativeSuggestion,
    CreativeSuggestionService,
)
from .lineage import LineageCorruptionError, LineageStore, ValidationError
from .session import SessionMemory

SUGGESTION_FEEDBACK_SCHEMA_VERSION = 1
SUGGESTION_FEEDBACK_DIRECTIONS = ("MORE", "LESS")


@dataclass(frozen=True)
class SuggestionFeedbackEvent:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    session_id: str
    semantic_key: str
    direction: str
    distance: str
    dimension: str
    preference_promoted: bool = field(default=False, init=False)
    learning_promoted: bool = field(default=False, init=False)
    automatic_weighting_applied: bool = field(default=False, init=False)
    song_mutation_authorized: bool = field(default=False, init=False)
    external_action_authorized: bool = field(default=False, init=False)


class SuggestionFeedbackMemory:
    """Append-only explicit More/Less responses to exact shown suggestions.

    Feedback is contextual soft evidence. It does not become Artist World doctrine,
    Learning causality, automatic suggestion weighting, or action authority.
    """

    _TRIGGERS = {
        "suggestion_feedback_binding_valid",
        "suggestion_feedback_immutable",
        "suggestion_feedback_delete_immutable",
        "suggestion_feedback_activity",
    }

    def __init__(self, store: LineageStore, sessions: SessionMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("SuggestionFeedbackMemory requires LineageStore")
        if not isinstance(sessions, SessionMemory) or sessions.store is not store:
            raise TypeError(
                "SuggestionFeedbackMemory requires SessionMemory for the same LineageStore"
            )
        self.store = store
        self.sessions = sessions
        self._conn = store._conn
        self._ensure_schema()
        self._validate_existing()

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _metadata_value(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _ensure_schema(self) -> None:
        exists = self._table_exists("suggestion_feedback")
        version = self._metadata_value("suggestion_feedback_schema_version")
        if exists or version is not None:
            if not exists or version != str(SUGGESTION_FEEDBACK_SCHEMA_VERSION):
                raise LineageCorruptionError(
                    "Suggestion feedback schema metadata/table mismatch"
                )
            return
        if not self._table_exists("sessions") or not self._table_exists(
            "activity_events"
        ):
            raise LineageCorruptionError(
                "Suggestion feedback requires canonical Session and Activity memory first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE suggestion_feedback (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        semantic_key TEXT NOT NULL
                            CHECK(length(trim(semantic_key)) BETWEEN 1 AND 200),
                        direction TEXT NOT NULL CHECK(direction IN ('MORE','LESS')),
                        distance TEXT NOT NULL CHECK(distance IN (
                            'FAMILIAR','ADJACENT','WILDCARD'
                        )),
                        dimension TEXT NOT NULL CHECK(dimension IN (
                            'ARRANGEMENT','RHYTHM','HARMONY','MELODY','SOUND','DYNAMICS'
                        ))
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX suggestion_feedback_key "
                    "ON suggestion_feedback(artist_id,semantic_key,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) "
                    "VALUES('suggestion_feedback_schema_version',?)",
                    (str(SUGGESTION_FEEDBACK_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize suggestion feedback memory"
            ) from exc

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER suggestion_feedback_binding_valid
            BEFORE INSERT ON suggestion_feedback
            WHEN NOT EXISTS (
                SELECT 1 FROM songs s
                JOIN sessions x ON x.song_id=s.id
                WHERE s.id=NEW.song_id
                  AND s.artist_id=NEW.artist_id
                  AND x.id=NEW.session_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'Suggestion feedback binding is invalid');
            END""",
            """CREATE TRIGGER suggestion_feedback_immutable
            BEFORE UPDATE ON suggestion_feedback
            BEGIN
                SELECT RAISE(ABORT, 'Suggestion feedback history is immutable');
            END""",
            """CREATE TRIGGER suggestion_feedback_delete_immutable
            BEFORE DELETE ON suggestion_feedback
            BEGIN
                SELECT RAISE(ABORT, 'Suggestion feedback history is immutable');
            END""",
            """CREATE TRIGGER suggestion_feedback_activity
            AFTER INSERT ON suggestion_feedback
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'SUGGESTION_FEEDBACK_RECORDED',
                    NEW.artist_id,NEW.song_id,NULL,
                    'SUGGESTION_FEEDBACK',NEW.id,
                    '{\"direction\":\"'||NEW.direction||'\"}'
                );
            END""",
        )

    @staticmethod
    def _text(value: object, field_name: str, *, maximum: int = 200) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be text")
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise ValidationError(
                f"{field_name} must be 1 through {maximum} characters"
            )
        return normalized

    @classmethod
    def _direction(cls, value: object) -> str:
        direction = cls._text(value, "suggestion feedback direction", maximum=16).upper()
        if direction not in SUGGESTION_FEEDBACK_DIRECTIONS:
            raise ValidationError(f"unsupported suggestion feedback direction: {direction}")
        return direction

    @classmethod
    def _distance(cls, value: object) -> str:
        distance = cls._text(value, "suggestion distance", maximum=32).upper()
        if distance not in SUGGESTION_DISTANCES:
            raise ValidationError(f"unsupported suggestion distance: {distance}")
        return distance

    @classmethod
    def _dimension(cls, value: object) -> str:
        dimension = cls._text(value, "suggestion dimension", maximum=32).upper()
        if dimension not in CREATIVE_DIMENSIONS:
            raise ValidationError(f"unsupported creative dimension: {dimension}")
        return dimension

    @classmethod
    def _record(cls, row: sqlite3.Row) -> SuggestionFeedbackEvent:
        return SuggestionFeedbackEvent(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            session_id=str(row["session_id"]),
            semantic_key=str(row["semantic_key"]),
            direction=str(row["direction"]),
            distance=str(row["distance"]),
            dimension=str(row["dimension"]),
        )

    def _validate_existing(self) -> None:
        if self._metadata_value("suggestion_feedback_schema_version") != str(
            SUGGESTION_FEEDBACK_SCHEMA_VERSION
        ):
            raise LineageCorruptionError("unsupported suggestion feedback schema version")
        triggers = {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'suggestion_feedback_%'"
            )
        }
        missing = self._TRIGGERS - triggers
        if missing:
            raise LineageCorruptionError(
                f"Suggestion feedback integrity hooks are incomplete: {sorted(missing)}"
            )
        for row in self._conn.execute(
            "SELECT seq,id,artist_id,song_id,session_id,semantic_key,"
            "direction,distance,dimension FROM suggestion_feedback ORDER BY seq"
        ):
            event = self._record(row)
            if event.artist_id != self.store.primary_artist_id:
                raise LineageCorruptionError(
                    "Suggestion feedback crosses Artist profile"
                )
            song = self.store.get_song(event.song_id)
            session = self.sessions.get_session(event.session_id)
            if (
                song is None
                or song.artist_id != event.artist_id
                or session is None
                or session.song_id != event.song_id
            ):
                raise LineageCorruptionError(
                    "Suggestion feedback contains invalid Song/Session binding"
                )
            self._text(event.semantic_key, "suggestion semantic key")
            self._direction(event.direction)
            self._distance(event.distance)
            self._dimension(event.dimension)

    def record(
        self, suggestion: CreativeSuggestion, *, direction: str
    ) -> SuggestionFeedbackEvent:
        if not isinstance(suggestion, CreativeSuggestion):
            raise TypeError("suggestion feedback requires the exact CreativeSuggestion shown")
        feedback_direction = self._direction(direction)
        semantic_key = self._text(
            suggestion.semantic_key, "suggestion semantic key"
        )
        distance = self._distance(suggestion.distance)
        dimension = self._dimension(suggestion.dimension)

        song = self.store.active_song()
        if song is None or song.id != suggestion.song_id:
            raise ValidationError(
                "The active Song changed before suggestion feedback was recorded"
            )
        if suggestion.session_id is None:
            raise ValidationError(
                "Start a work Session before remembering More/Less suggestion feedback"
            )
        session = self.sessions.latest_for_song(song.id)
        if session is None or session.id != suggestion.session_id:
            raise ValidationError(
                "The Song work Session changed before suggestion feedback was recorded"
            )

        canonical = CreativeSuggestionService(self.store, self.sessions).suggest(
            distance=distance,
            locked_dimensions=tuple(
                candidate
                for candidate in CREATIVE_DIMENSIONS
                if candidate != dimension
            ),
            variation=0,
        )
        if suggestion != canonical:
            raise ValidationError(
                "Suggestion feedback requires the canonical non-authorizing local suggestion result"
            )

        feedback_id = f"sfeedback_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO suggestion_feedback("
                    "id,artist_id,song_id,session_id,semantic_key,direction,distance,dimension"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        feedback_id,
                        self.store.primary_artist_id,
                        song.id,
                        session.id,
                        semantic_key,
                        feedback_direction,
                        distance,
                        dimension,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot record suggestion feedback: {exc}") from exc
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,session_id,semantic_key,"
            "direction,distance,dimension FROM suggestion_feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("new suggestion feedback disappeared")
        return self._record(row)

    def history(self) -> tuple[SuggestionFeedbackEvent, ...]:
        return tuple(
            self._record(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,session_id,semantic_key,"
                "direction,distance,dimension FROM suggestion_feedback ORDER BY seq"
            )
        )

    def for_semantic_key(self, semantic_key: str) -> tuple[SuggestionFeedbackEvent, ...]:
        key = self._text(semantic_key, "suggestion semantic key")
        return tuple(
            self._record(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,session_id,semantic_key,"
                "direction,distance,dimension FROM suggestion_feedback "
                "WHERE artist_id=? AND semantic_key=? ORDER BY seq",
                (self.store.primary_artist_id, key),
            )
        )
