from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Mapping

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .people import PeopleMemory

CREDITS_SCHEMA_VERSION = 1
CREDIT_TRUTH_TYPE = "USER_DECLARED"
SPLIT_STATES = {"DRAFT", "OPEN_CONFIRMATION", "VOIDED"}
CONFIRMATION_STATUSES = {"RECORDED_CONFIRMED", "RECORDED_DISPUTED"}


@dataclass(frozen=True)
class CreditEntry:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    person_id: str
    role: str
    role_context: str | None
    truth_type: str


@dataclass(frozen=True)
class CompositionSplitSheet:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    state: str
    closure_note: str | None


@dataclass(frozen=True)
class CompositionSplitAllocation:
    sequence: int
    id: str
    sheet_id: str
    artist_id: str
    song_id: str
    person_id: str
    basis_points: int


@dataclass(frozen=True)
class SplitConfirmation:
    sequence: int
    id: str
    sheet_id: str
    artist_id: str
    song_id: str
    person_id: str
    status: str
    note: str
    truth_type: str


class CreditsMemory:
    """Song-bound local credit and composition-split truth.

    This service reuses canonical Artist, Song and Person identities. It records
    what the Artist says was credited or confirmed. It does not certify legal
    ownership, contributor identity, signatures, registration, payment,
    publishing status, royalty entitlement or provider acceptance.
    """

    _TRIGGER_NAMES = {
        "credits_credit_binding_valid",
        "credits_credit_immutable",
        "credits_credit_delete_immutable",
        "credits_credit_activity",
        "credits_split_song_valid",
        "credits_split_binding_immutable",
        "credits_split_transition_valid",
        "credits_split_submit_complete",
        "credits_split_delete_immutable",
        "credits_split_created_activity",
        "credits_split_submitted_activity",
        "credits_split_voided_activity",
        "credits_allocation_binding_valid",
        "credits_allocation_insert_draft_only",
        "credits_allocation_update_draft_only",
        "credits_allocation_delete_draft_only",
        "credits_confirmation_binding_valid",
        "credits_confirmation_participant_only",
        "credits_confirmation_immutable",
        "credits_confirmation_delete_immutable",
        "credits_confirmation_activity",
    }

    def __init__(self, store: LineageStore, people: PeopleMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("CreditsMemory requires the canonical LineageStore")
        if not isinstance(people, PeopleMemory) or people.store is not store:
            raise TypeError("CreditsMemory requires PeopleMemory for the same LineageStore")
        self.store = store
        self.people = people
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
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER credits_credit_binding_valid
            BEFORE INSERT ON song_credits
            WHEN NOT EXISTS (
                SELECT 1 FROM songs s
                WHERE s.id=NEW.song_id AND s.artist_id=NEW.artist_id
            ) OR NOT EXISTS (
                SELECT 1 FROM people_people p
                WHERE p.id=NEW.person_id AND p.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'credit Artist/Song/Person binding is invalid');
            END""",
            """CREATE TRIGGER credits_credit_immutable
            BEFORE UPDATE ON song_credits
            BEGIN
                SELECT RAISE(ABORT, 'credit history is immutable');
            END""",
            """CREATE TRIGGER credits_credit_delete_immutable
            BEFORE DELETE ON song_credits
            BEGIN
                SELECT RAISE(ABORT, 'credit history is immutable');
            END""",
            """CREATE TRIGGER credits_credit_activity
            AFTER INSERT ON song_credits
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'SONG_CREDIT_RECORDED',NEW.artist_id,NEW.song_id,NULL,
                    'SONG_CREDIT',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER credits_split_song_valid
            BEFORE INSERT ON composition_split_sheets
            WHEN NOT EXISTS (
                SELECT 1 FROM songs s
                WHERE s.id=NEW.song_id AND s.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'split Song belongs to a different Artist');
            END""",
            """CREATE TRIGGER credits_split_binding_immutable
            BEFORE UPDATE ON composition_split_sheets
            WHEN NEW.id<>OLD.id OR NEW.artist_id<>OLD.artist_id OR NEW.song_id<>OLD.song_id
            BEGIN
                SELECT RAISE(ABORT, 'split identity and Song binding are immutable');
            END""",
            """CREATE TRIGGER credits_split_transition_valid
            BEFORE UPDATE ON composition_split_sheets
            WHEN NOT (
                OLD.state='DRAFT'
                AND NEW.state='OPEN_CONFIRMATION'
                AND NEW.closure_note IS NULL
            ) AND NOT (
                OLD.state IN ('DRAFT','OPEN_CONFIRMATION')
                AND NEW.state='VOIDED'
                AND NEW.closure_note IS NOT NULL
                AND length(trim(NEW.closure_note))>0
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid composition split state transition');
            END""",
            """CREATE TRIGGER credits_split_submit_complete
            BEFORE UPDATE ON composition_split_sheets
            WHEN OLD.state='DRAFT' AND NEW.state='OPEN_CONFIRMATION'
              AND (
                NOT EXISTS (
                    SELECT 1 FROM composition_split_allocations a
                    WHERE a.sheet_id=OLD.id
                )
                OR COALESCE((
                    SELECT SUM(a.basis_points)
                    FROM composition_split_allocations a
                    WHERE a.sheet_id=OLD.id
                ),0)<>10000
              )
            BEGIN
                SELECT RAISE(ABORT, 'composition split must total exactly 100 percent');
            END""",
            """CREATE TRIGGER credits_split_delete_immutable
            BEFORE DELETE ON composition_split_sheets
            BEGIN
                SELECT RAISE(ABORT, 'split history is immutable');
            END""",
            """CREATE TRIGGER credits_split_created_activity
            AFTER INSERT ON composition_split_sheets
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COMPOSITION_SPLIT_DRAFT_CREATED',NEW.artist_id,NEW.song_id,NULL,
                    'COMPOSITION_SPLIT',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER credits_split_submitted_activity
            AFTER UPDATE OF state ON composition_split_sheets
            WHEN OLD.state='DRAFT' AND NEW.state='OPEN_CONFIRMATION'
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COMPOSITION_SPLIT_SUBMITTED',NEW.artist_id,NEW.song_id,NULL,
                    'COMPOSITION_SPLIT',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER credits_split_voided_activity
            AFTER UPDATE OF state ON composition_split_sheets
            WHEN NEW.state='VOIDED' AND OLD.state<>'VOIDED'
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COMPOSITION_SPLIT_VOIDED',NEW.artist_id,NEW.song_id,NULL,
                    'COMPOSITION_SPLIT',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER credits_allocation_binding_valid
            BEFORE INSERT ON composition_split_allocations
            WHEN NOT EXISTS (
                SELECT 1 FROM composition_split_sheets s
                WHERE s.id=NEW.sheet_id
                  AND s.artist_id=NEW.artist_id
                  AND s.song_id=NEW.song_id
            ) OR NOT EXISTS (
                SELECT 1 FROM people_people p
                WHERE p.id=NEW.person_id AND p.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'split allocation binding is invalid');
            END""",
            """CREATE TRIGGER credits_allocation_insert_draft_only
            BEFORE INSERT ON composition_split_allocations
            WHEN NOT EXISTS (
                SELECT 1 FROM composition_split_sheets s
                WHERE s.id=NEW.sheet_id AND s.state='DRAFT'
            )
            BEGIN
                SELECT RAISE(ABORT, 'submitted split allocations are immutable');
            END""",
            """CREATE TRIGGER credits_allocation_update_draft_only
            BEFORE UPDATE ON composition_split_allocations
            WHEN NEW.id<>OLD.id
              OR NEW.sheet_id<>OLD.sheet_id
              OR NEW.artist_id<>OLD.artist_id
              OR NEW.song_id<>OLD.song_id
              OR NEW.person_id<>OLD.person_id
              OR NOT EXISTS (
                    SELECT 1 FROM composition_split_sheets s
                    WHERE s.id=OLD.sheet_id AND s.state='DRAFT'
              )
            BEGIN
                SELECT RAISE(ABORT, 'submitted split allocations are immutable');
            END""",
            """CREATE TRIGGER credits_allocation_delete_draft_only
            BEFORE DELETE ON composition_split_allocations
            WHEN NOT EXISTS (
                SELECT 1 FROM composition_split_sheets s
                WHERE s.id=OLD.sheet_id AND s.state='DRAFT'
            )
            BEGIN
                SELECT RAISE(ABORT, 'submitted split allocations are immutable');
            END""",
            """CREATE TRIGGER credits_confirmation_binding_valid
            BEFORE INSERT ON composition_split_confirmations
            WHEN NOT EXISTS (
                SELECT 1 FROM composition_split_sheets s
                WHERE s.id=NEW.sheet_id
                  AND s.artist_id=NEW.artist_id
                  AND s.song_id=NEW.song_id
                  AND s.state='OPEN_CONFIRMATION'
            ) OR NOT EXISTS (
                SELECT 1 FROM people_people p
                WHERE p.id=NEW.person_id AND p.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'split confirmation binding is invalid or closed');
            END""",
            """CREATE TRIGGER credits_confirmation_participant_only
            BEFORE INSERT ON composition_split_confirmations
            WHEN NOT EXISTS (
                SELECT 1 FROM composition_split_allocations a
                WHERE a.sheet_id=NEW.sheet_id AND a.person_id=NEW.person_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'only split participants can have confirmation records');
            END""",
            """CREATE TRIGGER credits_confirmation_immutable
            BEFORE UPDATE ON composition_split_confirmations
            BEGIN
                SELECT RAISE(ABORT, 'split confirmation history is immutable');
            END""",
            """CREATE TRIGGER credits_confirmation_delete_immutable
            BEFORE DELETE ON composition_split_confirmations
            BEGIN
                SELECT RAISE(ABORT, 'split confirmation history is immutable');
            END""",
            """CREATE TRIGGER credits_confirmation_activity
            AFTER INSERT ON composition_split_confirmations
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COMPOSITION_SPLIT_CONFIRMATION_RECORDED',
                    NEW.artist_id,NEW.song_id,NULL,
                    'COMPOSITION_SPLIT',NEW.sheet_id,
                    '{\"status\":\"'||NEW.status||'\"}'
                );
            END""",
        )

    def _ensure_schema(self) -> None:
        tables = {
            "song_credits",
            "composition_split_sheets",
            "composition_split_allocations",
            "composition_split_confirmations",
        }
        existing = {name for name in tables if self._table_exists(name)}
        version = self._metadata_value("credits_schema_version")
        any_existing = bool(existing) or version is not None
        if any_existing:
            if existing != tables or version != str(CREDITS_SCHEMA_VERSION):
                raise LineageCorruptionError("Credits schema metadata/table mismatch")
            return
        if not self._table_exists("people_people") or not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "CreditsMemory requires canonical People and Activity memory first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE song_credits (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id),
                        role TEXT NOT NULL COLLATE NOCASE CHECK(length(trim(role))>0),
                        role_context TEXT NULL CHECK(
                            role_context IS NULL OR length(trim(role_context))>0
                        ),
                        truth_type TEXT NOT NULL CHECK(truth_type='USER_DECLARED'),
                        UNIQUE(song_id,person_id,role)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE composition_split_sheets (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        state TEXT NOT NULL CHECK(state IN (
                            'DRAFT','OPEN_CONFIRMATION','VOIDED'
                        )),
                        closure_note TEXT NULL CHECK(
                            closure_note IS NULL OR length(trim(closure_note))>0
                        )
                    )"""
                )
                self._conn.execute(
                    """CREATE UNIQUE INDEX composition_split_one_active_per_song
                    ON composition_split_sheets(song_id)
                    WHERE state IN ('DRAFT','OPEN_CONFIRMATION')"""
                )
                self._conn.execute(
                    """CREATE TABLE composition_split_allocations (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        sheet_id TEXT NOT NULL REFERENCES composition_split_sheets(id),
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id),
                        basis_points INTEGER NOT NULL CHECK(
                            basis_points>=1 AND basis_points<=10000
                        ),
                        UNIQUE(sheet_id,person_id)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE composition_split_confirmations (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        sheet_id TEXT NOT NULL REFERENCES composition_split_sheets(id),
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id),
                        status TEXT NOT NULL CHECK(status IN (
                            'RECORDED_CONFIRMED','RECORDED_DISPUTED'
                        )),
                        note TEXT NOT NULL CHECK(length(trim(note))>0),
                        truth_type TEXT NOT NULL CHECK(truth_type='USER_DECLARED')
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX song_credits_by_song ON song_credits(song_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX split_allocations_by_sheet "
                    "ON composition_split_allocations(sheet_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX split_confirmations_by_sheet_person "
                    "ON composition_split_confirmations(sheet_id,person_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('credits_schema_version',?)",
                    (str(CREDITS_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Credits memory") from exc

    @staticmethod
    def _credit(row: sqlite3.Row) -> CreditEntry:
        return CreditEntry(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            person_id=str(row["person_id"]),
            role=str(row["role"]),
            role_context=None if row["role_context"] is None else str(row["role_context"]),
            truth_type=str(row["truth_type"]),
        )

    @staticmethod
    def _sheet(row: sqlite3.Row) -> CompositionSplitSheet:
        return CompositionSplitSheet(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            state=str(row["state"]),
            closure_note=None if row["closure_note"] is None else str(row["closure_note"]),
        )

    @staticmethod
    def _allocation(row: sqlite3.Row) -> CompositionSplitAllocation:
        return CompositionSplitAllocation(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            sheet_id=str(row["sheet_id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            person_id=str(row["person_id"]),
            basis_points=int(row["basis_points"]),
        )

    @staticmethod
    def _confirmation(row: sqlite3.Row) -> SplitConfirmation:
        return SplitConfirmation(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            sheet_id=str(row["sheet_id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            person_id=str(row["person_id"]),
            status=str(row["status"]),
            note=str(row["note"]),
            truth_type=str(row["truth_type"]),
        )

    def _require_song(self, song_id: str):
        normalized = str(song_id).strip()
        song = self.store.get_song(normalized)
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {normalized}"
            )
        if song.artist_id != self.store.primary_artist_id:
            raise ValidationError("Song belongs to a different Artist")
        return song

    def _require_person(self, person_id: str):
        normalized = str(person_id).strip()
        person = self.people.get_person(normalized)
        if person is None:
            raise NotFoundError(
                f"person not found in profile {self.store.profile_id}: {normalized}"
            )
        if person.artist_id != self.store.primary_artist_id:
            raise ValidationError("person belongs to a different Artist")
        return person

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("credits_schema_version") != str(CREDITS_SCHEMA_VERSION):
                raise LineageCorruptionError("unsupported Credits schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'credits_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"Credits integrity hooks are incomplete: {sorted(missing)}"
                )
            for credit in self.all_credits():
                song = self._require_song(credit.song_id)
                person = self._require_person(credit.person_id)
                if credit.artist_id != song.artist_id or credit.artist_id != person.artist_id:
                    raise LineageCorruptionError("credit binding does not match its Artist")
                if credit.truth_type != CREDIT_TRUTH_TYPE:
                    raise LineageCorruptionError("credit truth type is invalid")
            for sheet in self.all_split_sheets():
                song = self._require_song(sheet.song_id)
                if sheet.artist_id != song.artist_id:
                    raise LineageCorruptionError("split binding does not match its Artist")
                if sheet.state not in SPLIT_STATES:
                    raise LineageCorruptionError("split state is invalid")
                allocations = self.split_allocations(sheet.id)
                total = sum(item.basis_points for item in allocations)
                if total > 10000:
                    raise LineageCorruptionError("split allocations exceed 100 percent")
                if sheet.state == "OPEN_CONFIRMATION" and (not allocations or total != 10000):
                    raise LineageCorruptionError(
                        "submitted split is not arithmetically complete"
                    )
                if sheet.state == "VOIDED" and sheet.closure_note is None:
                    raise LineageCorruptionError("voided split is missing a reason")
                if sheet.state != "VOIDED" and sheet.closure_note is not None:
                    raise LineageCorruptionError("open split unexpectedly has a closure note")
                participant_ids = {item.person_id for item in allocations}
                for allocation in allocations:
                    person = self._require_person(allocation.person_id)
                    if (
                        allocation.artist_id != sheet.artist_id
                        or allocation.song_id != sheet.song_id
                        or person.artist_id != sheet.artist_id
                    ):
                        raise LineageCorruptionError("split allocation binding is invalid")
                for confirmation in self.confirmation_history(sheet.id):
                    if confirmation.person_id not in participant_ids:
                        raise LineageCorruptionError(
                            "split confirmation belongs to a non-participant"
                        )
                    if confirmation.status not in CONFIRMATION_STATUSES:
                        raise LineageCorruptionError("split confirmation status is invalid")
                    if confirmation.truth_type != CREDIT_TRUTH_TYPE:
                        raise LineageCorruptionError("split confirmation truth type is invalid")
                    if (
                        confirmation.artist_id != sheet.artist_id
                        or confirmation.song_id != sheet.song_id
                    ):
                        raise LineageCorruptionError("split confirmation binding is invalid")
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError, ValidationError, NotFoundError) as exc:
            raise LineageCorruptionError("Credits memory is unreadable or corrupt") from exc

    def record_credit(
        self,
        song_id: str,
        person_id: str,
        role: str,
        *,
        role_context: str | None = None,
    ) -> CreditEntry:
        song = self._require_song(song_id)
        person = self._require_person(person_id)
        normalized_role = self._clean_text(role, "role", maximum=120)
        context = self._optional_text(role_context, "role_context", maximum=500)
        credit_id = f"credit_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO song_credits("
                    "id,artist_id,song_id,person_id,role,role_context,truth_type"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        credit_id,
                        self.store.primary_artist_id,
                        song.id,
                        person.id,
                        normalized_role,
                        context,
                        CREDIT_TRUTH_TYPE,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot record Song credit: {exc}") from exc
        credit = self.get_credit(credit_id)
        if credit is None:
            raise LineageCorruptionError("new Song credit disappeared after creation")
        return credit

    def get_credit(self, credit_id: str) -> CreditEntry | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,person_id,role,role_context,truth_type "
            "FROM song_credits WHERE id=?",
            (str(credit_id).strip(),),
        ).fetchone()
        return None if row is None else self._credit(row)

    def all_credits(self) -> tuple[CreditEntry, ...]:
        return tuple(
            self._credit(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,person_id,role,role_context,truth_type "
                "FROM song_credits ORDER BY seq"
            )
        )

    def credits_for_song(self, song_id: str) -> tuple[CreditEntry, ...]:
        song = self._require_song(song_id)
        return tuple(
            self._credit(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,person_id,role,role_context,truth_type "
                "FROM song_credits WHERE song_id=? ORDER BY seq",
                (song.id,),
            )
        )

    def create_split_draft(self, song_id: str) -> CompositionSplitSheet:
        song = self._require_song(song_id)
        sheet_id = f"split_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO composition_split_sheets("
                    "id,artist_id,song_id,state,closure_note"
                    ") VALUES(?,?,?,'DRAFT',NULL)",
                    (sheet_id, self.store.primary_artist_id, song.id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                "this Song already has an active composition split proposal"
            ) from exc
        sheet = self.get_split_sheet(sheet_id)
        if sheet is None:
            raise LineageCorruptionError("new split draft disappeared after creation")
        return sheet

    def get_split_sheet(self, sheet_id: str) -> CompositionSplitSheet | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,state,closure_note "
            "FROM composition_split_sheets WHERE id=?",
            (str(sheet_id).strip(),),
        ).fetchone()
        return None if row is None else self._sheet(row)

    def all_split_sheets(self) -> tuple[CompositionSplitSheet, ...]:
        return tuple(
            self._sheet(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,state,closure_note "
                "FROM composition_split_sheets ORDER BY seq"
            )
        )

    def split_history(self, song_id: str) -> tuple[CompositionSplitSheet, ...]:
        song = self._require_song(song_id)
        return tuple(
            self._sheet(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,state,closure_note "
                "FROM composition_split_sheets WHERE song_id=? ORDER BY seq",
                (song.id,),
            )
        )

    def active_split_for_song(self, song_id: str) -> CompositionSplitSheet | None:
        song = self._require_song(song_id)
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,state,closure_note "
            "FROM composition_split_sheets "
            "WHERE song_id=? AND state IN ('DRAFT','OPEN_CONFIRMATION')",
            (song.id,),
        ).fetchone()
        return None if row is None else self._sheet(row)

    def split_allocations(self, sheet_id: str) -> tuple[CompositionSplitAllocation, ...]:
        return tuple(
            self._allocation(row)
            for row in self._conn.execute(
                "SELECT seq,id,sheet_id,artist_id,song_id,person_id,basis_points "
                "FROM composition_split_allocations WHERE sheet_id=? ORDER BY seq",
                (str(sheet_id).strip(),),
            )
        )

    def set_draft_allocations(
        self,
        sheet_id: str,
        allocations: Mapping[str, int],
    ) -> tuple[CompositionSplitAllocation, ...]:
        sheet = self.get_split_sheet(sheet_id)
        if sheet is None:
            raise NotFoundError(
                f"composition split not found in profile {self.store.profile_id}: {sheet_id}"
            )
        if sheet.state != "DRAFT":
            raise ValidationError("submitted composition split allocations are immutable")
        normalized: list[tuple[str, int]] = []
        seen: set[str] = set()
        for person_id, raw_basis_points in allocations.items():
            person = self._require_person(person_id)
            if person.id in seen:
                raise ValidationError("a split participant may appear only once")
            seen.add(person.id)
            if type(raw_basis_points) is not int:
                raise ValidationError("split shares must be whole basis points")
            basis_points = raw_basis_points
            if basis_points <= 0 or basis_points > 10000:
                raise ValidationError("split shares must be between 0.01 and 100 percent")
            normalized.append((person.id, basis_points))
        if sum(value for _, value in normalized) > 10000:
            raise ValidationError("draft composition split exceeds 100 percent")
        try:
            with self.store._tx():
                self._conn.execute(
                    "DELETE FROM composition_split_allocations WHERE sheet_id=?",
                    (sheet.id,),
                )
                for person_id, basis_points in normalized:
                    self._conn.execute(
                        "INSERT INTO composition_split_allocations("
                        "id,sheet_id,artist_id,song_id,person_id,basis_points"
                        ") VALUES(?,?,?,?,?,?)",
                        (
                            f"allocation_{uuid.uuid4().hex}",
                            sheet.id,
                            sheet.artist_id,
                            sheet.song_id,
                            person_id,
                            basis_points,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot save composition split draft: {exc}") from exc
        return self.split_allocations(sheet.id)

    def submit_split(self, sheet_id: str) -> CompositionSplitSheet:
        sheet = self.get_split_sheet(sheet_id)
        if sheet is None:
            raise NotFoundError(
                f"composition split not found in profile {self.store.profile_id}: {sheet_id}"
            )
        if sheet.state != "DRAFT":
            raise ValidationError("only a draft composition split can be submitted")
        allocations = self.split_allocations(sheet.id)
        total = sum(item.basis_points for item in allocations)
        if not allocations or total != 10000:
            raise ValidationError("composition split must total exactly 100.00 percent")
        try:
            with self.store._tx():
                changed = self._conn.execute(
                    "UPDATE composition_split_sheets SET state='OPEN_CONFIRMATION' "
                    "WHERE id=? AND state='DRAFT'",
                    (sheet.id,),
                ).rowcount
                if changed != 1:
                    raise LineageCorruptionError(
                        "composition split changed while it was being submitted"
                    )
        except LineageCorruptionError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot submit composition split: {exc}") from exc
        result = self.get_split_sheet(sheet.id)
        if result is None or result.state != "OPEN_CONFIRMATION":
            raise LineageCorruptionError("submitted composition split could not be read back")
        return result

    def confirmation_history(
        self,
        sheet_id: str,
        *,
        person_id: str | None = None,
    ) -> tuple[SplitConfirmation, ...]:
        normalized_sheet = str(sheet_id).strip()
        if person_id is None:
            rows = self._conn.execute(
                "SELECT seq,id,sheet_id,artist_id,song_id,person_id,status,note,truth_type "
                "FROM composition_split_confirmations WHERE sheet_id=? ORDER BY seq",
                (normalized_sheet,),
            )
        else:
            person = self._require_person(person_id)
            rows = self._conn.execute(
                "SELECT seq,id,sheet_id,artist_id,song_id,person_id,status,note,truth_type "
                "FROM composition_split_confirmations "
                "WHERE sheet_id=? AND person_id=? ORDER BY seq",
                (normalized_sheet, person.id),
            )
        return tuple(self._confirmation(row) for row in rows)

    def latest_confirmation(
        self,
        sheet_id: str,
        person_id: str,
    ) -> SplitConfirmation | None:
        person = self._require_person(person_id)
        row = self._conn.execute(
            "SELECT seq,id,sheet_id,artist_id,song_id,person_id,status,note,truth_type "
            "FROM composition_split_confirmations "
            "WHERE sheet_id=? AND person_id=? ORDER BY seq DESC LIMIT 1",
            (str(sheet_id).strip(), person.id),
        ).fetchone()
        return None if row is None else self._confirmation(row)

    def record_confirmation(
        self,
        sheet_id: str,
        person_id: str,
        *,
        status: str,
        note: str,
    ) -> SplitConfirmation:
        sheet = self.get_split_sheet(sheet_id)
        if sheet is None:
            raise NotFoundError(
                f"composition split not found in profile {self.store.profile_id}: {sheet_id}"
            )
        if sheet.state != "OPEN_CONFIRMATION":
            raise ValidationError("split confirmations can only be recorded after submission")
        person = self._require_person(person_id)
        participants = {item.person_id for item in self.split_allocations(sheet.id)}
        if person.id not in participants:
            raise ValidationError("only split participants can have confirmation records")
        normalized_status = str(status).strip().upper()
        if normalized_status not in CONFIRMATION_STATUSES:
            raise ValidationError(f"unsupported split confirmation status: {normalized_status}")
        normalized_note = self._clean_text(note, "confirmation note", maximum=1000)
        latest = self.latest_confirmation(sheet.id, person.id)
        if latest is not None and latest.status == normalized_status:
            raise ValidationError("that confirmation state is already the latest recorded state")
        confirmation_id = f"confirmation_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO composition_split_confirmations("
                    "id,sheet_id,artist_id,song_id,person_id,status,note,truth_type"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        confirmation_id,
                        sheet.id,
                        sheet.artist_id,
                        sheet.song_id,
                        person.id,
                        normalized_status,
                        normalized_note,
                        CREDIT_TRUTH_TYPE,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot record split confirmation: {exc}") from exc
        result = self.latest_confirmation(sheet.id, person.id)
        if result is None or result.id != confirmation_id:
            raise LineageCorruptionError("split confirmation disappeared after creation")
        return result

    def confirmation_state(self, sheet_id: str, person_id: str) -> str:
        latest = self.latest_confirmation(sheet_id, person_id)
        return "PENDING" if latest is None else latest.status

    def all_recorded_confirmed(self, sheet_id: str) -> bool:
        sheet = self.get_split_sheet(sheet_id)
        if sheet is None or sheet.state != "OPEN_CONFIRMATION":
            return False
        allocations = self.split_allocations(sheet.id)
        if not allocations:
            return False
        return all(
            self.confirmation_state(sheet.id, allocation.person_id)
            == "RECORDED_CONFIRMED"
            for allocation in allocations
        )

    def void_split(self, sheet_id: str, *, reason: str) -> CompositionSplitSheet:
        sheet = self.get_split_sheet(sheet_id)
        if sheet is None:
            raise NotFoundError(
                f"composition split not found in profile {self.store.profile_id}: {sheet_id}"
            )
        if sheet.state == "VOIDED":
            raise ValidationError("composition split is already voided")
        normalized_reason = self._clean_text(reason, "void reason", maximum=1000)
        try:
            with self.store._tx():
                changed = self._conn.execute(
                    "UPDATE composition_split_sheets SET state='VOIDED',closure_note=? "
                    "WHERE id=? AND state IN ('DRAFT','OPEN_CONFIRMATION')",
                    (normalized_reason, sheet.id),
                ).rowcount
                if changed != 1:
                    raise LineageCorruptionError(
                        "composition split changed while it was being voided"
                    )
        except LineageCorruptionError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot void composition split: {exc}") from exc
        result = self.get_split_sheet(sheet.id)
        if result is None or result.state != "VOIDED":
            raise LineageCorruptionError("voided composition split could not be read back")
        return result
