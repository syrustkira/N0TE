from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

PEOPLE_SCHEMA_VERSION = 1
FOLLOWUP_RESPONSIBILITIES = {"ARTIST_OWES", "WAITING_ON_OTHER", "MUTUAL"}
FOLLOWUP_STATES = {"OPEN", "RESOLVED", "CANCELED"}


@dataclass(frozen=True)
class Person:
    sequence: int
    id: str
    artist_id: str
    display_name: str
    relationship_context: str | None


@dataclass(frozen=True)
class FollowUp:
    sequence: int
    id: str
    artist_id: str
    person_id: str
    song_id: str | None
    responsibility: str
    summary: str
    due_on: str | None
    state: str
    resolution_note: str | None


class PeopleMemory:
    """Profile-local people, promises, waiting-on and follow-up continuity.

    People records and follow-ups live inside the canonical LineageStore. This
    service deliberately does not connect providers, infer identity equality,
    merge people, send messages, mutate calendars or grant external authority.
    Provider identity reconciliation and external execution remain separate
    downstream responsibilities.
    """

    _TRIGGER_NAMES = {
        "people_person_identity_immutable",
        "people_person_delete_immutable",
        "people_followup_person_same_artist",
        "people_followup_song_same_artist",
        "people_followup_binding_immutable",
        "people_followup_closed_immutable",
        "people_followup_delete_immutable",
        "people_followup_transition_shape",
        "people_person_created_activity",
        "people_followup_created_activity",
        "people_followup_closed_activity",
    }

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("PeopleMemory requires the canonical LineageStore")
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

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER people_person_identity_immutable
            BEFORE UPDATE ON people_people
            BEGIN
                SELECT RAISE(ABORT, 'person identity is immutable in this contract');
            END""",
            """CREATE TRIGGER people_person_delete_immutable
            BEFORE DELETE ON people_people
            BEGIN
                SELECT RAISE(ABORT, 'person history is immutable');
            END""",
            """CREATE TRIGGER people_followup_person_same_artist
            BEFORE INSERT ON people_followups
            WHEN NOT EXISTS (
                SELECT 1 FROM people_people p
                WHERE p.id=NEW.person_id AND p.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'follow-up person belongs to a different Artist');
            END""",
            """CREATE TRIGGER people_followup_song_same_artist
            BEFORE INSERT ON people_followups
            WHEN NEW.song_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM songs s
                WHERE s.id=NEW.song_id AND s.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'follow-up Song belongs to a different Artist');
            END""",
            """CREATE TRIGGER people_followup_binding_immutable
            BEFORE UPDATE ON people_followups
            WHEN NEW.id<>OLD.id OR NEW.artist_id<>OLD.artist_id
              OR NEW.person_id<>OLD.person_id
              OR NEW.song_id IS NOT OLD.song_id
              OR NEW.responsibility<>OLD.responsibility
              OR NEW.summary<>OLD.summary
              OR NEW.due_on IS NOT OLD.due_on
            BEGIN
                SELECT RAISE(ABORT, 'follow-up identity and binding are immutable');
            END""",
            """CREATE TRIGGER people_followup_closed_immutable
            BEFORE UPDATE ON people_followups
            WHEN OLD.state<>'OPEN'
            BEGIN
                SELECT RAISE(ABORT, 'closed follow-up is immutable');
            END""",
            """CREATE TRIGGER people_followup_delete_immutable
            BEFORE DELETE ON people_followups
            BEGIN
                SELECT RAISE(ABORT, 'follow-up history is immutable');
            END""",
            """CREATE TRIGGER people_followup_transition_shape
            BEFORE UPDATE ON people_followups
            WHEN NOT (
                OLD.state='OPEN'
                AND NEW.state IN ('RESOLVED','CANCELED')
                AND NEW.resolution_note IS NOT NULL
                AND length(trim(NEW.resolution_note))>0
            )
            BEGIN
                SELECT RAISE(ABORT, 'follow-up may only close once with a resolution note');
            END""",
            """CREATE TRIGGER people_person_created_activity
            AFTER INSERT ON people_people
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'PERSON_CREATED',NEW.artist_id,NULL,NULL,
                    'PERSON',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER people_followup_created_activity
            AFTER INSERT ON people_followups
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'FOLLOWUP_CREATED',NEW.artist_id,NEW.song_id,NULL,
                    'FOLLOWUP',NEW.id,
                    '{\"responsibility\":\"'||NEW.responsibility||'\"}'
                );
            END""",
            """CREATE TRIGGER people_followup_closed_activity
            AFTER UPDATE OF state ON people_followups
            WHEN OLD.state='OPEN' AND NEW.state IN ('RESOLVED','CANCELED')
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'FOLLOWUP_'||NEW.state,NEW.artist_id,NEW.song_id,NULL,
                    'FOLLOWUP',NEW.id,'{}'
                );
            END""",
        )

    def _ensure_schema(self) -> None:
        people_exists = self._table_exists("people_people")
        followups_exist = self._table_exists("people_followups")
        version = self._metadata_value("people_schema_version")
        any_existing = people_exists or followups_exist or version is not None
        if any_existing:
            if not people_exists or not followups_exist or version != str(PEOPLE_SCHEMA_VERSION):
                raise LineageCorruptionError("People schema metadata/table mismatch")
            return
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "PeopleMemory requires canonical Activity chronology first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE people_people (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        display_name TEXT NOT NULL CHECK(length(trim(display_name))>0),
                        relationship_context TEXT NULL CHECK(
                            relationship_context IS NULL
                            OR length(trim(relationship_context))>0
                        )
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE people_followups (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id),
                        song_id TEXT NULL REFERENCES songs(id),
                        responsibility TEXT NOT NULL CHECK(responsibility IN (
                            'ARTIST_OWES','WAITING_ON_OTHER','MUTUAL'
                        )),
                        summary TEXT NOT NULL CHECK(length(trim(summary))>0),
                        due_on TEXT NULL,
                        state TEXT NOT NULL DEFAULT 'OPEN'
                            CHECK(state IN ('OPEN','RESOLVED','CANCELED')),
                        resolution_note TEXT NULL CHECK(
                            resolution_note IS NULL
                            OR length(trim(resolution_note))>0
                        )
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX people_followups_open_by_person "
                    "ON people_followups(person_id,state,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX people_followups_open_by_song "
                    "ON people_followups(song_id,state,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('people_schema_version',?)",
                    (str(PEOPLE_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize People memory") from exc

    @staticmethod
    def _clean_text(value: str, field: str, *, maximum: int) -> str:
        text = " ".join(str(value).split())
        if not text:
            raise ValidationError(f"{field} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return text

    @classmethod
    def _optional_text(cls, value: str | None, field: str, *, maximum: int) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        if not text:
            return None
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return text

    @staticmethod
    def _normalize_responsibility(value: str) -> str:
        responsibility = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        if responsibility not in FOLLOWUP_RESPONSIBILITIES:
            raise ValidationError(
                f"unsupported follow-up responsibility: {responsibility}"
            )
        return responsibility

    @staticmethod
    def _normalize_due_on(value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        text = str(value).strip()
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("due_on must be an ISO calendar date (YYYY-MM-DD)") from exc
        return parsed.isoformat()

    @staticmethod
    def _person(row: sqlite3.Row) -> Person:
        return Person(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            display_name=str(row["display_name"]),
            relationship_context=(
                None
                if row["relationship_context"] is None
                else str(row["relationship_context"])
            ),
        )

    @staticmethod
    def _followup(row: sqlite3.Row) -> FollowUp:
        return FollowUp(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            person_id=str(row["person_id"]),
            song_id=None if row["song_id"] is None else str(row["song_id"]),
            responsibility=str(row["responsibility"]),
            summary=str(row["summary"]),
            due_on=None if row["due_on"] is None else str(row["due_on"]),
            state=str(row["state"]),
            resolution_note=(
                None if row["resolution_note"] is None else str(row["resolution_note"])
            ),
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("people_schema_version") != str(
                PEOPLE_SCHEMA_VERSION
            ):
                raise LineageCorruptionError("unsupported People schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'people_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"People integrity hooks are incomplete: {sorted(missing)}"
                )
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,display_name,relationship_context "
                "FROM people_people ORDER BY seq"
            ):
                person = self._person(row)
                if person.artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError(
                        "person Artist does not match active profile"
                    )
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,person_id,song_id,responsibility,summary,"
                "due_on,state,resolution_note FROM people_followups ORDER BY seq"
            ):
                followup = self._followup(row)
                if followup.artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError(
                        "follow-up Artist does not match active profile"
                    )
                person = self.get_person(followup.person_id)
                if person is None or person.artist_id != followup.artist_id:
                    raise LineageCorruptionError("follow-up is bound to an invalid person")
                if followup.song_id is not None:
                    song = self.store.get_song(followup.song_id)
                    if song is None or song.artist_id != followup.artist_id:
                        raise LineageCorruptionError("follow-up is bound to an invalid Song")
                if followup.responsibility not in FOLLOWUP_RESPONSIBILITIES:
                    raise LineageCorruptionError("follow-up responsibility is invalid")
                if followup.state not in FOLLOWUP_STATES:
                    raise LineageCorruptionError("follow-up state is invalid")
                if followup.due_on is not None:
                    self._normalize_due_on(followup.due_on)
                if followup.state == "OPEN" and followup.resolution_note is not None:
                    raise LineageCorruptionError(
                        "open follow-up unexpectedly has a resolution note"
                    )
                if followup.state != "OPEN" and followup.resolution_note is None:
                    raise LineageCorruptionError(
                        "closed follow-up is missing a resolution note"
                    )
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError, ValidationError) as exc:
            raise LineageCorruptionError("People memory is unreadable or corrupt") from exc

    def create_person(
        self,
        display_name: str,
        *,
        relationship_context: str | None = None,
    ) -> Person:
        name = self._clean_text(display_name, "display_name", maximum=160)
        context = self._optional_text(
            relationship_context,
            "relationship_context",
            maximum=800,
        )
        person_id = f"person_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO people_people("
                    "id,artist_id,display_name,relationship_context) VALUES(?,?,?,?)",
                    (person_id, self.store.primary_artist_id, name, context),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot create person: {exc}") from exc
        person = self.get_person(person_id)
        if person is None:
            raise LineageCorruptionError("new person disappeared after creation")
        return person

    def get_person(self, person_id: str) -> Person | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,display_name,relationship_context "
            "FROM people_people WHERE id=?",
            (str(person_id).strip(),),
        ).fetchone()
        return None if row is None else self._person(row)

    def people(self) -> tuple[Person, ...]:
        return tuple(
            self._person(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,display_name,relationship_context "
                "FROM people_people ORDER BY seq"
            )
        )

    def create_followup(
        self,
        person_id: str,
        summary: str,
        *,
        responsibility: str,
        song_id: str | None = None,
        due_on: str | None = None,
    ) -> FollowUp:
        person = self.get_person(person_id)
        if person is None:
            raise NotFoundError(
                f"person not found in profile {self.store.profile_id}: {person_id}"
            )
        if person.artist_id != self.store.primary_artist_id:
            raise ValidationError("follow-up person belongs to a different Artist")
        normalized_song: str | None = None
        if song_id is not None:
            normalized_song = str(song_id).strip()
            song = self.store.get_song(normalized_song)
            if song is None:
                raise NotFoundError(
                    f"Song not found in profile {self.store.profile_id}: {normalized_song}"
                )
            if song.artist_id != self.store.primary_artist_id:
                raise ValidationError("follow-up Song belongs to a different Artist")
        followup_id = f"followup_{uuid.uuid4().hex}"
        normalized_summary = self._clean_text(summary, "summary", maximum=600)
        normalized_responsibility = self._normalize_responsibility(responsibility)
        normalized_due = self._normalize_due_on(due_on)
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO people_followups("
                    "id,artist_id,person_id,song_id,responsibility,summary,due_on,"
                    "state,resolution_note) VALUES(?,?,?,?,?,?,?,'OPEN',NULL)",
                    (
                        followup_id,
                        self.store.primary_artist_id,
                        person.id,
                        normalized_song,
                        normalized_responsibility,
                        normalized_summary,
                        normalized_due,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot create follow-up: {exc}") from exc
        result = self.get_followup(followup_id)
        if result is None:
            raise LineageCorruptionError("new follow-up disappeared after creation")
        return result

    def get_followup(self, followup_id: str) -> FollowUp | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,person_id,song_id,responsibility,summary,"
            "due_on,state,resolution_note FROM people_followups WHERE id=?",
            (str(followup_id).strip(),),
        ).fetchone()
        return None if row is None else self._followup(row)

    def followups(
        self,
        *,
        state: str | None = None,
        person_id: str | None = None,
    ) -> tuple[FollowUp, ...]:
        clauses: list[str] = []
        params: list[str] = []
        if state is not None:
            normalized_state = str(state).strip().upper()
            if normalized_state not in FOLLOWUP_STATES:
                raise ValidationError(f"unsupported follow-up state: {normalized_state}")
            clauses.append("state=?")
            params.append(normalized_state)
        if person_id is not None:
            person = self.get_person(person_id)
            if person is None:
                raise NotFoundError(
                    f"person not found in profile {self.store.profile_id}: {person_id}"
                )
            clauses.append("person_id=?")
            params.append(person.id)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        return tuple(
            self._followup(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,person_id,song_id,responsibility,summary,"
                "due_on,state,resolution_note FROM people_followups"
                + where
                + " ORDER BY seq",
                params,
            )
        )

    def open_followups(self, *, person_id: str | None = None) -> tuple[FollowUp, ...]:
        return self.followups(state="OPEN", person_id=person_id)

    def _close_followup(
        self,
        followup_id: str,
        *,
        state: str,
        resolution_note: str,
    ) -> FollowUp:
        current = self.get_followup(followup_id)
        if current is None:
            raise NotFoundError(
                f"follow-up not found in profile {self.store.profile_id}: {followup_id}"
            )
        if current.state != "OPEN":
            raise ValidationError("follow-up is already closed")
        note = self._clean_text(
            resolution_note,
            "resolution_note",
            maximum=1000,
        )
        if state not in {"RESOLVED", "CANCELED"}:
            raise ValidationError(f"unsupported follow-up close state: {state}")
        try:
            with self.store._tx():
                changed = self._conn.execute(
                    "UPDATE people_followups SET state=?,resolution_note=? "
                    "WHERE id=? AND state='OPEN'",
                    (state, note, current.id),
                ).rowcount
                if changed != 1:
                    raise LineageCorruptionError(
                        "follow-up changed while its close was being recorded"
                    )
        except LineageCorruptionError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot close follow-up: {exc}") from exc
        result = self.get_followup(current.id)
        if result is None or result.state != state:
            raise LineageCorruptionError("closed follow-up could not be read back")
        return result

    def resolve_followup(self, followup_id: str, *, resolution_note: str) -> FollowUp:
        return self._close_followup(
            followup_id,
            state="RESOLVED",
            resolution_note=resolution_note,
        )

    def cancel_followup(self, followup_id: str, *, reason: str) -> FollowUp:
        return self._close_followup(
            followup_id,
            state="CANCELED",
            resolution_note=reason,
        )
