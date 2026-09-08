from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .people import PeopleMemory

PERSON_IDENTITY_SCHEMA_VERSION = 1

IDENTITY_REALMS = {
    "CONTACTS",
    "GMAIL",
    "SOCIAL",
    "CREDITS",
    "COLLABORATOR",
    "OPPORTUNITY",
    "OTHER",
}
IDENTITY_SOURCE_KINDS = {"USER_DECLARED", "OBSERVED"}
REVIEW_STATES = {"REVIEW_REQUIRED", "LINKED", "REJECTED", "SPLIT"}
ACTIVE_REVIEW_STATES = {"REVIEW_REQUIRED", "LINKED"}


@dataclass(frozen=True)
class ExternalIdentity:
    sequence: int
    id: str
    artist_id: str
    realm: str
    namespace: str
    subject: str
    display_label: str | None
    source_kind: str
    source_ref: str | None
    provider_verified: bool = field(default=False, init=False)
    canonical_person_proven: bool = field(default=False, init=False)
    external_action_authorized: bool = field(default=False, init=False)
    authority_effect: str = field(default="UNCHANGED", init=False)


@dataclass(frozen=True)
class IdentityReviewEvent:
    sequence: int
    id: str
    review_id: str
    state: str
    note: str | None


@dataclass(frozen=True)
class IdentityResolution:
    review_id: str
    external_identity: ExternalIdentity
    person_id: str
    state: str
    note: str | None
    event_count: int
    provider_verified: bool = field(default=False, init=False)
    destructive_person_merge: bool = field(default=False, init=False)
    external_action_authorized: bool = field(default=False, init=False)
    authority_effect: str = field(default="UNCHANGED", init=False)

    @property
    def local_reviewed_link(self) -> bool:
        return self.state == "LINKED"


class PersonIdentityMemory:
    """Review-first external-identity reconciliation over canonical People.

    External identities are observations or artist declarations, never canonical
    People on their own. Linking records an explicit local review decision; it
    does not mutate or merge Person rows, certify a provider identity, or grant
    any external action authority. A later split preserves the original link
    event and returns the external identity to an unresolved local state.
    """

    _TRIGGER_NAMES = {
        "person_identity_external_immutable_update",
        "person_identity_external_immutable_delete",
        "person_identity_review_same_artist",
        "person_identity_review_one_active",
        "person_identity_review_immutable_update",
        "person_identity_review_immutable_delete",
        "person_identity_event_first_state",
        "person_identity_event_transition",
        "person_identity_event_immutable_update",
        "person_identity_event_immutable_delete",
        "person_identity_external_activity",
        "person_identity_review_activity",
        "person_identity_event_activity",
    }
    _INDEX_NAMES = {
        "person_identity_external_key",
        "person_identity_review_external",
        "person_identity_event_review",
    }

    def __init__(self, store: LineageStore, people: PeopleMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("PersonIdentityMemory requires the canonical LineageStore")
        if not isinstance(people, PeopleMemory):
            raise TypeError("PersonIdentityMemory requires canonical PeopleMemory")
        if people.store is not store:
            raise ValidationError("PeopleMemory belongs to a different profile store")
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
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER person_identity_external_immutable_update
            BEFORE UPDATE ON person_external_identities
            BEGIN
                SELECT RAISE(ABORT, 'external identity history is immutable');
            END""",
            """CREATE TRIGGER person_identity_external_immutable_delete
            BEFORE DELETE ON person_external_identities
            BEGIN
                SELECT RAISE(ABORT, 'external identity history is immutable');
            END""",
            """CREATE TRIGGER person_identity_review_same_artist
            BEFORE INSERT ON person_identity_reviews
            WHEN NOT EXISTS (
                SELECT 1
                FROM person_external_identities e
                JOIN people_people p ON p.id=NEW.person_id
                WHERE e.id=NEW.external_identity_id
                  AND e.artist_id=NEW.artist_id
                  AND p.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'identity review crosses Artist boundary');
            END""",
            """CREATE TRIGGER person_identity_review_one_active
            BEFORE INSERT ON person_identity_reviews
            WHEN EXISTS (
                SELECT 1
                FROM person_identity_reviews r
                WHERE r.external_identity_id=NEW.external_identity_id
                  AND (
                    SELECT state
                    FROM person_identity_review_events e
                    WHERE e.review_id=r.id
                    ORDER BY e.seq DESC LIMIT 1
                  ) IN ('REVIEW_REQUIRED','LINKED')
            )
            BEGIN
                SELECT RAISE(ABORT, 'external identity already has an active review');
            END""",
            """CREATE TRIGGER person_identity_review_immutable_update
            BEFORE UPDATE ON person_identity_reviews
            BEGIN
                SELECT RAISE(ABORT, 'identity review binding is immutable');
            END""",
            """CREATE TRIGGER person_identity_review_immutable_delete
            BEFORE DELETE ON person_identity_reviews
            BEGIN
                SELECT RAISE(ABORT, 'identity review history is immutable');
            END""",
            """CREATE TRIGGER person_identity_event_first_state
            BEFORE INSERT ON person_identity_review_events
            WHEN NOT EXISTS (
                SELECT 1 FROM person_identity_review_events
                WHERE review_id=NEW.review_id
            ) AND NEW.state<>'REVIEW_REQUIRED'
            BEGIN
                SELECT RAISE(ABORT, 'identity review must begin as REVIEW_REQUIRED');
            END""",
            """CREATE TRIGGER person_identity_event_transition
            BEFORE INSERT ON person_identity_review_events
            WHEN EXISTS (
                SELECT 1 FROM person_identity_review_events
                WHERE review_id=NEW.review_id
            ) AND NOT (
                (
                    (SELECT state FROM person_identity_review_events
                     WHERE review_id=NEW.review_id ORDER BY seq DESC LIMIT 1)
                    ='REVIEW_REQUIRED'
                    AND NEW.state IN ('LINKED','REJECTED')
                )
                OR
                (
                    (SELECT state FROM person_identity_review_events
                     WHERE review_id=NEW.review_id ORDER BY seq DESC LIMIT 1)
                    ='LINKED'
                    AND NEW.state='SPLIT'
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid identity review transition');
            END""",
            """CREATE TRIGGER person_identity_event_immutable_update
            BEFORE UPDATE ON person_identity_review_events
            BEGIN
                SELECT RAISE(ABORT, 'identity review event history is immutable');
            END""",
            """CREATE TRIGGER person_identity_event_immutable_delete
            BEFORE DELETE ON person_identity_review_events
            BEGIN
                SELECT RAISE(ABORT, 'identity review event history is immutable');
            END""",
            """CREATE TRIGGER person_identity_external_activity
            AFTER INSERT ON person_external_identities
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'EXTERNAL_IDENTITY_RECORDED',NEW.artist_id,NULL,NULL,
                    'EXTERNAL_IDENTITY',NEW.id,
                    '{\"realm\":\"'||NEW.realm||'\",\"source_kind\":\"'||NEW.source_kind||'\"}'
                );
            END""",
            """CREATE TRIGGER person_identity_review_activity
            AFTER INSERT ON person_identity_reviews
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'IDENTITY_REVIEW_OPENED',NEW.artist_id,NULL,NULL,
                    'IDENTITY_REVIEW',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER person_identity_event_activity
            AFTER INSERT ON person_identity_review_events
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'IDENTITY_REVIEW_'||NEW.state,
                    r.artist_id,NULL,NULL,
                    'IDENTITY_REVIEW',NEW.review_id,'{}'
                FROM person_identity_reviews r WHERE r.id=NEW.review_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        external = self._table_exists("person_external_identities")
        reviews = self._table_exists("person_identity_reviews")
        events = self._table_exists("person_identity_review_events")
        version = self._metadata_value("person_identity_schema_version")
        any_existing = external or reviews or events or version is not None

        if any_existing:
            if (
                not external
                or not reviews
                or not events
                or version != str(PERSON_IDENTITY_SCHEMA_VERSION)
            ):
                raise LineageCorruptionError(
                    "Person identity schema metadata/table mismatch"
                )
            return

        if not self._table_exists("people_people"):
            raise LineageCorruptionError(
                "PersonIdentityMemory requires canonical PeopleMemory first"
            )
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "PersonIdentityMemory requires canonical Activity chronology first"
            )

        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE person_external_identities (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        realm TEXT NOT NULL CHECK(realm IN (
                            'CONTACTS','GMAIL','SOCIAL','CREDITS',
                            'COLLABORATOR','OPPORTUNITY','OTHER'
                        )),
                        namespace TEXT NOT NULL CHECK(length(trim(namespace))>0),
                        subject TEXT NOT NULL CHECK(length(trim(subject))>0),
                        display_label TEXT NULL CHECK(
                            display_label IS NULL OR length(trim(display_label))>0
                        ),
                        source_kind TEXT NOT NULL CHECK(
                            source_kind IN ('USER_DECLARED','OBSERVED')
                        ),
                        source_ref TEXT NULL CHECK(
                            source_ref IS NULL OR length(trim(source_ref))>0
                        ),
                        CHECK(source_kind<>'OBSERVED' OR source_ref IS NOT NULL)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE person_identity_reviews (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        external_identity_id TEXT NOT NULL
                            REFERENCES person_external_identities(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE person_identity_review_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        review_id TEXT NOT NULL REFERENCES person_identity_reviews(id),
                        state TEXT NOT NULL CHECK(state IN (
                            'REVIEW_REQUIRED','LINKED','REJECTED','SPLIT'
                        )),
                        note TEXT NULL CHECK(note IS NULL OR length(trim(note))>0)
                    )"""
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX person_identity_external_key "
                    "ON person_external_identities(artist_id,realm,namespace,subject)"
                )
                self._conn.execute(
                    "CREATE INDEX person_identity_review_external "
                    "ON person_identity_reviews(external_identity_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX person_identity_event_review "
                    "ON person_identity_review_events(review_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) "
                    "VALUES('person_identity_schema_version',?)",
                    (str(PERSON_IDENTITY_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize Person identity reconciliation"
            ) from exc

    @staticmethod
    def _strict_text(value: object, field_name: str, *, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be text")
        text = " ".join(value.split())
        if not text:
            raise ValidationError(f"{field_name} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field_name} is too long")
        return text

    @classmethod
    def _optional_text(
        cls,
        value: object | None,
        field_name: str,
        *,
        maximum: int,
    ) -> str | None:
        if value is None:
            return None
        return cls._strict_text(value, field_name, maximum=maximum)

    @classmethod
    def _normalize_realm(cls, value: object) -> str:
        realm = cls._strict_text(value, "realm", maximum=40).upper()
        if realm not in IDENTITY_REALMS:
            raise ValidationError(f"unsupported identity realm: {realm}")
        return realm

    @classmethod
    def _normalize_source_kind(cls, value: object) -> str:
        source_kind = cls._strict_text(value, "source_kind", maximum=40).upper()
        if source_kind not in IDENTITY_SOURCE_KINDS:
            raise ValidationError(
                "identity source_kind must be USER_DECLARED or OBSERVED"
            )
        return source_kind

    @staticmethod
    def _external(row: sqlite3.Row) -> ExternalIdentity:
        return ExternalIdentity(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            realm=str(row["realm"]),
            namespace=str(row["namespace"]),
            subject=str(row["subject"]),
            display_label=(
                None if row["display_label"] is None else str(row["display_label"])
            ),
            source_kind=str(row["source_kind"]),
            source_ref=None if row["source_ref"] is None else str(row["source_ref"]),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> IdentityReviewEvent:
        return IdentityReviewEvent(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            review_id=str(row["review_id"]),
            state=str(row["state"]),
            note=None if row["note"] is None else str(row["note"]),
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("person_identity_schema_version") != str(
                PERSON_IDENTITY_SCHEMA_VERSION
            ):
                raise LineageCorruptionError(
                    "unsupported Person identity schema version"
                )

            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'person_identity_%'"
                )
            }
            missing_triggers = self._TRIGGER_NAMES - trigger_names
            if missing_triggers:
                raise LineageCorruptionError(
                    "Person identity integrity hooks are incomplete: "
                    f"{sorted(missing_triggers)}"
                )

            index_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'person_identity_%'"
                )
            }
            missing_indexes = self._INDEX_NAMES - index_names
            if missing_indexes:
                raise LineageCorruptionError(
                    "Person identity indexes are incomplete: "
                    f"{sorted(missing_indexes)}"
                )

            external_ids: set[str] = set()
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,realm,namespace,subject,display_label,"
                "source_kind,source_ref FROM person_external_identities ORDER BY seq"
            ):
                identity = self._external(row)
                external_ids.add(identity.id)
                if identity.artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError(
                        "external identity Artist does not match active profile"
                    )
                if identity.realm not in IDENTITY_REALMS:
                    raise LineageCorruptionError("external identity realm is invalid")
                if identity.source_kind not in IDENTITY_SOURCE_KINDS:
                    raise LineageCorruptionError(
                        "external identity source kind is invalid"
                    )
                if (
                    identity.source_kind == "OBSERVED"
                    and identity.source_ref is None
                ):
                    raise LineageCorruptionError(
                        "observed external identity is missing source provenance"
                    )
                self._strict_text(identity.namespace, "namespace", maximum=120)
                self._strict_text(identity.subject, "subject", maximum=500)

            active_by_identity: dict[str, int] = {}
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,external_identity_id,person_id "
                "FROM person_identity_reviews ORDER BY seq"
            ):
                review_id = str(row["id"])
                artist_id = str(row["artist_id"])
                external_id = str(row["external_identity_id"])
                person_id = str(row["person_id"])
                if artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError(
                        "identity review Artist does not match active profile"
                    )
                if external_id not in external_ids:
                    raise LineageCorruptionError(
                        "identity review references missing external identity"
                    )
                identity = self.get_external_identity(external_id)
                if identity is None or identity.artist_id != artist_id:
                    raise LineageCorruptionError(
                        "identity review external identity crosses Artist boundary"
                    )
                person = self.people.get_person(person_id)
                if person is None or person.artist_id != artist_id:
                    raise LineageCorruptionError(
                        "identity review Person crosses Artist boundary"
                    )

                events = self.review_events(review_id)
                states = tuple(event.state for event in events)
                if not states or states[0] != "REVIEW_REQUIRED":
                    raise LineageCorruptionError(
                        "identity review is missing REVIEW_REQUIRED origin"
                    )
                valid_sequences = {
                    ("REVIEW_REQUIRED",),
                    ("REVIEW_REQUIRED", "LINKED"),
                    ("REVIEW_REQUIRED", "REJECTED"),
                    ("REVIEW_REQUIRED", "LINKED", "SPLIT"),
                }
                if states not in valid_sequences:
                    raise LineageCorruptionError(
                        "identity review contains an invalid transition history"
                    )
                if any(
                    event.state != "REVIEW_REQUIRED" and event.note is None
                    for event in events
                ):
                    raise LineageCorruptionError(
                        "identity review decision is missing its reason"
                    )
                if states[-1] in ACTIVE_REVIEW_STATES:
                    active_by_identity[external_id] = (
                        active_by_identity.get(external_id, 0) + 1
                    )

            if any(count > 1 for count in active_by_identity.values()):
                raise LineageCorruptionError(
                    "external identity has multiple active review paths"
                )
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValidationError, TypeError, ValueError) as exc:
            raise LineageCorruptionError(
                "Person identity reconciliation is unreadable or corrupt"
            ) from exc

    def record_external_identity(
        self,
        *,
        realm: object,
        namespace: object,
        subject: object,
        source_kind: object,
        source_ref: object | None = None,
        display_label: object | None = None,
    ) -> ExternalIdentity:
        normalized_realm = self._normalize_realm(realm)
        normalized_namespace = self._strict_text(
            namespace, "namespace", maximum=120
        ).lower()
        normalized_subject = self._strict_text(
            subject, "subject", maximum=500
        )
        normalized_source_kind = self._normalize_source_kind(source_kind)
        normalized_source_ref = self._optional_text(
            source_ref, "source_ref", maximum=1000
        )
        normalized_label = self._optional_text(
            display_label, "display_label", maximum=240
        )
        if (
            normalized_source_kind == "OBSERVED"
            and normalized_source_ref is None
        ):
            raise ValidationError("OBSERVED identity requires source_ref provenance")

        identity_id = f"extid_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO person_external_identities("
                    "id,artist_id,realm,namespace,subject,display_label,"
                    "source_kind,source_ref) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identity_id,
                        self.store.primary_artist_id,
                        normalized_realm,
                        normalized_namespace,
                        normalized_subject,
                        normalized_label,
                        normalized_source_kind,
                        normalized_source_ref,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                "external identity already exists or violates reconciliation bounds"
            ) from exc

        result = self.get_external_identity(identity_id)
        if result is None:
            raise LineageCorruptionError(
                "new external identity disappeared after recording"
            )
        return result

    def get_external_identity(self, identity_id: object) -> ExternalIdentity | None:
        identity_id = self._strict_text(identity_id, "identity_id", maximum=100)
        row = self._conn.execute(
            "SELECT seq,id,artist_id,realm,namespace,subject,display_label,"
            "source_kind,source_ref FROM person_external_identities WHERE id=?",
            (identity_id,),
        ).fetchone()
        return None if row is None else self._external(row)

    def external_identities(self) -> tuple[ExternalIdentity, ...]:
        return tuple(
            self._external(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,realm,namespace,subject,display_label,"
                "source_kind,source_ref FROM person_external_identities ORDER BY seq"
            )
        )

    def _require_external_identity(self, identity_id: object) -> ExternalIdentity:
        identity = self.get_external_identity(identity_id)
        if identity is None:
            raise NotFoundError(
                f"external identity not found in profile {self.store.profile_id}: "
                f"{identity_id}"
            )
        if identity.artist_id != self.store.primary_artist_id:
            raise ValidationError("external identity belongs to a different Artist")
        return identity

    def _require_person(self, person_id: object):
        person_id = self._strict_text(person_id, "person_id", maximum=100)
        person = self.people.get_person(person_id)
        if person is None:
            raise NotFoundError(
                f"person not found in profile {self.store.profile_id}: {person_id}"
            )
        if person.artist_id != self.store.primary_artist_id:
            raise ValidationError("Person belongs to a different Artist")
        return person

    def review_events(self, review_id: object) -> tuple[IdentityReviewEvent, ...]:
        review_id = self._strict_text(review_id, "review_id", maximum=100)
        return tuple(
            self._event(row)
            for row in self._conn.execute(
                "SELECT seq,id,review_id,state,note "
                "FROM person_identity_review_events "
                "WHERE review_id=? ORDER BY seq",
                (review_id,),
            )
        )

    def _review_row(self, review_id: object) -> sqlite3.Row | None:
        review_id = self._strict_text(review_id, "review_id", maximum=100)
        return self._conn.execute(
            "SELECT seq,id,artist_id,external_identity_id,person_id "
            "FROM person_identity_reviews WHERE id=?",
            (review_id,),
        ).fetchone()

    def _resolution_from_row(self, row: sqlite3.Row) -> IdentityResolution:
        review_id = str(row["id"])
        events = self.review_events(review_id)
        if not events:
            raise LineageCorruptionError("identity review has no state history")
        identity = self._require_external_identity(
            str(row["external_identity_id"])
        )
        return IdentityResolution(
            review_id=review_id,
            external_identity=identity,
            person_id=str(row["person_id"]),
            state=events[-1].state,
            note=events[-1].note,
            event_count=len(events),
        )

    def get_review(self, review_id: object) -> IdentityResolution | None:
        row = self._review_row(review_id)
        return None if row is None else self._resolution_from_row(row)

    def reviews_for_identity(
        self,
        identity_id: object,
    ) -> tuple[IdentityResolution, ...]:
        identity = self._require_external_identity(identity_id)
        return tuple(
            self._resolution_from_row(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,external_identity_id,person_id "
                "FROM person_identity_reviews "
                "WHERE external_identity_id=? ORDER BY seq",
                (identity.id,),
            )
        )

    def current_resolution(
        self,
        identity_id: object,
    ) -> IdentityResolution | None:
        active = tuple(
            review
            for review in self.reviews_for_identity(identity_id)
            if review.state in ACTIVE_REVIEW_STATES
        )
        if len(active) > 1:
            raise LineageCorruptionError(
                "external identity has multiple active review paths"
            )
        return None if not active else active[0]

    def propose_link(
        self,
        identity_id: object,
        person_id: object,
    ) -> IdentityResolution:
        identity = self._require_external_identity(identity_id)
        person = self._require_person(person_id)
        if self.current_resolution(identity.id) is not None:
            raise ValidationError(
                "external identity already has an unresolved or linked review"
            )

        review_id = f"idreview_{uuid.uuid4().hex}"
        event_id = f"idevent_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO person_identity_reviews("
                    "id,artist_id,external_identity_id,person_id) VALUES(?,?,?,?)",
                    (
                        review_id,
                        self.store.primary_artist_id,
                        identity.id,
                        person.id,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO person_identity_review_events("
                    "id,review_id,state,note) "
                    "VALUES(?,?,'REVIEW_REQUIRED',NULL)",
                    (event_id, review_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot open identity review: {exc}") from exc

        result = self.get_review(review_id)
        if result is None:
            raise LineageCorruptionError(
                "new identity review disappeared after creation"
            )
        return result

    def _append_decision(
        self,
        review_id: object,
        *,
        state: str,
        note: object,
    ) -> IdentityResolution:
        current = self.get_review(review_id)
        if current is None:
            raise NotFoundError(
                f"identity review not found in profile {self.store.profile_id}: "
                f"{review_id}"
            )
        normalized_note = self._strict_text(
            note, "decision_note", maximum=1200
        )
        event_id = f"idevent_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO person_identity_review_events("
                    "id,review_id,state,note) VALUES(?,?,?,?)",
                    (event_id, current.review_id, state, normalized_note),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"invalid identity review transition: {exc}") from exc

        result = self.get_review(current.review_id)
        if result is None:
            raise LineageCorruptionError(
                "identity review disappeared after decision"
            )
        return result

    def link(
        self,
        review_id: object,
        *,
        reason: object,
    ) -> IdentityResolution:
        current = self.get_review(review_id)
        if current is None:
            raise NotFoundError(
                f"identity review not found in profile {self.store.profile_id}: "
                f"{review_id}"
            )
        if current.state != "REVIEW_REQUIRED":
            raise ValidationError("only REVIEW_REQUIRED identity can be linked")
        return self._append_decision(
            current.review_id,
            state="LINKED",
            note=reason,
        )

    def reject(
        self,
        review_id: object,
        *,
        reason: object,
    ) -> IdentityResolution:
        current = self.get_review(review_id)
        if current is None:
            raise NotFoundError(
                f"identity review not found in profile {self.store.profile_id}: "
                f"{review_id}"
            )
        if current.state != "REVIEW_REQUIRED":
            raise ValidationError("only REVIEW_REQUIRED identity can be rejected")
        return self._append_decision(
            current.review_id,
            state="REJECTED",
            note=reason,
        )

    def split(
        self,
        review_id: object,
        *,
        reason: object,
    ) -> IdentityResolution:
        current = self.get_review(review_id)
        if current is None:
            raise NotFoundError(
                f"identity review not found in profile {self.store.profile_id}: "
                f"{review_id}"
            )
        if current.state != "LINKED":
            raise ValidationError("only LINKED identity can be split")
        return self._append_decision(
            current.review_id,
            state="SPLIT",
            note=reason,
        )
