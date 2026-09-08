from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date

from .evidence import EvidenceClaim, EvidenceMemory
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .people import FOLLOWUP_RESPONSIBILITIES, PeopleMemory

OBLIGATION_SCHEMA_VERSION = 1
OBLIGATION_KINDS = ("DELIVERABLE", "DEADLINE", "LICENSE", "PAYMENT", "OTHER")
OBLIGATION_STATUSES = ("OPEN", "BLOCKED", "DISPUTED", "SATISFIED", "WAIVED", "CANCELED")
TERMINAL_OBLIGATION_STATUSES = {"SATISFIED", "WAIVED", "CANCELED"}
OBLIGATION_AUTHORITY = "EVIDENCE_ONLY"
_PROVENANCE_REQUIRED = {"OBSERVED", "MEASURED", "PROVIDER_VERIFIED", "INFERRED"}


class ObligationError(RuntimeError):
    """Obligation state could not be represented truthfully."""


@dataclass(frozen=True)
class ObligationEvent:
    sequence: int
    id: str
    obligation_id: str
    status: str
    evidence_claim_id: str
    source_kind: str
    source_ref: str | None
    note: str | None

    @property
    def action_authority_granted(self) -> bool:
        return False


@dataclass(frozen=True)
class ObligationTriggerEvent:
    sequence: int
    id: str
    obligation_id: str
    evidence_claim_id: str
    source_kind: str
    source_ref: str | None
    note: str

    @property
    def action_authority_granted(self) -> bool:
        return False


@dataclass(frozen=True)
class ObligationSnapshot:
    sequence: int
    id: str
    profile_id: str
    artist_id: str
    person_id: str
    person_display_name: str
    song_id: str | None
    followup_id: str | None
    followup_state: str | None
    kind: str
    responsibility: str
    summary: str
    due_on: str | None
    trigger_ref: str | None
    consequence_note: str | None
    source_claim_id: str
    source_kind: str
    source_ref: str | None
    source_current: bool
    events: tuple[ObligationEvent, ...]
    trigger_events: tuple[ObligationTriggerEvent, ...]
    authority: str = OBLIGATION_AUTHORITY

    @property
    def status(self) -> str:
        if not self.events:
            raise ObligationError("obligation has no lifecycle evidence")
        return self.events[-1].status

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_OBLIGATION_STATUSES

    @property
    def latest_status_evidence_current(self) -> bool:
        # Filled by service validation: active lifecycle evidence is required at
        # write time, but later supersession must not rewrite historical status.
        return True

    @property
    def trigger_state(self) -> str:
        if self.trigger_ref is None:
            return "NONE"
        return "OBSERVED" if self.trigger_events else "PENDING"

    @property
    def source_truth_class(self) -> str:
        if self.source_kind == "PROVIDER_VERIFIED":
            return "PROVIDER_VERIFIED"
        if self.source_kind in {"OBSERVED", "MEASURED"}:
            return "OBSERVED"
        if self.source_kind in {"USER_DECLARED", "REMEMBERED"}:
            return "DECLARED"
        return "INFERRED"

    @staticmethod
    def _as_of(value: date | str) -> date:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValidationError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc

    def due_state(self, *, as_of: date | str) -> str:
        if self.terminal:
            return "CLOSED"
        if self.trigger_ref is not None and not self.trigger_events:
            return "WAITING_FOR_TRIGGER"
        if self.due_on is None:
            return "UNSCHEDULED"
        current = self._as_of(as_of)
        due = date.fromisoformat(self.due_on)
        if current < due:
            return "UPCOMING"
        if current == due:
            return "DUE"
        return "OVERDUE"

    def attention_state(self, *, as_of: date | str) -> str:
        if self.terminal:
            return "CLOSED"
        if not self.source_current:
            return "NEEDS_REVALIDATION"
        if self.followup_state in {"RESOLVED", "CANCELED"}:
            return "NEEDS_RECONCILIATION"
        if self.status == "BLOCKED":
            return "BLOCKED"
        if self.status == "DISPUTED":
            return "DISPUTED"
        timing = self.due_state(as_of=as_of)
        if timing == "WAITING_FOR_TRIGGER":
            return "WAITING"
        if timing == "OVERDUE":
            return "OVERDUE"
        return "ACTIONABLE"

    @property
    def legal_entitlement_verified(self) -> bool:
        return False

    @property
    def payment_authority_granted(self) -> bool:
        return False

    @property
    def license_authority_granted(self) -> bool:
        return False

    @property
    def messaging_authority_granted(self) -> bool:
        return False

    @property
    def calendar_authority_granted(self) -> bool:
        return False

    @property
    def external_action_authority_granted(self) -> bool:
        return False

    @property
    def priority_score(self) -> None:
        return None


class ObligationMemory:
    """Source-bound obligations inside the canonical profile database.

    This layer makes explicit promises/waiting/deliverables/deadlines/licenses/
    payments queryable without making them legal, financial, calendar, message,
    or task-execution authority. It reuses People and Evidence identity/truth and
    writes only append-only obligation lifecycle/trigger history.
    """

    _TRIGGER_NAMES = {
        "obligation_binding_immutable",
        "obligation_delete_immutable",
        "obligation_person_same_artist",
        "obligation_song_same_artist",
        "obligation_followup_binding_matches",
        "obligation_source_scope_matches",
        "obligation_event_immutable_update",
        "obligation_event_immutable_delete",
        "obligation_event_evidence_scope_matches",
        "obligation_event_transition_shape",
        "obligation_trigger_immutable_update",
        "obligation_trigger_immutable_delete",
        "obligation_trigger_evidence_scope_matches",
        "obligation_created_activity",
        "obligation_status_activity",
        "obligation_trigger_activity",
    }

    def __init__(
        self,
        store: LineageStore,
        people: PeopleMemory,
        evidence: EvidenceMemory,
    ) -> None:
        if not isinstance(store, LineageStore):
            raise TypeError("ObligationMemory requires LineageStore")
        if not isinstance(people, PeopleMemory) or people.store is not store:
            raise TypeError("ObligationMemory requires PeopleMemory for the same LineageStore")
        if not isinstance(evidence, EvidenceMemory) or evidence.store is not store:
            raise TypeError("ObligationMemory requires EvidenceMemory for the same LineageStore")
        self.store = store
        self.people = people
        self.evidence = evidence
        self._conn = store._conn
        self._ensure_schema()
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
        scope_match = """(
            c.scope_kind='ARTIST' AND c.scope_id=NEW.artist_id
            OR c.scope_kind='SONG' AND NEW.song_id IS NOT NULL AND c.scope_id=NEW.song_id
            OR c.scope_kind='VERSION' AND NEW.song_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM versions v WHERE v.id=c.scope_id AND v.song_id=NEW.song_id
            )
        )"""
        event_scope_match = """(
            c.scope_kind='ARTIST' AND c.scope_id=o.artist_id
            OR c.scope_kind='SONG' AND o.song_id IS NOT NULL AND c.scope_id=o.song_id
            OR c.scope_kind='VERSION' AND o.song_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM versions v WHERE v.id=c.scope_id AND v.song_id=o.song_id
            )
        )"""
        return (
            """CREATE TRIGGER obligation_binding_immutable
            BEFORE UPDATE ON business_obligations
            BEGIN
                SELECT RAISE(ABORT, 'obligation identity and binding are immutable');
            END""",
            """CREATE TRIGGER obligation_delete_immutable
            BEFORE DELETE ON business_obligations
            BEGIN
                SELECT RAISE(ABORT, 'obligation history is immutable');
            END""",
            """CREATE TRIGGER obligation_person_same_artist
            BEFORE INSERT ON business_obligations
            WHEN NOT EXISTS (
                SELECT 1 FROM people_people p
                WHERE p.id=NEW.person_id AND p.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'obligation person belongs to a different Artist');
            END""",
            """CREATE TRIGGER obligation_song_same_artist
            BEFORE INSERT ON business_obligations
            WHEN NEW.song_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM songs s
                WHERE s.id=NEW.song_id AND s.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'obligation Song belongs to a different Artist');
            END""",
            """CREATE TRIGGER obligation_followup_binding_matches
            BEFORE INSERT ON business_obligations
            WHEN NEW.followup_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM people_followups f
                WHERE f.id=NEW.followup_id
                  AND f.artist_id=NEW.artist_id
                  AND f.person_id=NEW.person_id
                  AND f.song_id IS NEW.song_id
                  AND f.responsibility=NEW.responsibility
                  AND (f.due_on IS NULL OR f.due_on IS NEW.due_on)
            )
            BEGIN
                SELECT RAISE(ABORT, 'obligation follow-up binding does not match');
            END""",
            f"""CREATE TRIGGER obligation_source_scope_matches
            BEFORE INSERT ON business_obligations
            WHEN NOT EXISTS (
                SELECT 1 FROM evidence_claims c
                WHERE c.id=NEW.source_claim_id AND {scope_match}
            )
            BEGIN
                SELECT RAISE(ABORT, 'obligation source evidence scope does not match');
            END""",
            """CREATE TRIGGER obligation_event_immutable_update
            BEFORE UPDATE ON business_obligation_events
            BEGIN
                SELECT RAISE(ABORT, 'obligation lifecycle evidence is append-only');
            END""",
            """CREATE TRIGGER obligation_event_immutable_delete
            BEFORE DELETE ON business_obligation_events
            BEGIN
                SELECT RAISE(ABORT, 'obligation lifecycle evidence is append-only');
            END""",
            f"""CREATE TRIGGER obligation_event_evidence_scope_matches
            BEFORE INSERT ON business_obligation_events
            WHEN NOT EXISTS (
                SELECT 1 FROM business_obligations o
                JOIN evidence_claims c ON c.id=NEW.evidence_claim_id
                WHERE o.id=NEW.obligation_id AND {event_scope_match}
            )
            BEGIN
                SELECT RAISE(ABORT, 'obligation lifecycle evidence scope does not match');
            END""",
            """CREATE TRIGGER obligation_event_transition_shape
            BEFORE INSERT ON business_obligation_events
            WHEN (
                (NOT EXISTS (
                    SELECT 1 FROM business_obligation_events e
                    WHERE e.obligation_id=NEW.obligation_id
                ) AND NEW.status<>'OPEN')
                OR
                (EXISTS (
                    SELECT 1 FROM business_obligation_events e
                    WHERE e.obligation_id=NEW.obligation_id
                ) AND (
                    (SELECT e.status FROM business_obligation_events e
                     WHERE e.obligation_id=NEW.obligation_id ORDER BY e.seq DESC LIMIT 1)
                    IN ('SATISFIED','WAIVED','CANCELED')
                ))
                OR
                (EXISTS (
                    SELECT 1 FROM business_obligation_events e
                    WHERE e.obligation_id=NEW.obligation_id
                ) AND NEW.status=(
                    SELECT e.status FROM business_obligation_events e
                    WHERE e.obligation_id=NEW.obligation_id ORDER BY e.seq DESC LIMIT 1
                ))
                OR
                (NEW.status='OPEN' AND EXISTS (
                    SELECT 1 FROM business_obligation_events e
                    WHERE e.obligation_id=NEW.obligation_id
                ) AND (
                    SELECT e.status FROM business_obligation_events e
                    WHERE e.obligation_id=NEW.obligation_id ORDER BY e.seq DESC LIMIT 1
                ) NOT IN ('BLOCKED','DISPUTED'))
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid obligation lifecycle transition');
            END""",
            """CREATE TRIGGER obligation_trigger_immutable_update
            BEFORE UPDATE ON business_obligation_triggers
            BEGIN
                SELECT RAISE(ABORT, 'obligation trigger evidence is append-only');
            END""",
            """CREATE TRIGGER obligation_trigger_immutable_delete
            BEFORE DELETE ON business_obligation_triggers
            BEGIN
                SELECT RAISE(ABORT, 'obligation trigger evidence is append-only');
            END""",
            f"""CREATE TRIGGER obligation_trigger_evidence_scope_matches
            BEFORE INSERT ON business_obligation_triggers
            WHEN NOT EXISTS (
                SELECT 1 FROM business_obligations o
                JOIN evidence_claims c ON c.id=NEW.evidence_claim_id
                WHERE o.id=NEW.obligation_id AND o.trigger_ref IS NOT NULL
                  AND {event_scope_match}
            )
            BEGIN
                SELECT RAISE(ABORT, 'obligation trigger evidence scope does not match');
            END""",
            """CREATE TRIGGER obligation_created_activity
            AFTER INSERT ON business_obligations
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'OBLIGATION_CREATED',NEW.artist_id,NEW.song_id,NULL,
                    'OBLIGATION',NEW.id,
                    '{\"kind\":\"'||NEW.kind||'\",\"responsibility\":\"'||NEW.responsibility||'\"}'
                );
            END""",
            """CREATE TRIGGER obligation_status_activity
            AFTER INSERT ON business_obligation_events
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                ) SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'OBLIGATION_STATUS_'||NEW.status,o.artist_id,o.song_id,NULL,
                    'OBLIGATION',o.id,
                    '{\"status\":\"'||NEW.status||'\"}'
                FROM business_obligations o WHERE o.id=NEW.obligation_id;
            END""",
            """CREATE TRIGGER obligation_trigger_activity
            AFTER INSERT ON business_obligation_triggers
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                ) SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'OBLIGATION_TRIGGER_OBSERVED',o.artist_id,o.song_id,NULL,
                    'OBLIGATION',o.id,'{}'
                FROM business_obligations o WHERE o.id=NEW.obligation_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        tables = {
            name: self._table_exists(name)
            for name in (
                "business_obligations",
                "business_obligation_events",
                "business_obligation_triggers",
            )
        }
        version = self._metadata_value("obligation_schema_version")
        if any(tables.values()) or version is not None:
            if not all(tables.values()) or version != str(OBLIGATION_SCHEMA_VERSION):
                raise LineageCorruptionError("Obligation schema metadata/table mismatch")
            return
        for required in ("activity_events", "people_people", "people_followups", "evidence_claims"):
            if not self._table_exists(required):
                raise LineageCorruptionError(
                    f"ObligationMemory requires canonical {required} first"
                )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE business_obligations (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id),
                        song_id TEXT NULL REFERENCES songs(id),
                        followup_id TEXT NULL REFERENCES people_followups(id),
                        kind TEXT NOT NULL CHECK(kind IN (
                            'DELIVERABLE','DEADLINE','LICENSE','PAYMENT','OTHER'
                        )),
                        responsibility TEXT NOT NULL CHECK(responsibility IN (
                            'ARTIST_OWES','WAITING_ON_OTHER','MUTUAL'
                        )),
                        summary TEXT NOT NULL CHECK(length(trim(summary))>0),
                        due_on TEXT NULL,
                        trigger_ref TEXT NULL CHECK(
                            trigger_ref IS NULL OR length(trim(trigger_ref))>0
                        ),
                        source_claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
                        consequence_note TEXT NULL CHECK(
                            consequence_note IS NULL OR length(trim(consequence_note))>0
                        )
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE business_obligation_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        obligation_id TEXT NOT NULL REFERENCES business_obligations(id),
                        status TEXT NOT NULL CHECK(status IN (
                            'OPEN','BLOCKED','DISPUTED','SATISFIED','WAIVED','CANCELED'
                        )),
                        evidence_claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
                        note TEXT NULL CHECK(note IS NULL OR length(trim(note))>0)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE business_obligation_triggers (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        obligation_id TEXT NOT NULL REFERENCES business_obligations(id),
                        evidence_claim_id TEXT NOT NULL REFERENCES evidence_claims(id),
                        note TEXT NOT NULL CHECK(length(trim(note))>0)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX obligation_open_by_person "
                    "ON business_obligations(person_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX obligation_by_song "
                    "ON business_obligations(song_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX obligation_event_history "
                    "ON business_obligation_events(obligation_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX obligation_trigger_history "
                    "ON business_obligation_triggers(obligation_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('obligation_schema_version',?)",
                    (str(OBLIGATION_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Obligation memory") from exc

    @staticmethod
    def _clean_text(value: object, field: str, maximum: int) -> str:
        text = " ".join(str(value).split())
        if not text:
            raise ValidationError(f"{field} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return text

    @classmethod
    def _optional_text(cls, value: object | None, field: str, maximum: int) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        if not text:
            return None
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return text

    @staticmethod
    def _enum(value: object, field: str, allowed: tuple[str, ...] | set[str]) -> str:
        text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        if text not in allowed:
            raise ValidationError(f"unsupported {field}: {text}")
        return text

    @staticmethod
    def _due_on(value: object | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        text = str(value).strip()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValidationError("due_on must be an ISO calendar date (YYYY-MM-DD)") from exc

    def _person(self, person_id: str):
        person = self.people.get_person(str(person_id).strip())
        if person is None:
            raise NotFoundError(f"person not found: {person_id}")
        if person.artist_id != self.store.primary_artist_id:
            raise ValidationError("obligation person belongs to a different Artist")
        return person

    def _song(self, song_id: object | None) -> str | None:
        if song_id is None or not str(song_id).strip():
            return None
        value = str(song_id).strip()
        song = self.store.get_song(value)
        if song is None:
            raise NotFoundError(f"Song not found: {value}")
        if song.artist_id != self.store.primary_artist_id:
            raise ValidationError("obligation Song belongs to a different Artist")
        return value

    def _claim_compatible(self, claim: EvidenceClaim, song_id: str | None) -> bool:
        if claim.scope_kind == "ARTIST":
            return claim.scope_id == self.store.primary_artist_id
        if song_id is None:
            return False
        if claim.scope_kind == "SONG":
            return claim.scope_id == song_id
        if claim.scope_kind == "VERSION":
            version = self.store.get_version(claim.scope_id)
            return version is not None and version.song_id == song_id
        return False

    def _claim_current(self, claim: EvidenceClaim) -> bool:
        return claim.id in {
            item.id
            for item in self.evidence.active_claims(
                claim.scope_kind, claim.scope_id, claim.key
            )
        }

    def _evidence_claim(self, claim_id: str, *, song_id: str | None) -> EvidenceClaim:
        claim = self.evidence.get_claim(str(claim_id).strip())
        if claim is None:
            raise NotFoundError(f"evidence claim not found: {claim_id}")
        if not self._claim_compatible(claim, song_id):
            raise ValidationError("obligation evidence scope does not match Artist/Song binding")
        if claim.source_kind in _PROVENANCE_REQUIRED and not (
            claim.source_ref is not None and str(claim.source_ref).strip()
        ):
            raise ValidationError(
                f"{claim.source_kind} obligation evidence requires source_ref provenance"
            )
        if not self._claim_current(claim):
            raise ValidationError("obligation may bind only currently active evidence")
        return claim

    def _followup(
        self,
        followup_id: object | None,
        *,
        person_id: str,
        song_id: str | None,
        responsibility: str,
        due_on: str | None,
    ) -> str | None:
        if followup_id is None or not str(followup_id).strip():
            return None
        value = str(followup_id).strip()
        followup = self.people.get_followup(value)
        if followup is None:
            raise NotFoundError(f"follow-up not found: {value}")
        if (
            followup.artist_id != self.store.primary_artist_id
            or followup.person_id != person_id
            or followup.song_id != song_id
            or followup.responsibility != responsibility
        ):
            raise ValidationError("obligation follow-up binding does not match")
        if followup.due_on is not None and followup.due_on != due_on:
            raise ValidationError("obligation due_on conflicts with linked follow-up")
        return value

    def create_obligation(
        self,
        person_id: str,
        *,
        kind: str,
        responsibility: str,
        summary: str,
        source_claim_id: str,
        song_id: str | None = None,
        followup_id: str | None = None,
        due_on: str | None = None,
        trigger_ref: str | None = None,
        consequence_note: str | None = None,
    ) -> ObligationSnapshot:
        person = self._person(person_id)
        normalized_song = self._song(song_id)
        normalized_kind = self._enum(kind, "obligation kind", OBLIGATION_KINDS)
        normalized_responsibility = self._enum(
            responsibility, "obligation responsibility", FOLLOWUP_RESPONSIBILITIES
        )
        normalized_summary = self._clean_text(summary, "summary", 1200)
        normalized_due = self._due_on(due_on)
        normalized_trigger = self._optional_text(trigger_ref, "trigger_ref", 800)
        normalized_consequence = self._optional_text(
            consequence_note, "consequence_note", 1200
        )
        source = self._evidence_claim(source_claim_id, song_id=normalized_song)
        normalized_followup = self._followup(
            followup_id,
            person_id=person.id,
            song_id=normalized_song,
            responsibility=normalized_responsibility,
            due_on=normalized_due,
        )
        obligation_id = f"obl_{uuid.uuid4().hex}"
        event_id = f"oble_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO business_obligations("
                    "id,artist_id,person_id,song_id,followup_id,kind,responsibility,"
                    "summary,due_on,trigger_ref,source_claim_id,consequence_note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        obligation_id,
                        self.store.primary_artist_id,
                        person.id,
                        normalized_song,
                        normalized_followup,
                        normalized_kind,
                        normalized_responsibility,
                        normalized_summary,
                        normalized_due,
                        normalized_trigger,
                        source.id,
                        normalized_consequence,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO business_obligation_events("
                    "id,obligation_id,status,evidence_claim_id,note) "
                    "VALUES(?,?,'OPEN',?,NULL)",
                    (event_id, obligation_id, source.id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot create obligation: {exc}") from exc
        return self.get(obligation_id)

    def record_trigger(
        self,
        obligation_id: str,
        *,
        evidence_claim_id: str,
        note: str,
    ) -> ObligationSnapshot:
        current = self.get(obligation_id)
        if current.trigger_ref is None:
            raise ValidationError("obligation has no explicit trigger to observe")
        if current.terminal:
            raise ValidationError("terminal obligation cannot receive trigger evidence")
        claim = self._evidence_claim(evidence_claim_id, song_id=current.song_id)
        normalized_note = self._clean_text(note, "note", 1200)
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO business_obligation_triggers("
                    "id,obligation_id,evidence_claim_id,note) VALUES(?,?,?,?)",
                    (f"oblt_{uuid.uuid4().hex}", current.id, claim.id, normalized_note),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot record obligation trigger: {exc}") from exc
        return self.get(current.id)

    def transition(
        self,
        obligation_id: str,
        *,
        status: str,
        evidence_claim_id: str,
        note: str,
    ) -> ObligationSnapshot:
        current = self.get(obligation_id)
        normalized = self._enum(status, "obligation status", OBLIGATION_STATUSES)
        if current.terminal:
            raise ValidationError("terminal obligation lifecycle is immutable")
        if normalized == current.status:
            raise ValidationError("obligation lifecycle transition must change status")
        if normalized == "OPEN" and current.status not in {"BLOCKED", "DISPUTED"}:
            raise ValidationError("OPEN may only reopen BLOCKED or DISPUTED obligations")
        claim = self._evidence_claim(evidence_claim_id, song_id=current.song_id)
        normalized_note = self._clean_text(note, "note", 1200)
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO business_obligation_events("
                    "id,obligation_id,status,evidence_claim_id,note) VALUES(?,?,?,?,?)",
                    (
                        f"oble_{uuid.uuid4().hex}",
                        current.id,
                        normalized,
                        claim.id,
                        normalized_note,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot transition obligation: {exc}") from exc
        return self.get(current.id)

    def _event(self, row: sqlite3.Row) -> ObligationEvent:
        claim = self.evidence.get_claim(str(row["evidence_claim_id"]))
        if claim is None:
            raise LineageCorruptionError("obligation event lost source evidence")
        return ObligationEvent(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            obligation_id=str(row["obligation_id"]),
            status=str(row["status"]),
            evidence_claim_id=claim.id,
            source_kind=claim.source_kind,
            source_ref=claim.source_ref,
            note=None if row["note"] is None else str(row["note"]),
        )

    def _trigger_event(self, row: sqlite3.Row) -> ObligationTriggerEvent:
        claim = self.evidence.get_claim(str(row["evidence_claim_id"]))
        if claim is None:
            raise LineageCorruptionError("obligation trigger lost source evidence")
        return ObligationTriggerEvent(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            obligation_id=str(row["obligation_id"]),
            evidence_claim_id=claim.id,
            source_kind=claim.source_kind,
            source_ref=claim.source_ref,
            note=str(row["note"]),
        )

    def get(self, obligation_id: str) -> ObligationSnapshot:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,person_id,song_id,followup_id,kind,responsibility,"
            "summary,due_on,trigger_ref,source_claim_id,consequence_note "
            "FROM business_obligations WHERE id=?",
            (str(obligation_id).strip(),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"obligation not found: {obligation_id}")
        person = self._person(str(row["person_id"]))
        song_id = None if row["song_id"] is None else str(row["song_id"])
        source = self.evidence.get_claim(str(row["source_claim_id"]))
        if source is None or not self._claim_compatible(source, song_id):
            raise LineageCorruptionError("obligation lost compatible source evidence")
        events = tuple(
            self._event(event_row)
            for event_row in self._conn.execute(
                "SELECT seq,id,obligation_id,status,evidence_claim_id,note "
                "FROM business_obligation_events WHERE obligation_id=? ORDER BY seq",
                (str(row["id"]),),
            )
        )
        triggers = tuple(
            self._trigger_event(trigger_row)
            for trigger_row in self._conn.execute(
                "SELECT seq,id,obligation_id,evidence_claim_id,note "
                "FROM business_obligation_triggers WHERE obligation_id=? ORDER BY seq",
                (str(row["id"]),),
            )
        )
        followup_id = None if row["followup_id"] is None else str(row["followup_id"])
        followup_state = None
        if followup_id is not None:
            followup = self.people.get_followup(followup_id)
            if followup is None:
                raise LineageCorruptionError("obligation lost linked follow-up")
            followup_state = followup.state
        snapshot = ObligationSnapshot(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            profile_id=self.store.profile_id,
            artist_id=str(row["artist_id"]),
            person_id=person.id,
            person_display_name=person.display_name,
            song_id=song_id,
            followup_id=followup_id,
            followup_state=followup_state,
            kind=str(row["kind"]),
            responsibility=str(row["responsibility"]),
            summary=str(row["summary"]),
            due_on=None if row["due_on"] is None else str(row["due_on"]),
            trigger_ref=None if row["trigger_ref"] is None else str(row["trigger_ref"]),
            consequence_note=(
                None if row["consequence_note"] is None else str(row["consequence_note"])
            ),
            source_claim_id=source.id,
            source_kind=source.source_kind,
            source_ref=source.source_ref,
            source_current=self._claim_current(source),
            events=events,
            trigger_events=triggers,
        )
        if snapshot.artist_id != self.store.primary_artist_id:
            raise LineageCorruptionError("obligation Artist does not match active profile")
        if not snapshot.events or snapshot.events[0].status != "OPEN":
            raise LineageCorruptionError("obligation lifecycle does not begin OPEN")
        return snapshot

    def all(self) -> tuple[ObligationSnapshot, ...]:
        ids = [
            str(row["id"])
            for row in self._conn.execute(
                "SELECT id FROM business_obligations ORDER BY seq"
            )
        ]
        return tuple(self.get(obligation_id) for obligation_id in ids)

    def for_person(self, person_id: str) -> tuple[ObligationSnapshot, ...]:
        person = self._person(person_id)
        ids = [
            str(row["id"])
            for row in self._conn.execute(
                "SELECT id FROM business_obligations WHERE person_id=? ORDER BY seq",
                (person.id,),
            )
        ]
        return tuple(self.get(obligation_id) for obligation_id in ids)

    def for_song(self, song_id: str) -> tuple[ObligationSnapshot, ...]:
        normalized = self._song(song_id)
        assert normalized is not None
        ids = [
            str(row["id"])
            for row in self._conn.execute(
                "SELECT id FROM business_obligations WHERE song_id=? ORDER BY seq",
                (normalized,),
            )
        ]
        return tuple(self.get(obligation_id) for obligation_id in ids)

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("obligation_schema_version") != str(
                OBLIGATION_SCHEMA_VERSION
            ):
                raise LineageCorruptionError("unsupported Obligation schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'obligation_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"Obligation integrity hooks are incomplete: {sorted(missing)}"
                )
            for row in self._conn.execute(
                "SELECT id FROM business_obligations ORDER BY seq"
            ):
                snapshot = self.get(str(row["id"]))
                if snapshot.kind not in OBLIGATION_KINDS:
                    raise LineageCorruptionError("obligation kind is invalid")
                if snapshot.responsibility not in FOLLOWUP_RESPONSIBILITIES:
                    raise LineageCorruptionError("obligation responsibility is invalid")
                if snapshot.due_on is not None:
                    self._due_on(snapshot.due_on)
                previous = None
                for event in snapshot.events:
                    if event.status not in OBLIGATION_STATUSES:
                        raise LineageCorruptionError("obligation lifecycle status is invalid")
                    claim = self.evidence.get_claim(event.evidence_claim_id)
                    if claim is None or not self._claim_compatible(claim, snapshot.song_id):
                        raise LineageCorruptionError(
                            "obligation lifecycle evidence binding is invalid"
                        )
                    if previous in TERMINAL_OBLIGATION_STATUSES:
                        raise LineageCorruptionError(
                            "terminal obligation has later lifecycle evidence"
                        )
                    if previous == event.status:
                        raise LineageCorruptionError(
                            "obligation lifecycle repeats the same status"
                        )
                    if event.status == "OPEN" and previous not in (None, "BLOCKED", "DISPUTED"):
                        raise LineageCorruptionError("obligation OPEN transition is invalid")
                    previous = event.status
                for trigger in snapshot.trigger_events:
                    if snapshot.trigger_ref is None:
                        raise LineageCorruptionError(
                            "obligation has trigger evidence without a trigger"
                        )
                    claim = self.evidence.get_claim(trigger.evidence_claim_id)
                    if claim is None or not self._claim_compatible(claim, snapshot.song_id):
                        raise LineageCorruptionError(
                            "obligation trigger evidence binding is invalid"
                        )
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValidationError, ValueError, TypeError) as exc:
            raise LineageCorruptionError("Obligation memory is unreadable or corrupt") from exc
