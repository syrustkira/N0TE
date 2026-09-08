from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from .evidence import EvidenceClaim, EvidenceMemory, SOURCE_KINDS, TWIN_DOMAINS
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

SESSION_SCHEMA_VERSION = 1
SESSION_STATES = {"OPEN", "CLOSED"}
SESSION_ITEM_KINDS = {
    "OBSERVATION",
    "DECISION",
    "REJECTED_IDEA",
    "UNRESOLVED",
    "MARK",
}
PROMOTION_SCOPES = {"SONG", "VERSION"}


@dataclass(frozen=True)
class SessionItem:
    sequence: int
    id: str
    session_id: str
    kind: str
    body: str


@dataclass(frozen=True)
class SongSession:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    version_id: str | None
    objective: str
    state: str
    debrief_summary: str | None
    next_action: str | None


@dataclass(frozen=True)
class SessionPromotion:
    item_id: str
    claim_id: str


class SessionMemory:
    """Song-bound work intent, scratch exploration, debrief and explicit learning.

    Session scratch is durable history but is not durable Artist/product doctrine.
    Only explicit promotion writes a canonical EvidenceClaim. Promotion requests
    are immutable and may safely remain pending after interruption; the claim and
    Session link are committed atomically by a database trigger inside the normal
    EvidenceMemory transaction.
    """

    _TRIGGER_NAMES = {
        "session_version_same_song",
        "session_binding_immutable",
        "session_closed_immutable",
        "session_delete_immutable",
        "session_close_requires_debrief",
        "session_open_has_no_debrief",
        "session_items_open_only",
        "session_items_immutable_update",
        "session_items_immutable_delete",
        "session_promotion_requests_immutable_update",
        "session_promotion_requests_immutable_delete",
        "session_promotion_scope_matches_session",
        "session_promotions_immutable_update",
        "session_promotions_immutable_delete",
        "session_promotion_claim_link",
        "session_started_activity",
        "session_item_added_activity",
        "session_closed_activity",
        "session_promoted_activity",
    }

    def __init__(self, store: LineageStore, evidence: EvidenceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("SessionMemory requires the canonical LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError("SessionMemory requires EvidenceMemory for the same LineageStore")
        self.store = store
        self.evidence = evidence
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
        table_names = (
            "sessions",
            "session_items",
            "session_promotion_requests",
            "session_promotions",
        )
        tables = {name: self._table_exists(name) for name in table_names}
        version = self._metadata_value("session_schema_version")
        if any(tables.values()) or version is not None:
            if not all(tables.values()) or version != str(SESSION_SCHEMA_VERSION):
                raise LineageCorruptionError("Session schema metadata/table mismatch")
            return
        if not self._table_exists("activity_events") or not self._table_exists("evidence_claims"):
            raise LineageCorruptionError(
                "SessionMemory requires canonical Evidence and Activity first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE sessions (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        version_id TEXT NULL REFERENCES versions(id),
                        objective TEXT NOT NULL CHECK(length(trim(objective)) > 0),
                        state TEXT NOT NULL DEFAULT 'OPEN' CHECK(state IN ('OPEN','CLOSED')),
                        debrief_summary TEXT NULL,
                        next_action TEXT NULL
                    )"""
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX session_one_open_per_song "
                    "ON sessions(song_id) WHERE state='OPEN'"
                )
                self._conn.execute(
                    """CREATE TABLE session_items (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        kind TEXT NOT NULL CHECK(kind IN (
                            'OBSERVATION','DECISION','REJECTED_IDEA','UNRESOLVED','MARK'
                        )),
                        body TEXT NOT NULL CHECK(length(trim(body)) > 0)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX session_items_by_session ON session_items(session_id,seq)"
                )
                self._conn.execute(
                    """CREATE TABLE session_promotion_requests (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        item_id TEXT NOT NULL UNIQUE REFERENCES session_items(id),
                        source_ref TEXT NOT NULL UNIQUE,
                        scope_kind TEXT NOT NULL CHECK(scope_kind IN ('SONG','VERSION')),
                        scope_id TEXT NOT NULL,
                        key TEXT NOT NULL CHECK(length(trim(key)) > 0),
                        value_json TEXT NOT NULL,
                        source_kind TEXT NOT NULL CHECK(source_kind IN (
                            'USER_DECLARED','OBSERVED','MEASURED','PROVIDER_VERIFIED','REMEMBERED','INFERRED'
                        )),
                        twin_domain TEXT NOT NULL CHECK(twin_domain IN (
                            'TECHNICAL','CREATIVE','UNSPECIFIED'
                        )),
                        confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE session_promotions (
                        item_id TEXT PRIMARY KEY REFERENCES session_items(id),
                        claim_id TEXT NOT NULL UNIQUE REFERENCES evidence_claims(id)
                    )"""
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX session_promotion_claim_source "
                    "ON evidence_claims(source_ref) "
                    "WHERE source_ref LIKE 'session-promotion:%'"
                )

                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('session_schema_version',?)",
                    (str(SESSION_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Session memory") from exc

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER session_version_same_song
            BEFORE INSERT ON sessions
            WHEN NEW.version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM versions v
                WHERE v.id=NEW.version_id AND v.song_id=NEW.song_id
            ) BEGIN
                SELECT RAISE(ABORT, 'Session version belongs to a different Song');
            END""",
            """CREATE TRIGGER session_binding_immutable
            BEFORE UPDATE ON sessions
            WHEN NEW.id<>OLD.id OR NEW.artist_id<>OLD.artist_id
              OR NEW.song_id<>OLD.song_id OR NEW.version_id IS NOT OLD.version_id
              OR NEW.objective<>OLD.objective
            BEGIN
                SELECT RAISE(ABORT, 'Session identity and intent are immutable');
            END""",
            """CREATE TRIGGER session_closed_immutable
            BEFORE UPDATE ON sessions
            WHEN OLD.state='CLOSED'
            BEGIN
                SELECT RAISE(ABORT, 'closed Session is immutable');
            END""",
            """CREATE TRIGGER session_delete_immutable
            BEFORE DELETE ON sessions
            BEGIN
                SELECT RAISE(ABORT, 'Session history is immutable');
            END""",
            """CREATE TRIGGER session_close_requires_debrief
            BEFORE UPDATE ON sessions
            WHEN NEW.state='CLOSED' AND (
                NEW.debrief_summary IS NULL OR length(trim(NEW.debrief_summary))=0
                OR NEW.next_action IS NULL OR length(trim(NEW.next_action))=0
            ) BEGIN
                SELECT RAISE(ABORT, 'closing Session requires debrief and next action');
            END""",
            """CREATE TRIGGER session_open_has_no_debrief
            BEFORE UPDATE ON sessions
            WHEN NEW.state='OPEN' AND (
                NEW.debrief_summary IS NOT NULL OR NEW.next_action IS NOT NULL
            ) BEGIN
                SELECT RAISE(ABORT, 'open Session cannot contain final debrief');
            END""",
            """CREATE TRIGGER session_items_open_only
            BEFORE INSERT ON session_items
            WHEN NOT EXISTS (
                SELECT 1 FROM sessions s
                WHERE s.id=NEW.session_id AND s.state='OPEN'
            ) BEGIN
                SELECT RAISE(ABORT, 'scratch can be appended only to an open Session');
            END""",
            """CREATE TRIGGER session_items_immutable_update
            BEFORE UPDATE ON session_items BEGIN
                SELECT RAISE(ABORT, 'Session scratch is append-only');
            END""",
            """CREATE TRIGGER session_items_immutable_delete
            BEFORE DELETE ON session_items BEGIN
                SELECT RAISE(ABORT, 'Session scratch is append-only');
            END""",
            """CREATE TRIGGER session_promotion_requests_immutable_update
            BEFORE UPDATE ON session_promotion_requests BEGIN
                SELECT RAISE(ABORT, 'Session promotion request is immutable');
            END""",
            """CREATE TRIGGER session_promotion_requests_immutable_delete
            BEFORE DELETE ON session_promotion_requests BEGIN
                SELECT RAISE(ABORT, 'Session promotion request is immutable');
            END""",
            """CREATE TRIGGER session_promotion_scope_matches_session
            BEFORE INSERT ON session_promotion_requests
            WHEN NOT EXISTS (
                SELECT 1
                FROM session_items i JOIN sessions s ON s.id=i.session_id
                WHERE i.id=NEW.item_id AND (
                    (NEW.scope_kind='SONG' AND NEW.scope_id=s.song_id)
                    OR (
                        NEW.scope_kind='VERSION'
                        AND s.version_id IS NOT NULL
                        AND NEW.scope_id=s.version_id
                    )
                )
            ) BEGIN
                SELECT RAISE(ABORT, 'Session promotion scope crosses Session binding');
            END""",
            """CREATE TRIGGER session_promotions_immutable_update
            BEFORE UPDATE ON session_promotions BEGIN
                SELECT RAISE(ABORT, 'Session promotion link is immutable');
            END""",
            """CREATE TRIGGER session_promotions_immutable_delete
            BEFORE DELETE ON session_promotions BEGIN
                SELECT RAISE(ABORT, 'Session promotion link is immutable');
            END""",
            """CREATE TRIGGER session_promotion_claim_link
            AFTER INSERT ON evidence_claims
            WHEN NEW.source_ref IS NOT NULL
            BEGIN
                INSERT INTO session_promotions(item_id,claim_id)
                SELECT r.item_id,NEW.id
                FROM session_promotion_requests r
                WHERE r.source_ref=NEW.source_ref
                  AND r.scope_kind=NEW.scope_kind
                  AND r.scope_id=NEW.scope_id
                  AND r.key=NEW.key
                  AND r.value_json=NEW.value_json
                  AND r.source_kind=NEW.source_kind
                  AND r.twin_domain=NEW.twin_domain
                  AND r.confidence=NEW.confidence;
            END""",
            """CREATE TRIGGER session_started_activity
            AFTER INSERT ON sessions
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'SESSION_STARTED',NEW.artist_id,NEW.song_id,NEW.version_id,
                    'SESSION',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER session_item_added_activity
            AFTER INSERT ON session_items
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'SESSION_SCRATCH_ADDED',s.artist_id,s.song_id,s.version_id,
                    'SESSION_ITEM',NEW.id,
                    '{"kind":"'||NEW.kind||'"}'
                FROM sessions s WHERE s.id=NEW.session_id;
            END""",
            """CREATE TRIGGER session_closed_activity
            AFTER UPDATE OF state ON sessions
            WHEN OLD.state='OPEN' AND NEW.state='CLOSED'
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'SESSION_CLOSED',NEW.artist_id,NEW.song_id,NEW.version_id,
                    'SESSION',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER session_promoted_activity
            AFTER INSERT ON session_promotions
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'SESSION_ITEM_PROMOTED',s.artist_id,s.song_id,s.version_id,
                    'EVIDENCE_CLAIM',NEW.claim_id,'{}'
                FROM session_items i
                JOIN sessions s ON s.id=i.session_id
                WHERE i.id=NEW.item_id;
            END""",
        )

    @staticmethod
    def _clean_text(value: str, field: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValidationError(f"{field} must not be empty")
        return value

    @staticmethod
    def _session(row: sqlite3.Row) -> SongSession:
        return SongSession(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            version_id=None if row["version_id"] is None else str(row["version_id"]),
            objective=str(row["objective"]),
            state=str(row["state"]),
            debrief_summary=None if row["debrief_summary"] is None else str(row["debrief_summary"]),
            next_action=None if row["next_action"] is None else str(row["next_action"]),
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> SessionItem:
        return SessionItem(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            kind=str(row["kind"]),
            body=str(row["body"]),
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("session_schema_version") != str(SESSION_SCHEMA_VERSION):
                raise LineageCorruptionError("unsupported Session schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'session_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(f"Session integrity hooks are incomplete: {sorted(missing)}")
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,version_id,objective,state,"
                "debrief_summary,next_action FROM sessions ORDER BY seq"
            ):
                session = self._session(row)
                song = self.store.get_song(session.song_id)
                if song is None or song.artist_id != session.artist_id:
                    raise LineageCorruptionError("Session is bound to invalid Song/Artist")
                if session.artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError("Session artist does not match active profile")
                if session.version_id is not None:
                    version = self.store.get_version(session.version_id)
                    if version is None or version.song_id != session.song_id:
                        raise LineageCorruptionError("Session version crosses Songs")
                if session.state not in SESSION_STATES:
                    raise LineageCorruptionError("Session contains invalid state")
                if session.state == "OPEN" and (
                    session.debrief_summary is not None or session.next_action is not None
                ):
                    raise LineageCorruptionError("open Session contains final debrief")
                if session.state == "CLOSED" and (
                    not session.debrief_summary or not session.next_action
                ):
                    raise LineageCorruptionError("closed Session is missing debrief")
            for row in self._conn.execute(
                "SELECT seq,id,session_id,kind,body FROM session_items ORDER BY seq"
            ):
                item = self._item(row)
                if item.kind not in SESSION_ITEM_KINDS:
                    raise LineageCorruptionError("Session scratch contains invalid kind")
                if self.get_session(item.session_id) is None:
                    raise LineageCorruptionError("Session scratch lost its Session")
            for row in self._conn.execute(
                "SELECT r.item_id,r.source_ref,r.scope_kind,r.scope_id,r.key,r.value_json,"
                "r.source_kind,r.twin_domain,r.confidence,i.body,s.song_id,s.version_id "
                "FROM session_promotion_requests r "
                "JOIN session_items i ON i.id=r.item_id "
                "JOIN sessions s ON s.id=i.session_id ORDER BY r.seq"
            ):
                if not str(row["source_ref"]).startswith("session-promotion:"):
                    raise LineageCorruptionError("Session promotion source reference is invalid")
                if str(row["scope_kind"]) not in PROMOTION_SCOPES:
                    raise LineageCorruptionError("Session promotion contains invalid scope")
                if str(row["source_kind"]) not in SOURCE_KINDS:
                    raise LineageCorruptionError("Session promotion contains invalid source")
                if str(row["twin_domain"]) not in TWIN_DOMAINS:
                    raise LineageCorruptionError("Session promotion contains invalid Twin domain")
                if not 0.0 <= float(row["confidence"]) <= 1.0:
                    raise LineageCorruptionError("Session promotion contains invalid confidence")
                if json.loads(str(row["value_json"])) != str(row["body"]):
                    raise LineageCorruptionError("Session promotion value diverges from scratch")
                if str(row["scope_kind"]) == "SONG":
                    if str(row["scope_id"]) != str(row["song_id"]):
                        raise LineageCorruptionError("Session promotion crosses Songs")
                elif row["version_id"] is None or str(row["scope_id"]) != str(row["version_id"]):
                    raise LineageCorruptionError("Session promotion crosses Session version")
            invalid_link = self._conn.execute(
                "SELECT p.item_id,p.claim_id FROM session_promotions p "
                "LEFT JOIN session_promotion_requests r ON r.item_id=p.item_id "
                "LEFT JOIN evidence_claims c ON c.id=p.claim_id "
                "WHERE r.item_id IS NULL OR c.id IS NULL "
                "OR c.source_ref<>r.source_ref OR c.scope_kind<>r.scope_kind "
                "OR c.scope_id<>r.scope_id OR c.key<>r.key OR c.value_json<>r.value_json "
                "OR c.source_kind<>r.source_kind OR c.twin_domain<>r.twin_domain "
                "OR c.confidence<>r.confidence LIMIT 1"
            ).fetchone()
            if invalid_link is not None:
                raise LineageCorruptionError("Session promotion link is inconsistent")
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise LineageCorruptionError("Session memory is unreadable or corrupt") from exc

    def get_session(self, session_id: str) -> SongSession | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,version_id,objective,state,"
            "debrief_summary,next_action FROM sessions WHERE id=?",
            (str(session_id),),
        ).fetchone()
        return None if row is None else self._session(row)

    def _require_session(self, session_id: str) -> SongSession:
        session = self.get_session(session_id)
        if session is None:
            raise NotFoundError(f"Session not found in profile {self.store.profile_id}: {session_id}")
        return session

    def start_session(
        self,
        *,
        song_id: str,
        objective: str,
        version_id: str | None = None,
    ) -> SongSession:
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        objective = self._clean_text(objective, "objective")
        if version_id is None:
            version_id = song.current_version_id
        elif self.store.get_version(version_id) is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version_id is not None:
            version = self.store.get_version(version_id)
            assert version is not None
            if version.song_id != song.id:
                raise ValidationError("Session version belongs to a different Song")
        session_id = f"sess_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO sessions(id,artist_id,song_id,version_id,objective,state) "
                    "VALUES(?,?,?,?,?,'OPEN')",
                    (
                        session_id,
                        self.store.primary_artist_id,
                        song.id,
                        version_id,
                        objective,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot start Session: {exc}") from exc
        return self._require_session(session_id)

    def append_scratch(self, session_id: str, *, kind: str, body: str) -> SessionItem:
        session = self._require_session(session_id)
        if session.state != "OPEN":
            raise ValidationError("cannot append scratch to a closed Session")
        kind = str(kind).strip().upper()
        if kind not in SESSION_ITEM_KINDS:
            raise ValidationError(f"unsupported Session scratch kind: {kind}")
        body = self._clean_text(body, "body")
        item_id = f"sitem_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO session_items(id,session_id,kind,body) VALUES(?,?,?,?)",
                    (item_id,session.id,kind,body),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot append Session scratch: {exc}") from exc
        row = self._conn.execute(
            "SELECT seq,id,session_id,kind,body FROM session_items WHERE id=?",
            (item_id,),
        ).fetchone()
        assert row is not None
        return self._item(row)

    def items_for_session(self, session_id: str) -> tuple[SessionItem, ...]:
        self._require_session(session_id)
        return tuple(
            self._item(row)
            for row in self._conn.execute(
                "SELECT seq,id,session_id,kind,body FROM session_items "
                "WHERE session_id=? ORDER BY seq",
                (session_id,),
            )
        )

    def latest_for_song(self, song_id: str) -> SongSession | None:
        if self.store.get_song(song_id) is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,version_id,objective,state,"
            "debrief_summary,next_action FROM sessions WHERE song_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (song_id,),
        ).fetchone()
        return None if row is None else self._session(row)

    def close_session(
        self,
        session_id: str,
        *,
        debrief_summary: str,
        next_action: str,
    ) -> SongSession:
        session = self._require_session(session_id)
        if session.state != "OPEN":
            raise ValidationError("Session is already closed")
        debrief_summary = self._clean_text(debrief_summary, "debrief_summary")
        next_action = self._clean_text(next_action, "next_action")
        try:
            with self.store._tx():
                self._conn.execute(
                    "UPDATE sessions SET state='CLOSED',debrief_summary=?,next_action=? "
                    "WHERE id=? AND state='OPEN'",
                    (debrief_summary,next_action,session.id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot close Session: {exc}") from exc
        return self._require_session(session.id)

    def _item_with_session(self, item_id: str) -> tuple[SessionItem, SongSession]:
        row = self._conn.execute(
            "SELECT i.seq AS item_seq,i.id AS item_id,i.session_id,i.kind,i.body,"
            "s.seq AS session_seq,s.artist_id,s.song_id,s.version_id,s.objective,"
            "s.state,s.debrief_summary,s.next_action "
            "FROM session_items i JOIN sessions s ON s.id=i.session_id WHERE i.id=?",
            (str(item_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Session item not found in profile {self.store.profile_id}: {item_id}")
        return (
            SessionItem(
                sequence=int(row["item_seq"]),
                id=str(row["item_id"]),
                session_id=str(row["session_id"]),
                kind=str(row["kind"]),
                body=str(row["body"]),
            ),
            SongSession(
                sequence=int(row["session_seq"]),
                id=str(row["session_id"]),
                artist_id=str(row["artist_id"]),
                song_id=str(row["song_id"]),
                version_id=None if row["version_id"] is None else str(row["version_id"]),
                objective=str(row["objective"]),
                state=str(row["state"]),
                debrief_summary=None if row["debrief_summary"] is None else str(row["debrief_summary"]),
                next_action=None if row["next_action"] is None else str(row["next_action"]),
            ),
        )

    def promotion_for_item(self, item_id: str) -> SessionPromotion | None:
        row = self._conn.execute(
            "SELECT item_id,claim_id FROM session_promotions WHERE item_id=?",
            (str(item_id),),
        ).fetchone()
        return None if row is None else SessionPromotion(str(row["item_id"]),str(row["claim_id"]))

    def promote_item(
        self,
        item_id: str,
        *,
        scope_kind: str,
        key: str,
        source_kind: str = "USER_DECLARED",
        twin_domain: str = "UNSPECIFIED",
        confidence: float = 1.0,
    ) -> EvidenceClaim:
        item,session = self._item_with_session(item_id)
        scope_kind = str(scope_kind).strip().upper()
        key = self._clean_text(key,"key")
        source_kind = str(source_kind).strip().upper()
        twin_domain = str(twin_domain).strip().upper()
        if scope_kind not in PROMOTION_SCOPES:
            raise ValidationError(f"unsupported Session promotion scope: {scope_kind}")
        if source_kind not in SOURCE_KINDS:
            raise ValidationError(f"unsupported evidence source: {source_kind}")
        if twin_domain not in TWIN_DOMAINS:
            raise ValidationError(f"unsupported Twin domain: {twin_domain}")
        try:
            confidence = float(confidence)
        except (TypeError,ValueError) as exc:
            raise ValidationError("confidence must be between 0 and 1") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValidationError("confidence must be between 0 and 1")
        if scope_kind == "SONG":
            scope_id = session.song_id
        else:
            if session.version_id is None:
                raise ValidationError("Session has no bound Version to promote into")
            scope_id = session.version_id
        value_json = json.dumps(item.body,sort_keys=True,separators=(",",":"),allow_nan=False)

        request = self._conn.execute(
            "SELECT id,item_id,source_ref,scope_kind,scope_id,key,value_json,"
            "source_kind,twin_domain,confidence FROM session_promotion_requests "
            "WHERE item_id=?",
            (item.id,),
        ).fetchone()
        if request is None:
            request_id = f"spr_{uuid.uuid4().hex}"
            source_ref = f"session-promotion:{request_id}"
            try:
                with self.store._tx():
                    self._conn.execute(
                        "INSERT INTO session_promotion_requests("
                        "id,item_id,source_ref,scope_kind,scope_id,key,value_json,"
                        "source_kind,twin_domain,confidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            request_id,item.id,source_ref,scope_kind,scope_id,key,
                            value_json,source_kind,twin_domain,confidence,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"cannot request Session promotion: {exc}") from exc
            request = self._conn.execute(
                "SELECT id,item_id,source_ref,scope_kind,scope_id,key,value_json,"
                "source_kind,twin_domain,confidence FROM session_promotion_requests "
                "WHERE item_id=?",
                (item.id,),
            ).fetchone()
            assert request is not None
        else:
            expected = (
                scope_kind,scope_id,key,value_json,source_kind,twin_domain,confidence,
            )
            actual = (
                str(request["scope_kind"]),str(request["scope_id"]),str(request["key"]),
                str(request["value_json"]),str(request["source_kind"]),
                str(request["twin_domain"]),float(request["confidence"]),
            )
            if actual != expected:
                raise ValidationError(
                    "Session item already has a different immutable promotion request"
                )

        linked = self.promotion_for_item(item.id)
        if linked is not None:
            claim = self.evidence.get_claim(linked.claim_id)
            if claim is None:
                raise LineageCorruptionError("Session promotion lost its EvidenceClaim")
            return claim

        try:
            claim = self.evidence.record_claim(
                scope_kind=scope_kind,
                scope_id=scope_id,
                key=key,
                value=item.body,
                source_kind=source_kind,
                source_ref=str(request["source_ref"]),
                confidence=confidence,
                twin_domain=twin_domain,
            )
        except ValidationError:
            linked = self.promotion_for_item(item.id)
            if linked is None:
                raise
            claim = self.evidence.get_claim(linked.claim_id)
            if claim is None:
                raise LineageCorruptionError("Session promotion lost its EvidenceClaim")
            return claim

        linked = self.promotion_for_item(item.id)
        if linked is None or linked.claim_id != claim.id:
            raise LineageCorruptionError(
                "Evidence promotion committed without matching Session link"
            )
        return claim
