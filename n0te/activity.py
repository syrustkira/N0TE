from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

ACTIVITY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ActivityEvent:
    sequence: int
    id: str
    event_type: str
    artist_id: str
    song_id: str | None
    version_id: str | None
    object_type: str
    object_id: str
    payload: Any


class ActivityLog:
    """Append-only chronology inside the canonical profile lineage database."""

    _TRIGGER_NAMES = {
        "activity_version_matches_song",
        "activity_events_immutable_update",
        "activity_events_immutable_delete",
        "activity_song_created",
        "activity_song_selected",
        "activity_asset_attached",
        "activity_version_created",
        "activity_current_version_changed",
        "activity_version_approved",
        "activity_evidence_claim_recorded",
        "activity_evidence_supersession_linked",
    }

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("ActivityLog requires the canonical LineageStore")
        self.store = store
        self._conn = store._conn
        self._ensure_schema_and_hooks()
        self._validate_existing()

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _metadata_value(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER activity_version_matches_song
            BEFORE INSERT ON activity_events
            WHEN NEW.version_id IS NOT NULL AND (
                NEW.song_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM versions v
                    WHERE v.id=NEW.version_id AND v.song_id=NEW.song_id
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'activity version belongs to a different Song');
            END""",
            """CREATE TRIGGER activity_events_immutable_update
            BEFORE UPDATE ON activity_events
            BEGIN
                SELECT RAISE(ABORT, 'activity history is append-only');
            END""",
            """CREATE TRIGGER activity_events_immutable_delete
            BEFORE DELETE ON activity_events
            BEGIN
                SELECT RAISE(ABORT, 'activity history is append-only');
            END""",
            """CREATE TRIGGER activity_song_created
            AFTER INSERT ON songs
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                VALUES('act_'||lower(hex(randomblob(16))),'SONG_CREATED',NEW.artist_id,NEW.id,NULL,'SONG',NEW.id,'{}');
            END""",
            """CREATE TRIGGER activity_song_selected
            AFTER UPDATE OF value ON metadata
            WHEN NEW.key='active_song_id' AND NEW.value<>'' AND NEW.value IS NOT OLD.value
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                SELECT 'act_'||lower(hex(randomblob(16))),'SONG_SELECTED',s.artist_id,s.id,s.current_version_id,'SONG',s.id,'{}'
                FROM songs s WHERE s.id=NEW.value;
            END""",
            """CREATE TRIGGER activity_asset_attached
            AFTER INSERT ON assets
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                SELECT 'act_'||lower(hex(randomblob(16))),'ASSET_ATTACHED',s.artist_id,NEW.song_id,NULL,'ASSET',NEW.id,'{}'
                FROM songs s WHERE s.id=NEW.song_id;
            END""",
            """CREATE TRIGGER activity_version_created
            AFTER INSERT ON versions
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                SELECT 'act_'||lower(hex(randomblob(16))),'VERSION_CREATED',s.artist_id,NEW.song_id,NEW.id,'VERSION',NEW.id,'{}'
                FROM songs s WHERE s.id=NEW.song_id;
            END""",
            """CREATE TRIGGER activity_current_version_changed
            AFTER UPDATE OF current_version_id ON songs
            WHEN NEW.current_version_id IS NOT OLD.current_version_id AND NEW.current_version_id IS NOT NULL
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                VALUES('act_'||lower(hex(randomblob(16))),'CURRENT_VERSION_CHANGED',NEW.artist_id,NEW.id,NEW.current_version_id,'VERSION',NEW.current_version_id,'{}');
            END""",
            """CREATE TRIGGER activity_version_approved
            AFTER UPDATE OF approved_version_id ON songs
            WHEN NEW.approved_version_id IS NOT OLD.approved_version_id AND NEW.approved_version_id IS NOT NULL
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                VALUES('act_'||lower(hex(randomblob(16))),'VERSION_APPROVED',NEW.artist_id,NEW.id,NEW.approved_version_id,'VERSION',NEW.approved_version_id,'{}');
            END""",
            """CREATE TRIGGER activity_evidence_claim_recorded
            AFTER INSERT ON evidence_claims
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'EVIDENCE_CLAIM_RECORDED',
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    CASE
                        WHEN NEW.scope_kind='SONG' THEN NEW.scope_id
                        WHEN NEW.scope_kind='VERSION' THEN (SELECT song_id FROM versions WHERE id=NEW.scope_id)
                        ELSE NULL
                    END,
                    CASE WHEN NEW.scope_kind='VERSION' THEN NEW.scope_id ELSE NULL END,
                    'EVIDENCE_CLAIM',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER activity_evidence_supersession_linked
            AFTER INSERT ON evidence_supersessions
            BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'EVIDENCE_SUPERSESSION_LINKED',
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    CASE
                        WHEN c.scope_kind='SONG' THEN c.scope_id
                        WHEN c.scope_kind='VERSION' THEN (SELECT song_id FROM versions WHERE id=c.scope_id)
                        ELSE NULL
                    END,
                    CASE WHEN c.scope_kind='VERSION' THEN c.scope_id ELSE NULL END,
                    'EVIDENCE_CLAIM',NEW.new_claim_id,'{}'
                FROM evidence_claims c WHERE c.id=NEW.new_claim_id;
            END""",
        )

    def _ensure_schema_and_hooks(self) -> None:
        table_exists = self._table_exists("activity_events")
        version = self._metadata_value("activity_schema_version")
        if table_exists != (version is not None):
            raise LineageCorruptionError("activity schema metadata/table mismatch")
        if table_exists:
            if version != str(ACTIVITY_SCHEMA_VERSION):
                raise LineageCorruptionError(f"unsupported activity schema version: {version}")
            return
        if not self._table_exists("evidence_claims") or not self._table_exists("evidence_supersessions"):
            raise LineageCorruptionError(
                "ActivityLog requires EvidenceMemory to initialize canonical evidence tables first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE activity_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL CHECK(length(trim(event_type)) > 0),
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NULL REFERENCES songs(id),
                        version_id TEXT NULL REFERENCES versions(id),
                        object_type TEXT NOT NULL CHECK(length(trim(object_type)) > 0),
                        object_id TEXT NOT NULL CHECK(length(trim(object_id)) > 0),
                        payload_json TEXT NOT NULL
                    )"""
                )
                self._conn.execute("CREATE INDEX activity_song_sequence ON activity_events(song_id,seq)")
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('activity_schema_version',?)",
                    (str(ACTIVITY_SCHEMA_VERSION),),
                )
                self._conn.execute(
                    "INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json) "
                    "VALUES('act_'||lower(hex(randomblob(16))),'ACTIVITY_TRACKING_ENABLED',?,NULL,NULL,'PROFILE',?,'{}')",
                    (self.store.primary_artist_id, self.store.profile_id),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Activity chronology") from exc

    def _validate_existing(self) -> None:
        try:
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'activity_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(f"activity hooks are incomplete: {sorted(missing)}")
            invalid = self._conn.execute(
                "SELECT a.id FROM activity_events a "
                "LEFT JOIN artists ar ON ar.id=a.artist_id "
                "LEFT JOIN songs s ON s.id=a.song_id "
                "LEFT JOIN versions v ON v.id=a.version_id "
                "WHERE ar.id IS NULL "
                "OR (a.song_id IS NOT NULL AND s.id IS NULL) "
                "OR (a.version_id IS NOT NULL AND (v.id IS NULL OR a.song_id IS NULL OR v.song_id<>a.song_id)) "
                "LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise LineageCorruptionError("activity history contains invalid object references")
            count = int(self._conn.execute("SELECT COUNT(*) AS n FROM activity_events").fetchone()["n"])
            if count < 1:
                raise LineageCorruptionError("activity history unexpectedly became empty")
            for row in self._conn.execute("SELECT payload_json FROM activity_events"):
                json.loads(str(row["payload_json"]))
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError("activity history is unreadable or corrupt") from exc

    @staticmethod
    def _event(row: sqlite3.Row) -> ActivityEvent:
        return ActivityEvent(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            event_type=str(row["event_type"]),
            artist_id=str(row["artist_id"]),
            song_id=None if row["song_id"] is None else str(row["song_id"]),
            version_id=None if row["version_id"] is None else str(row["version_id"]),
            object_type=str(row["object_type"]),
            object_id=str(row["object_id"]),
            payload=json.loads(str(row["payload_json"])),
        )

    def checkpoint(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM activity_events").fetchone()
        return int(row["seq"])

    def for_song(self, song_id: str, *, after_sequence: int = 0, limit: int | None = None) -> tuple[ActivityEvent, ...]:
        if self.store.get_song(song_id) is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        if int(after_sequence) < 0:
            raise ValidationError("after_sequence must be >= 0")
        sql = (
            "SELECT seq,id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json "
            "FROM activity_events WHERE song_id=? AND seq>? ORDER BY seq"
        )
        params: list[Any] = [song_id, int(after_sequence)]
        if limit is not None:
            if int(limit) <= 0:
                raise ValidationError("limit must be > 0")
            sql += " LIMIT ?"
            params.append(int(limit))
        return tuple(self._event(row) for row in self._conn.execute(sql, params))

    def for_profile(self, *, after_sequence: int = 0, limit: int | None = None) -> tuple[ActivityEvent, ...]:
        if int(after_sequence) < 0:
            raise ValidationError("after_sequence must be >= 0")
        sql = (
            "SELECT seq,id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json "
            "FROM activity_events WHERE seq>? ORDER BY seq"
        )
        params: list[Any] = [int(after_sequence)]
        if limit is not None:
            if int(limit) <= 0:
                raise ValidationError("limit must be > 0")
            sql += " LIMIT ?"
            params.append(int(limit))
        return tuple(self._event(row) for row in self._conn.execute(sql, params))
