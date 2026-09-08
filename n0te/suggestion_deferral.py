from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .lineage import LineageCorruptionError, LineageStore, ValidationError
from .session import SessionMemory

SUGGESTION_DEFERRAL_SCHEMA_VERSION = 2
LATER_THIS_SONG = "LATER_THIS_SONG"
NEXT_SONG = "NEXT_SONG"
NEVER_SUGGEST_AGAIN = "NEVER_SUGGEST_AGAIN"
SUGGESTION_DEFERRAL_SCOPES = (
    LATER_THIS_SONG,
    NEXT_SONG,
    NEVER_SUGGEST_AGAIN,
)


@dataclass(frozen=True)
class SuggestionDeferral:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    session_id: str | None
    semantic_key: str
    scope: str


class SuggestionDeferralMemory:
    """Explicit artist-owned suggestion deferrals in the canonical profile DB.

    Scope is intentionally concrete rather than calendar-like:

    * LATER_THIS_SONG hides a semantic suggestion key only for the exact work
      Session in which the artist deferred it. A distinct later Session for the
      same Song makes it eligible again.
    * NEXT_SONG hides the key until the Artist later selects a different Song.
      Crossing that Song-selection horizon ends the active suppression even if
      the Artist later returns to the source Song. The immutable decision stays
      in history and the Artist may explicitly choose NEXT_SONG again.
    * NEVER_SUGGEST_AGAIN is an explicit Artist-wide suppression of that stable
      semantic key. It is not inferred from skips, taste, or engagement.

    Deferrals are immutable history and never grant DAW/provider/action authority.
    """

    _TRIGGERS = {
        "suggestion_deferral_binding_valid",
        "suggestion_deferral_immutable",
        "suggestion_deferral_delete_immutable",
        "suggestion_deferral_activity",
    }
    _INDEXES = {
        "suggestion_deferral_unique_later",
        "suggestion_deferral_next_lookup",
        "suggestion_deferral_unique_never",
    }

    def __init__(self, store: LineageStore, sessions: SessionMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("SuggestionDeferralMemory requires LineageStore")
        if not isinstance(sessions, SessionMemory) or sessions.store is not store:
            raise TypeError(
                "SuggestionDeferralMemory requires SessionMemory for the same LineageStore"
            )
        self.store = store
        self.sessions = sessions
        self._conn = store._conn
        self._ensure_schema()
        self._validate_existing()

    def _table_exists(self, name: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    def _metadata_value(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _trigger_names(self) -> set[str]:
        return {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'suggestion_deferral_%'"
            )
        }

    def _index_names(self) -> set[str]:
        return {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'suggestion_deferral_%'"
            )
        }

    def _validate_v1_before_migration(self) -> None:
        missing = self._TRIGGERS - self._trigger_names()
        if missing:
            raise LineageCorruptionError(
                f"Suggestion deferral v1 integrity hooks are incomplete: {sorted(missing)}"
            )
        for row in self._conn.execute(
            "SELECT seq,id,artist_id,song_id,session_id,semantic_key,scope "
            "FROM suggestion_deferrals ORDER BY seq"
        ):
            if str(row["artist_id"]) != self.store.primary_artist_id:
                raise LineageCorruptionError(
                    "Suggestion deferral v1 artist does not match active profile"
                )
            if str(row["scope"]) != LATER_THIS_SONG:
                raise LineageCorruptionError(
                    "Suggestion deferral v1 contains an unknown scope"
                )
            song_id = str(row["song_id"])
            song = self.store.get_song(song_id)
            if song is None or song.artist_id != self.store.primary_artist_id:
                raise LineageCorruptionError(
                    "Suggestion deferral v1 references invalid Song"
                )
            session = self.sessions.get_session(str(row["session_id"]))
            if session is None or session.song_id != song_id:
                raise LineageCorruptionError(
                    "Suggestion deferral v1 references invalid Session"
                )

    @staticmethod
    def _table_statement() -> str:
        return """CREATE TABLE suggestion_deferrals (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            artist_id TEXT NOT NULL REFERENCES artists(id),
            song_id TEXT NOT NULL REFERENCES songs(id),
            session_id TEXT NULL REFERENCES sessions(id),
            semantic_key TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN (
                'LATER_THIS_SONG','NEXT_SONG','NEVER_SUGGEST_AGAIN'
            ))
        )"""

    @staticmethod
    def _index_statements() -> tuple[str, ...]:
        return (
            "CREATE UNIQUE INDEX suggestion_deferral_unique_later "
            "ON suggestion_deferrals(artist_id,song_id,session_id,semantic_key) "
            "WHERE scope='LATER_THIS_SONG'",
            "CREATE INDEX suggestion_deferral_next_lookup "
            "ON suggestion_deferrals(artist_id,song_id,semantic_key,seq) "
            "WHERE scope='NEXT_SONG'",
            "CREATE UNIQUE INDEX suggestion_deferral_unique_never "
            "ON suggestion_deferrals(artist_id,semantic_key) "
            "WHERE scope='NEVER_SUGGEST_AGAIN'",
        )

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER suggestion_deferral_binding_valid
            BEFORE INSERT ON suggestion_deferrals
            WHEN NOT EXISTS (
                SELECT 1 FROM songs s
                WHERE s.id=NEW.song_id AND s.artist_id=NEW.artist_id
            ) OR (
                NEW.session_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM sessions x
                    WHERE x.id=NEW.session_id AND x.song_id=NEW.song_id
                )
            ) OR (
                NEW.scope='LATER_THIS_SONG' AND NEW.session_id IS NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'Suggestion deferral binding is invalid');
            END""",
            """CREATE TRIGGER suggestion_deferral_immutable
            BEFORE UPDATE ON suggestion_deferrals
            BEGIN
                SELECT RAISE(ABORT, 'Suggestion deferral history is immutable');
            END""",
            """CREATE TRIGGER suggestion_deferral_delete_immutable
            BEFORE DELETE ON suggestion_deferrals
            BEGIN
                SELECT RAISE(ABORT, 'Suggestion deferral history is immutable');
            END""",
            """CREATE TRIGGER suggestion_deferral_activity
            AFTER INSERT ON suggestion_deferrals
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'SUGGESTION_DEFERRED',NEW.artist_id,NEW.song_id,NULL,
                    'SUGGESTION_DEFERRAL',NEW.id,
                    '{\"scope\":\"'||NEW.scope||'\"}'
                );
            END""",
        )

    def _create_v2_schema(self, *, create_triggers: bool = True) -> None:
        self._conn.execute(self._table_statement())
        for statement in self._index_statements():
            self._conn.execute(statement)
        if create_triggers:
            for statement in self._trigger_statements():
                self._conn.execute(statement)

    def _migrate_v1_to_v2(self) -> None:
        self._validate_v1_before_migration()
        try:
            with self.store._tx():
                for trigger in self._TRIGGERS:
                    self._conn.execute(f"DROP TRIGGER {trigger}")
                self._conn.execute(
                    "ALTER TABLE suggestion_deferrals RENAME TO suggestion_deferrals_v1"
                )
                # Historical v1 rows already emitted Activity when the artist
                # created them. Copy before installing v2 triggers so migration
                # preserves history without duplicating those events.
                self._create_v2_schema(create_triggers=False)
                self._conn.execute(
                    "INSERT INTO suggestion_deferrals("
                    "seq,id,artist_id,song_id,session_id,semantic_key,scope) "
                    "SELECT seq,id,artist_id,song_id,session_id,semantic_key,scope "
                    "FROM suggestion_deferrals_v1 ORDER BY seq"
                )
                self._conn.execute("DROP TABLE suggestion_deferrals_v1")
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                changed = self._conn.execute(
                    "UPDATE metadata SET value=? "
                    "WHERE key='suggestion_deferral_schema_version'",
                    (str(SUGGESTION_DEFERRAL_SCHEMA_VERSION),),
                ).rowcount
                if changed != 1:
                    raise LineageCorruptionError(
                        "Suggestion deferral schema metadata disappeared during migration"
                    )
        except LineageCorruptionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot migrate suggestion deferral memory to schema v2"
            ) from exc

    def _ensure_schema(self) -> None:
        exists = self._table_exists("suggestion_deferrals")
        version = self._metadata_value("suggestion_deferral_schema_version")
        if exists or version is not None:
            if exists and version == "1":
                self._migrate_v1_to_v2()
                return
            if not exists or version != str(SUGGESTION_DEFERRAL_SCHEMA_VERSION):
                raise LineageCorruptionError(
                    "Suggestion deferral schema metadata/table mismatch"
                )
            return
        if not self._table_exists("sessions") or not self._table_exists(
            "activity_events"
        ):
            raise LineageCorruptionError(
                "Suggestion deferral requires canonical Session and Activity memory first"
            )
        try:
            with self.store._tx():
                self._create_v2_schema()
                self._conn.execute(
                    "INSERT INTO metadata(key,value) "
                    "VALUES('suggestion_deferral_schema_version',?)",
                    (str(SUGGESTION_DEFERRAL_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize suggestion deferral memory"
            ) from exc

    @staticmethod
    def _normalize_key(value: str) -> str:
        key = str(value).strip()
        if not key or len(key) > 200:
            raise ValidationError(
                "suggestion semantic key must be 1 through 200 characters"
            )
        return key

    @staticmethod
    def _normalize_scope(value: str) -> str:
        scope = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        if scope not in SUGGESTION_DEFERRAL_SCOPES:
            raise ValidationError(f"unsupported suggestion deferral scope: {scope}")
        return scope

    @staticmethod
    def _record(row: sqlite3.Row) -> SuggestionDeferral:
        return SuggestionDeferral(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            session_id=(
                None if row["session_id"] is None else str(row["session_id"])
            ),
            semantic_key=str(row["semantic_key"]),
            scope=str(row["scope"]),
        )

    def _validate_existing(self) -> None:
        if self._metadata_value("suggestion_deferral_schema_version") != str(
            SUGGESTION_DEFERRAL_SCHEMA_VERSION
        ):
            raise LineageCorruptionError(
                "unsupported suggestion deferral schema version"
            )
        missing_triggers = self._TRIGGERS - self._trigger_names()
        if missing_triggers:
            raise LineageCorruptionError(
                "Suggestion deferral integrity hooks are incomplete: "
                f"{sorted(missing_triggers)}"
            )
        missing_indexes = self._INDEXES - self._index_names()
        if missing_indexes:
            raise LineageCorruptionError(
                "Suggestion deferral index hooks are incomplete: "
                f"{sorted(missing_indexes)}"
            )
        for row in self._conn.execute(
            "SELECT seq,id,artist_id,song_id,session_id,semantic_key,scope "
            "FROM suggestion_deferrals ORDER BY seq"
        ):
            record = self._record(row)
            if record.artist_id != self.store.primary_artist_id:
                raise LineageCorruptionError(
                    "Suggestion deferral artist does not match active profile"
                )
            if record.scope not in SUGGESTION_DEFERRAL_SCOPES:
                raise LineageCorruptionError(
                    "Suggestion deferral contains invalid scope"
                )
            song = self.store.get_song(record.song_id)
            if song is None or song.artist_id != record.artist_id:
                raise LineageCorruptionError(
                    "Suggestion deferral references invalid Song"
                )
            if record.scope == LATER_THIS_SONG and record.session_id is None:
                raise LineageCorruptionError(
                    "Session-scoped suggestion deferral lost its Session"
                )
            if record.session_id is not None:
                session = self.sessions.get_session(record.session_id)
                if session is None or session.song_id != record.song_id:
                    raise LineageCorruptionError(
                        "Suggestion deferral references invalid Session"
                    )

    def _next_song_horizon_crossed(self, record: SuggestionDeferral) -> bool:
        if record.scope != NEXT_SONG:
            raise ValueError("next-Song horizon requires a NEXT_SONG deferral")
        origin = self._conn.execute(
            "SELECT seq FROM activity_events "
            "WHERE artist_id=? AND event_type='SUGGESTION_DEFERRED' "
            "AND object_type='SUGGESTION_DEFERRAL' AND object_id=? "
            "ORDER BY seq LIMIT 1",
            (record.artist_id, record.id),
        ).fetchone()
        if origin is None:
            raise LineageCorruptionError(
                "Next-Song suggestion deferral lost its Activity provenance"
            )
        crossed = self._conn.execute(
            "SELECT 1 FROM activity_events "
            "WHERE artist_id=? AND event_type='SONG_SELECTED' AND seq>? "
            "AND song_id IS NOT NULL AND song_id<>? LIMIT 1",
            (record.artist_id, int(origin["seq"]), record.song_id),
        ).fetchone()
        return crossed is not None

    def _existing(
        self,
        *,
        semantic_key: str,
        scope: str,
        song_id: str,
        session_id: str | None,
    ) -> SuggestionDeferral | None:
        select = (
            "SELECT seq,id,artist_id,song_id,session_id,semantic_key,scope "
            "FROM suggestion_deferrals WHERE artist_id=? "
        )
        if scope == LATER_THIS_SONG:
            row = self._conn.execute(
                select
                + "AND song_id=? AND session_id=? AND semantic_key=? AND scope=? LIMIT 1",
                (
                    self.store.primary_artist_id,
                    song_id,
                    session_id,
                    semantic_key,
                    scope,
                ),
            ).fetchone()
            return None if row is None else self._record(row)
        if scope == NEXT_SONG:
            rows = self._conn.execute(
                select
                + "AND song_id=? AND semantic_key=? AND scope=? ORDER BY seq DESC",
                (
                    self.store.primary_artist_id,
                    song_id,
                    semantic_key,
                    scope,
                ),
            )
            for row in rows:
                record = self._record(row)
                if not self._next_song_horizon_crossed(record):
                    return record
            return None
        row = self._conn.execute(
            select + "AND semantic_key=? AND scope=? LIMIT 1",
            (
                self.store.primary_artist_id,
                semantic_key,
                scope,
            ),
        ).fetchone()
        return None if row is None else self._record(row)

    def _defer(self, semantic_key: str, scope: str) -> SuggestionDeferral:
        key = self._normalize_key(semantic_key)
        scope = self._normalize_scope(scope)
        song = self.store.active_song()
        if song is None:
            raise ValidationError(
                "Start or select a Song before deferring a suggestion"
            )
        session = self.sessions.latest_for_song(song.id)
        if scope == LATER_THIS_SONG and session is None:
            raise ValidationError(
                "Start a work Session before deferring a suggestion until later this Song"
            )
        session_id = None if session is None else session.id
        deferral_id = f"defer_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                # BEGIN IMMEDIATE serializes the active-horizon check and insert.
                # That matters for NEXT_SONG because completed historical rows
                # are intentionally allowed while duplicate active horizons are not.
                existing = self._existing(
                    semantic_key=key,
                    scope=scope,
                    song_id=song.id,
                    session_id=session_id,
                )
                if existing is not None:
                    return existing
                self._conn.execute(
                    "INSERT INTO suggestion_deferrals("
                    "id,artist_id,song_id,session_id,semantic_key,scope) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        deferral_id,
                        self.store.primary_artist_id,
                        song.id,
                        session_id,
                        key,
                        scope,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = self._existing(
                semantic_key=key,
                scope=scope,
                song_id=song.id,
                session_id=session_id,
            )
            if existing is not None:
                return existing
            raise ValidationError("cannot persist suggestion deferral safely")
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,session_id,semantic_key,scope "
            "FROM suggestion_deferrals WHERE id=?",
            (deferral_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("new suggestion deferral disappeared")
        return self._record(row)

    def defer_later_this_song(self, semantic_key: str) -> SuggestionDeferral:
        return self._defer(semantic_key, LATER_THIS_SONG)

    def defer_until_next_song(self, semantic_key: str) -> SuggestionDeferral:
        return self._defer(semantic_key, NEXT_SONG)

    def never_suggest_again(self, semantic_key: str) -> SuggestionDeferral:
        return self._defer(semantic_key, NEVER_SUGGEST_AGAIN)

    def is_deferred_now(self, semantic_key: str) -> bool:
        key = self._normalize_key(semantic_key)
        song = self.store.active_song()
        if song is None:
            return False
        session = self.sessions.latest_for_song(song.id)
        session_id = None if session is None else session.id
        for row in self._conn.execute(
            "SELECT seq,id,artist_id,song_id,session_id,semantic_key,scope "
            "FROM suggestion_deferrals WHERE artist_id=? AND semantic_key=? "
            "ORDER BY seq DESC",
            (self.store.primary_artist_id, key),
        ):
            record = self._record(row)
            if record.scope == NEVER_SUGGEST_AGAIN:
                return True
            if record.scope == NEXT_SONG and not self._next_song_horizon_crossed(record):
                return True
            if (
                record.scope == LATER_THIS_SONG
                and record.song_id == song.id
                and record.session_id == session_id
                and session_id is not None
            ):
                return True
        return False

    def history(self) -> tuple[SuggestionDeferral, ...]:
        return tuple(
            self._record(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,session_id,semantic_key,scope "
                "FROM suggestion_deferrals ORDER BY seq"
            )
        )
