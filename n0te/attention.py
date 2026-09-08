from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

ATTENTION_SCHEMA_VERSION = 1
FOCUS_MODES = {"MAKE", "FINISH", "MANAGE", "RELEASE", "PERFORM"}
FOCUS_STATES = {"ACTIVE", "ENDED"}
FOCUS_END_REASONS = {"ENDED", "SWITCHED"}


@dataclass(frozen=True)
class FocusSession:
    sequence: int
    id: str
    artist_id: str
    song_id: str | None
    mode: str
    state: str
    end_reason: str | None


class AttentionMemory:
    """Durable Artist-level attention state for Headquarters.

    Headquarters Focus is intentionally separate from DAW FocusContext. This
    service records what kind of work deserves the artist's attention now; it
    never identifies or authorizes a DAW object target.
    """

    _TRIGGER_NAMES = {
        "attention_focus_song_same_artist",
        "attention_focus_binding_immutable",
        "attention_focus_ended_immutable",
        "attention_focus_delete_immutable",
        "attention_focus_end_shape",
        "attention_focus_started_activity",
        "attention_focus_ended_activity",
    }
    _INDEX_NAMES = {"attention_one_active_focus_per_artist"}

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("AttentionMemory requires the canonical LineageStore")
        self.store = store
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
        exists = self._table_exists("attention_focus_sessions")
        version = self._metadata_value("attention_schema_version")
        if exists or version is not None:
            if not exists or version != str(ATTENTION_SCHEMA_VERSION):
                raise LineageCorruptionError("Attention schema metadata/table mismatch")
            return
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "AttentionMemory requires canonical Activity chronology first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE attention_focus_sessions (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NULL REFERENCES songs(id),
                        mode TEXT NOT NULL CHECK(mode IN (
                            'MAKE','FINISH','MANAGE','RELEASE','PERFORM'
                        )),
                        state TEXT NOT NULL DEFAULT 'ACTIVE'
                            CHECK(state IN ('ACTIVE','ENDED')),
                        end_reason TEXT NULL CHECK(
                            end_reason IS NULL OR end_reason IN ('ENDED','SWITCHED')
                        )
                    )"""
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX attention_one_active_focus_per_artist "
                    "ON attention_focus_sessions(artist_id) WHERE state='ACTIVE'"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('attention_schema_version',?)",
                    (str(ATTENTION_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Attention memory") from exc

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER attention_focus_song_same_artist
            BEFORE INSERT ON attention_focus_sessions
            WHEN NEW.song_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM songs s
                WHERE s.id=NEW.song_id AND s.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'Focus Song belongs to a different Artist');
            END""",
            """CREATE TRIGGER attention_focus_binding_immutable
            BEFORE UPDATE ON attention_focus_sessions
            WHEN NEW.id<>OLD.id OR NEW.artist_id<>OLD.artist_id
              OR NEW.song_id IS NOT OLD.song_id OR NEW.mode<>OLD.mode
            BEGIN
                SELECT RAISE(ABORT, 'Focus identity and binding are immutable');
            END""",
            """CREATE TRIGGER attention_focus_ended_immutable
            BEFORE UPDATE ON attention_focus_sessions
            WHEN OLD.state='ENDED'
            BEGIN
                SELECT RAISE(ABORT, 'ended Focus Session is immutable');
            END""",
            """CREATE TRIGGER attention_focus_delete_immutable
            BEFORE DELETE ON attention_focus_sessions
            BEGIN
                SELECT RAISE(ABORT, 'Focus history is immutable');
            END""",
            """CREATE TRIGGER attention_focus_end_shape
            BEFORE UPDATE ON attention_focus_sessions
            WHEN NOT (
                OLD.state='ACTIVE'
                AND NEW.state='ENDED'
                AND NEW.end_reason IN ('ENDED','SWITCHED')
            )
            BEGIN
                SELECT RAISE(ABORT, 'Focus Session may only transition ACTIVE to ENDED');
            END""",
            """CREATE TRIGGER attention_focus_started_activity
            AFTER INSERT ON attention_focus_sessions
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'FOCUS_SESSION_STARTED',NEW.artist_id,NEW.song_id,NULL,
                    'FOCUS_SESSION',NEW.id,
                    '{\"mode\":\"'||NEW.mode||'\"}'
                );
            END""",
            """CREATE TRIGGER attention_focus_ended_activity
            AFTER UPDATE OF state ON attention_focus_sessions
            WHEN OLD.state='ACTIVE' AND NEW.state='ENDED'
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'FOCUS_SESSION_ENDED',NEW.artist_id,NEW.song_id,NULL,
                    'FOCUS_SESSION',NEW.id,
                    '{\"mode\":\"'||NEW.mode||'\",\"reason\":\"'||NEW.end_reason||'\"}'
                );
            END""",
        )

    @staticmethod
    def _normalize_mode(value: str) -> str:
        mode = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        if mode not in FOCUS_MODES:
            raise ValidationError(f"unsupported Headquarters Focus mode: {mode}")
        return mode

    @staticmethod
    def _session(row: sqlite3.Row) -> FocusSession:
        return FocusSession(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=None if row["song_id"] is None else str(row["song_id"]),
            mode=str(row["mode"]),
            state=str(row["state"]),
            end_reason=None if row["end_reason"] is None else str(row["end_reason"]),
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("attention_schema_version") != str(
                ATTENTION_SCHEMA_VERSION
            ):
                raise LineageCorruptionError("unsupported Attention schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'attention_%'"
                )
            }
            missing_triggers = self._TRIGGER_NAMES - trigger_names
            if missing_triggers:
                raise LineageCorruptionError(
                    f"Attention integrity hooks are incomplete: {sorted(missing_triggers)}"
                )
            index_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'attention_%'"
                )
            }
            missing_indexes = self._INDEX_NAMES - index_names
            if missing_indexes:
                raise LineageCorruptionError(
                    f"Attention uniqueness hooks are incomplete: {sorted(missing_indexes)}"
                )
            active_count = 0
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,mode,state,end_reason "
                "FROM attention_focus_sessions ORDER BY seq"
            ):
                session = self._session(row)
                if session.artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError(
                        "Focus Session artist does not match active profile"
                    )
                if session.mode not in FOCUS_MODES or session.state not in FOCUS_STATES:
                    raise LineageCorruptionError("Focus Session contains invalid state")
                if session.song_id is not None:
                    song = self.store.get_song(session.song_id)
                    if song is None or song.artist_id != session.artist_id:
                        raise LineageCorruptionError(
                            "Focus Session is bound to invalid Song/Artist"
                        )
                if session.state == "ACTIVE":
                    active_count += 1
                    if session.end_reason is not None:
                        raise LineageCorruptionError(
                            "active Focus Session contains an end reason"
                        )
                elif session.end_reason not in FOCUS_END_REASONS:
                    raise LineageCorruptionError(
                        "ended Focus Session is missing a valid end reason"
                    )
            if active_count > 1:
                raise LineageCorruptionError(
                    "profile contains more than one active Focus Session"
                )
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            raise LineageCorruptionError(
                "Attention memory is unreadable or corrupt"
            ) from exc

    def active_focus(self) -> FocusSession | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,mode,state,end_reason "
            "FROM attention_focus_sessions WHERE state='ACTIVE' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._session(row)

    def history(self) -> tuple[FocusSession, ...]:
        return tuple(
            self._session(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,mode,state,end_reason "
                "FROM attention_focus_sessions ORDER BY seq"
            )
        )

    def start_focus(self, mode: str, *, song_id: str | None = None) -> FocusSession:
        mode = self._normalize_mode(mode)
        if song_id is not None:
            song_id = str(song_id).strip()
            song = self.store.get_song(song_id)
            if song is None:
                raise NotFoundError(
                    f"Song not found in profile {self.store.profile_id}: {song_id}"
                )
            if song.artist_id != self.store.primary_artist_id:
                raise ValidationError("Focus Song belongs to a different Artist")
        current = self.active_focus()
        if current is not None and current.mode == mode and current.song_id == song_id:
            return current

        focus_id = f"focus_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                if current is not None:
                    changed = self._conn.execute(
                        "UPDATE attention_focus_sessions "
                        "SET state='ENDED',end_reason='SWITCHED' "
                        "WHERE id=? AND state='ACTIVE'",
                        (current.id,),
                    ).rowcount
                    if changed != 1:
                        raise LineageCorruptionError(
                            "active Focus Session changed during switch"
                        )
                self._conn.execute(
                    "INSERT INTO attention_focus_sessions("
                    "id,artist_id,song_id,mode,state,end_reason) "
                    "VALUES(?,?,?,?,'ACTIVE',NULL)",
                    (focus_id, self.store.primary_artist_id, song_id, mode),
                )
        except LineageCorruptionError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot start Focus Session: {exc}") from exc
        active = self.active_focus()
        if active is None or active.id != focus_id:
            raise LineageCorruptionError("new Focus Session did not become active")
        return active

    def end_focus(self) -> FocusSession | None:
        current = self.active_focus()
        if current is None:
            return None
        try:
            with self.store._tx():
                changed = self._conn.execute(
                    "UPDATE attention_focus_sessions "
                    "SET state='ENDED',end_reason='ENDED' "
                    "WHERE id=? AND state='ACTIVE'",
                    (current.id,),
                ).rowcount
                if changed != 1:
                    raise LineageCorruptionError(
                        "active Focus Session changed during end"
                    )
        except LineageCorruptionError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot end Focus Session: {exc}") from exc
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,mode,state,end_reason "
            "FROM attention_focus_sessions WHERE id=?",
            (current.id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("ended Focus Session disappeared")
        return self._session(row)
