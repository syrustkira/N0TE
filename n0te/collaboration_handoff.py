from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .people import PeopleMemory

HANDOFF_SCHEMA_VERSION = 1
ACCESS_ROLES = ("VIEW", "REVIEW", "CONTRIBUTE")
PACKAGE_STATES = ("PREPARED", "REVOKED")
FEEDBACK_STATES = ("OPEN", "ADDRESSED", "DISMISSED")
MAX_FEEDBACK_POSITION_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class HandoffPackage:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    version_id: str
    person_id: str
    access_role: str
    label: str | None
    state: str
    revocation_reason: str | None
    asset_ids: tuple[str, ...]
    external_share_executed: bool = field(default=False, init=False)
    provider_access_granted: bool = field(default=False, init=False)
    external_revocation_verified: bool = field(default=False, init=False)

    @property
    def locally_available(self) -> bool:
        return self.state == "PREPARED"


@dataclass(frozen=True)
class CollaboratorFeedback:
    sequence: int
    id: str
    package_id: str
    artist_id: str
    song_id: str
    version_id: str
    person_id: str
    position_ms: int | None
    body: str
    state: str
    response_version_id: str | None
    resolution_note: str | None
    attribution_verified: bool = field(default=False, init=False)
    artist_preference_promoted: bool = field(default=False, init=False)
    external_message_received_verified: bool = field(default=False, init=False)


class CollaborationHandoffMemory:
    """Local collaborator handoff and feedback continuity over canonical lineage.

    This service owns only the local handoff contract. It reuses canonical Person,
    Song, Version and Asset identity and persists inside the existing LineageStore.
    Preparing a package never uploads bytes, creates a provider share, verifies a
    collaborator identity, sends a message or grants external authority. Feedback is
    an attributed local record, not Artist preference, artistic decision or verified
    observation.
    """

    _TRIGGER_NAMES = {
        "collab_package_person_same_artist",
        "collab_package_version_same_song",
        "collab_package_version_has_assets",
        "collab_package_binding_immutable",
        "collab_package_revoked_immutable",
        "collab_package_delete_immutable",
        "collab_package_revoke_shape",
        "collab_feedback_package_binding",
        "collab_feedback_binding_immutable",
        "collab_feedback_resolved_immutable",
        "collab_feedback_delete_immutable",
        "collab_feedback_resolution_shape",
        "collab_feedback_addressed_version_shape",
        "collab_package_prepared_activity",
        "collab_package_revoked_activity",
        "collab_feedback_recorded_activity",
        "collab_feedback_resolved_activity",
    }
    _INDEX_NAMES = {
        "collab_one_prepared_package_per_person_version",
        "collab_packages_by_song",
        "collab_feedback_by_package",
    }

    def __init__(self, store: LineageStore, people: PeopleMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("CollaborationHandoffMemory requires canonical LineageStore")
        if not isinstance(people, PeopleMemory):
            raise TypeError("CollaborationHandoffMemory requires canonical PeopleMemory")
        if people.store is not store:
            raise ValueError("CollaborationHandoffMemory and PeopleMemory must share one canonical store")
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
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _required_text(value: str, field_name: str, *, maximum: int) -> str:
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
        cls, value: str | None, field_name: str, *, maximum: int
    ) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be text")
        text = " ".join(value.split())
        if not text:
            return None
        if len(text) > maximum:
            raise ValidationError(f"{field_name} is too long")
        return text

    @staticmethod
    def _required_id(value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be text")
        value = value.strip()
        if not value:
            raise ValidationError(f"{field_name} must not be empty")
        if len(value) > 512:
            raise ValidationError(f"{field_name} is too long")
        return value

    @staticmethod
    def _normalize_access_role(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("access_role must be text")
        role = value.strip().upper().replace("-", "_").replace(" ", "_")
        if role not in ACCESS_ROLES:
            raise ValidationError(f"unsupported handoff access role: {role}")
        return role

    @staticmethod
    def _normalize_position_ms(value: int | None) -> int | None:
        if value is None:
            return None
        if type(value) is not int:
            raise ValidationError("position_ms must be an integer")
        if value < 0 or value > MAX_FEEDBACK_POSITION_MS:
            raise ValidationError(
                f"position_ms must be between 0 and {MAX_FEEDBACK_POSITION_MS}"
            )
        return value

    @staticmethod
    def _normalize_feedback_outcome(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("feedback outcome must be text")
        outcome = value.strip().upper()
        if outcome not in {"ADDRESSED", "DISMISSED"}:
            raise ValidationError(f"unsupported feedback outcome: {outcome}")
        return outcome

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER collab_package_person_same_artist
            BEFORE INSERT ON collab_handoff_packages
            WHEN NOT EXISTS (
                SELECT 1 FROM people_people p
                WHERE p.id=NEW.person_id AND p.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff person belongs to a different Artist');
            END""",
            """CREATE TRIGGER collab_package_version_same_song
            BEFORE INSERT ON collab_handoff_packages
            WHEN NOT EXISTS (
                SELECT 1 FROM versions v JOIN songs s ON s.id=v.song_id
                WHERE v.id=NEW.version_id
                  AND v.song_id=NEW.song_id
                  AND s.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff Version does not belong to the exact Artist/Song');
            END""",
            """CREATE TRIGGER collab_package_version_has_assets
            BEFORE INSERT ON collab_handoff_packages
            WHEN NOT EXISTS (
                SELECT 1 FROM version_assets va WHERE va.version_id=NEW.version_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff Version has no canonical assets');
            END""",
            """CREATE TRIGGER collab_package_binding_immutable
            BEFORE UPDATE ON collab_handoff_packages
            WHEN NEW.id<>OLD.id
              OR NEW.artist_id<>OLD.artist_id
              OR NEW.song_id<>OLD.song_id
              OR NEW.version_id<>OLD.version_id
              OR NEW.person_id<>OLD.person_id
              OR NEW.access_role<>OLD.access_role
              OR NEW.label IS NOT OLD.label
            BEGIN
                SELECT RAISE(ABORT, 'handoff identity and package binding are immutable');
            END""",
            """CREATE TRIGGER collab_package_revoked_immutable
            BEFORE UPDATE ON collab_handoff_packages
            WHEN OLD.state='REVOKED'
            BEGIN
                SELECT RAISE(ABORT, 'revoked handoff package is immutable');
            END""",
            """CREATE TRIGGER collab_package_delete_immutable
            BEFORE DELETE ON collab_handoff_packages
            BEGIN
                SELECT RAISE(ABORT, 'handoff package history is immutable');
            END""",
            """CREATE TRIGGER collab_package_revoke_shape
            BEFORE UPDATE ON collab_handoff_packages
            WHEN NOT (
                OLD.state='PREPARED'
                AND NEW.state='REVOKED'
                AND NEW.revocation_reason IS NOT NULL
                AND length(trim(NEW.revocation_reason))>0
            )
            BEGIN
                SELECT RAISE(ABORT, 'handoff package may only transition PREPARED to REVOKED');
            END""",
            """CREATE TRIGGER collab_feedback_package_binding
            BEFORE INSERT ON collab_handoff_feedback
            WHEN NOT EXISTS (
                SELECT 1 FROM collab_handoff_packages p
                WHERE p.id=NEW.package_id
                  AND p.artist_id=NEW.artist_id
                  AND p.song_id=NEW.song_id
                  AND p.version_id=NEW.version_id
                  AND p.person_id=NEW.person_id
                  AND p.state='PREPARED'
            )
            BEGIN
                SELECT RAISE(ABORT, 'feedback must bind the exact prepared handoff package');
            END""",
            """CREATE TRIGGER collab_feedback_binding_immutable
            BEFORE UPDATE ON collab_handoff_feedback
            WHEN NEW.id<>OLD.id
              OR NEW.package_id<>OLD.package_id
              OR NEW.artist_id<>OLD.artist_id
              OR NEW.song_id<>OLD.song_id
              OR NEW.version_id<>OLD.version_id
              OR NEW.person_id<>OLD.person_id
              OR NEW.position_ms IS NOT OLD.position_ms
              OR NEW.body<>OLD.body
            BEGIN
                SELECT RAISE(ABORT, 'feedback attribution and source binding are immutable');
            END""",
            """CREATE TRIGGER collab_feedback_resolved_immutable
            BEFORE UPDATE ON collab_handoff_feedback
            WHEN OLD.state<>'OPEN'
            BEGIN
                SELECT RAISE(ABORT, 'resolved collaborator feedback is immutable');
            END""",
            """CREATE TRIGGER collab_feedback_delete_immutable
            BEFORE DELETE ON collab_handoff_feedback
            BEGIN
                SELECT RAISE(ABORT, 'collaborator feedback history is immutable');
            END""",
            """CREATE TRIGGER collab_feedback_resolution_shape
            BEFORE UPDATE ON collab_handoff_feedback
            WHEN NOT (
                OLD.state='OPEN'
                AND NEW.state IN ('ADDRESSED','DISMISSED')
                AND NEW.resolution_note IS NOT NULL
                AND length(trim(NEW.resolution_note))>0
                AND (
                    (NEW.state='ADDRESSED' AND NEW.response_version_id IS NOT NULL)
                    OR (NEW.state='DISMISSED' AND NEW.response_version_id IS NULL)
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'feedback may only resolve once with an exact outcome shape');
            END""",
            """CREATE TRIGGER collab_feedback_addressed_version_shape
            BEFORE UPDATE ON collab_handoff_feedback
            WHEN NEW.state='ADDRESSED' AND NOT EXISTS (
                SELECT 1
                FROM versions response
                JOIN versions source ON source.id=OLD.version_id
                WHERE response.id=NEW.response_version_id
                  AND response.song_id=OLD.song_id
                  AND response.ordinal>source.ordinal
            )
            BEGIN
                SELECT RAISE(ABORT, 'addressed feedback requires a later Version of the exact same Song');
            END""",
            """CREATE TRIGGER collab_package_prepared_activity
            AFTER INSERT ON collab_handoff_packages
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COLLAB_HANDOFF_PREPARED',NEW.artist_id,NEW.song_id,NEW.version_id,
                    'COLLAB_HANDOFF',NEW.id,
                    '{\"access_role\":\"'||NEW.access_role||'\"}'
                );
            END""",
            """CREATE TRIGGER collab_package_revoked_activity
            AFTER UPDATE OF state ON collab_handoff_packages
            WHEN OLD.state='PREPARED' AND NEW.state='REVOKED'
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COLLAB_HANDOFF_REVOKED',NEW.artist_id,NEW.song_id,NEW.version_id,
                    'COLLAB_HANDOFF',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER collab_feedback_recorded_activity
            AFTER INSERT ON collab_handoff_feedback
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COLLAB_FEEDBACK_RECORDED',NEW.artist_id,NEW.song_id,NEW.version_id,
                    'COLLAB_FEEDBACK',NEW.id,'{}'
                );
            END""",
            """CREATE TRIGGER collab_feedback_resolved_activity
            AFTER UPDATE OF state ON collab_handoff_feedback
            WHEN OLD.state='OPEN' AND NEW.state IN ('ADDRESSED','DISMISSED')
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'COLLAB_FEEDBACK_'||NEW.state,NEW.artist_id,NEW.song_id,
                    CASE WHEN NEW.response_version_id IS NULL
                         THEN NEW.version_id ELSE NEW.response_version_id END,
                    'COLLAB_FEEDBACK',NEW.id,'{}'
                );
            END""",
        )

    def _ensure_schema(self) -> None:
        packages_exist = self._table_exists("collab_handoff_packages")
        feedback_exist = self._table_exists("collab_handoff_feedback")
        version = self._metadata_value("collaboration_handoff_schema_version")
        any_existing = packages_exist or feedback_exist or version is not None
        if any_existing:
            if (
                not packages_exist
                or not feedback_exist
                or version != str(HANDOFF_SCHEMA_VERSION)
            ):
                raise LineageCorruptionError(
                    "collaboration handoff schema metadata/table mismatch"
                )
            return
        if not self._table_exists("people_people"):
            raise LineageCorruptionError(
                "CollaborationHandoffMemory requires canonical People identity first"
            )
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "CollaborationHandoffMemory requires canonical Activity chronology first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE collab_handoff_packages (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        version_id TEXT NOT NULL REFERENCES versions(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id),
                        access_role TEXT NOT NULL CHECK(access_role IN (
                            'VIEW','REVIEW','CONTRIBUTE'
                        )),
                        label TEXT NULL CHECK(
                            label IS NULL OR length(trim(label))>0
                        ),
                        state TEXT NOT NULL DEFAULT 'PREPARED'
                            CHECK(state IN ('PREPARED','REVOKED')),
                        revocation_reason TEXT NULL CHECK(
                            revocation_reason IS NULL
                            OR length(trim(revocation_reason))>0
                        )
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE collab_handoff_feedback (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        package_id TEXT NOT NULL REFERENCES collab_handoff_packages(id),
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        version_id TEXT NOT NULL REFERENCES versions(id),
                        person_id TEXT NOT NULL REFERENCES people_people(id),
                        position_ms INTEGER NULL CHECK(
                            position_ms IS NULL OR (
                                position_ms>=0 AND position_ms<=86400000
                            )
                        ),
                        body TEXT NOT NULL CHECK(length(trim(body))>0),
                        state TEXT NOT NULL DEFAULT 'OPEN'
                            CHECK(state IN ('OPEN','ADDRESSED','DISMISSED')),
                        response_version_id TEXT NULL REFERENCES versions(id),
                        resolution_note TEXT NULL CHECK(
                            resolution_note IS NULL
                            OR length(trim(resolution_note))>0
                        )
                    )"""
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX collab_one_prepared_package_per_person_version "
                    "ON collab_handoff_packages(song_id,version_id,person_id) "
                    "WHERE state='PREPARED'"
                )
                self._conn.execute(
                    "CREATE INDEX collab_packages_by_song "
                    "ON collab_handoff_packages(song_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX collab_feedback_by_package "
                    "ON collab_handoff_feedback(package_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    (
                        "collaboration_handoff_schema_version",
                        str(HANDOFF_SCHEMA_VERSION),
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize collaborator handoff memory"
            ) from exc

    @staticmethod
    def _package(row: sqlite3.Row, asset_ids: tuple[str, ...]) -> HandoffPackage:
        return HandoffPackage(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            version_id=str(row["version_id"]),
            person_id=str(row["person_id"]),
            access_role=str(row["access_role"]),
            label=None if row["label"] is None else str(row["label"]),
            state=str(row["state"]),
            revocation_reason=(
                None
                if row["revocation_reason"] is None
                else str(row["revocation_reason"])
            ),
            asset_ids=asset_ids,
        )

    @staticmethod
    def _feedback(row: sqlite3.Row) -> CollaboratorFeedback:
        return CollaboratorFeedback(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            package_id=str(row["package_id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            version_id=str(row["version_id"]),
            person_id=str(row["person_id"]),
            position_ms=(
                None if row["position_ms"] is None else int(row["position_ms"])
            ),
            body=str(row["body"]),
            state=str(row["state"]),
            response_version_id=(
                None
                if row["response_version_id"] is None
                else str(row["response_version_id"])
            ),
            resolution_note=(
                None
                if row["resolution_note"] is None
                else str(row["resolution_note"])
            ),
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("collaboration_handoff_schema_version") != str(
                HANDOFF_SCHEMA_VERSION
            ):
                raise LineageCorruptionError(
                    "unsupported collaborator handoff schema version"
                )
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'collab_%'"
                )
            }
            missing_triggers = self._TRIGGER_NAMES - trigger_names
            if missing_triggers:
                raise LineageCorruptionError(
                    "collaborator handoff integrity hooks are incomplete: "
                    f"{sorted(missing_triggers)}"
                )
            index_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'collab_%'"
                )
            }
            missing_indexes = self._INDEX_NAMES - index_names
            if missing_indexes:
                raise LineageCorruptionError(
                    "collaborator handoff indexes are incomplete: "
                    f"{sorted(missing_indexes)}"
                )

            active_seen: set[tuple[str, str, str]] = set()
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,version_id,person_id,access_role,"
                "label,state,revocation_reason FROM collab_handoff_packages ORDER BY seq"
            ):
                package = self._package(
                    row, self.store.version_asset_ids(str(row["version_id"]))
                )
                if package.artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError(
                        "handoff package Artist does not match active profile"
                    )
                song = self.store.get_song(package.song_id)
                version = self.store.get_version(package.version_id)
                person = self.people.get_person(package.person_id)
                if song is None or song.artist_id != package.artist_id:
                    raise LineageCorruptionError(
                        "handoff package is bound to invalid Song"
                    )
                if version is None or version.song_id != package.song_id:
                    raise LineageCorruptionError(
                        "handoff package is bound to invalid Version"
                    )
                if person is None or person.artist_id != package.artist_id:
                    raise LineageCorruptionError(
                        "handoff package is bound to invalid Person"
                    )
                if not package.asset_ids:
                    raise LineageCorruptionError(
                        "handoff package Version no longer has canonical assets"
                    )
                if package.access_role not in ACCESS_ROLES:
                    raise LineageCorruptionError(
                        "handoff package access role is invalid"
                    )
                self._optional_text(package.label, "label", maximum=240)
                if package.state not in PACKAGE_STATES:
                    raise LineageCorruptionError("handoff package state is invalid")
                if package.state == "PREPARED":
                    if package.revocation_reason is not None:
                        raise LineageCorruptionError(
                            "prepared handoff package unexpectedly has revocation reason"
                        )
                    key = (package.song_id, package.version_id, package.person_id)
                    if key in active_seen:
                        raise LineageCorruptionError(
                            "multiple prepared handoff packages exist for the same Person/Version"
                        )
                    active_seen.add(key)
                elif package.revocation_reason is None:
                    raise LineageCorruptionError(
                        "revoked handoff package is missing reason"
                    )

            for row in self._conn.execute(
                "SELECT seq,id,package_id,artist_id,song_id,version_id,person_id,"
                "position_ms,body,state,response_version_id,resolution_note "
                "FROM collab_handoff_feedback ORDER BY seq"
            ):
                feedback = self._feedback(row)
                package = self.get_package(feedback.package_id)
                if package is None:
                    raise LineageCorruptionError(
                        "collaborator feedback references missing package"
                    )
                if (
                    feedback.artist_id != package.artist_id
                    or feedback.song_id != package.song_id
                    or feedback.version_id != package.version_id
                    or feedback.person_id != package.person_id
                ):
                    raise LineageCorruptionError(
                        "collaborator feedback no longer matches its exact package binding"
                    )
                self._normalize_position_ms(feedback.position_ms)
                self._required_text(feedback.body, "feedback body", maximum=8000)
                if feedback.state not in FEEDBACK_STATES:
                    raise LineageCorruptionError("collaborator feedback state is invalid")
                if feedback.state == "OPEN":
                    if (
                        feedback.response_version_id is not None
                        or feedback.resolution_note is not None
                    ):
                        raise LineageCorruptionError(
                            "open collaborator feedback contains resolution data"
                        )
                else:
                    if feedback.resolution_note is None:
                        raise LineageCorruptionError(
                            "resolved collaborator feedback is missing resolution note"
                        )
                    self._required_text(
                        feedback.resolution_note,
                        "resolution_note",
                        maximum=2000,
                    )
                    if feedback.state == "DISMISSED":
                        if feedback.response_version_id is not None:
                            raise LineageCorruptionError(
                                "dismissed collaborator feedback cannot claim a response Version"
                            )
                    else:
                        if feedback.response_version_id is None:
                            raise LineageCorruptionError(
                                "addressed collaborator feedback is missing response Version"
                            )
                        response = self.store.get_version(feedback.response_version_id)
                        source = self.store.get_version(feedback.version_id)
                        if (
                            response is None
                            or source is None
                            or response.song_id != feedback.song_id
                            or response.ordinal <= source.ordinal
                        ):
                            raise LineageCorruptionError(
                                "addressed collaborator feedback has invalid response Version"
                            )
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError, ValidationError) as exc:
            raise LineageCorruptionError(
                "collaborator handoff memory is unreadable or corrupt"
            ) from exc

    def get_package(self, package_id: str) -> HandoffPackage | None:
        package_id = self._required_id(package_id, "package_id")
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,version_id,person_id,access_role,"
            "label,state,revocation_reason FROM collab_handoff_packages WHERE id=?",
            (package_id,),
        ).fetchone()
        if row is None:
            return None
        return self._package(
            row,
            self.store.version_asset_ids(str(row["version_id"])),
        )

    def packages_for_song(self, song_id: str) -> tuple[HandoffPackage, ...]:
        song_id = self._required_id(song_id, "song_id")
        self.store._require_song(song_id)
        rows = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,version_id,person_id,access_role,"
            "label,state,revocation_reason FROM collab_handoff_packages "
            "WHERE song_id=? ORDER BY seq",
            (song_id,),
        ).fetchall()
        return tuple(
            self._package(row, self.store.version_asset_ids(str(row["version_id"])))
            for row in rows
        )

    def packages_for_person(self, person_id: str) -> tuple[HandoffPackage, ...]:
        person_id = self._required_id(person_id, "person_id")
        person = self.people.get_person(person_id)
        if person is None or person.artist_id != self.store.primary_artist_id:
            raise NotFoundError(f"Person not found in active Artist profile: {person_id}")
        rows = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,version_id,person_id,access_role,"
            "label,state,revocation_reason FROM collab_handoff_packages "
            "WHERE person_id=? ORDER BY seq",
            (person_id,),
        ).fetchall()
        return tuple(
            self._package(row, self.store.version_asset_ids(str(row["version_id"])))
            for row in rows
        )

    def prepare_package(
        self,
        *,
        song_id: str,
        version_id: str,
        person_id: str,
        access_role: str,
        label: str | None = None,
    ) -> HandoffPackage:
        song_id = self._required_id(song_id, "song_id")
        version_id = self._required_id(version_id, "version_id")
        person_id = self._required_id(person_id, "person_id")
        role = self._normalize_access_role(access_role)
        package_label = self._optional_text(label, "label", maximum=240)

        song = self.store._require_song(song_id)
        version = self.store.get_version(version_id)
        if version is None:
            raise NotFoundError(f"Version not found in active profile: {version_id}")
        if version.song_id != song.id:
            raise ValidationError("handoff Version belongs to a different Song")
        asset_ids = self.store.version_asset_ids(version.id)
        if not asset_ids:
            raise ValidationError(
                "handoff package requires a Version with at least one canonical Asset"
            )
        person = self.people.get_person(person_id)
        if person is None or person.artist_id != self.store.primary_artist_id:
            raise NotFoundError(f"Person not found in active Artist profile: {person_id}")

        package_id = f"handoff_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO collab_handoff_packages("
                    "id,artist_id,song_id,version_id,person_id,access_role,label,"
                    "state,revocation_reason) VALUES(?,?,?,?,?,?,?,'PREPARED',NULL)",
                    (
                        package_id,
                        self.store.primary_artist_id,
                        song.id,
                        version.id,
                        person.id,
                        role,
                        package_label,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                "cannot prepare collaborator handoff package; revoke the existing "
                "prepared package for this Person/Version before replacing it"
            ) from exc
        package = self.get_package(package_id)
        if package is None:
            raise LineageCorruptionError(
                "new collaborator handoff package disappeared after creation"
            )
        return package

    def revoke_package(self, package_id: str, *, reason: str) -> HandoffPackage:
        package_id = self._required_id(package_id, "package_id")
        revocation_reason = self._required_text(reason, "reason", maximum=2000)
        current = self.get_package(package_id)
        if current is None:
            raise NotFoundError(f"handoff package not found: {package_id}")
        if current.state == "REVOKED":
            if current.revocation_reason == revocation_reason:
                return current
            raise ValidationError(
                "handoff package was already revoked with a different durable reason"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    "UPDATE collab_handoff_packages "
                    "SET state='REVOKED',revocation_reason=? WHERE id=?",
                    (revocation_reason, current.id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError("cannot revoke collaborator handoff package") from exc
        revoked = self.get_package(current.id)
        if revoked is None or revoked.state != "REVOKED":
            raise LineageCorruptionError(
                "collaborator handoff package did not preserve revocation"
            )
        return revoked

    def record_feedback(
        self,
        package_id: str,
        *,
        body: str,
        position_ms: int | None = None,
    ) -> CollaboratorFeedback:
        package_id = self._required_id(package_id, "package_id")
        text = self._required_text(body, "feedback body", maximum=8000)
        position = self._normalize_position_ms(position_ms)
        package = self.get_package(package_id)
        if package is None:
            raise NotFoundError(f"handoff package not found: {package_id}")
        if package.state != "PREPARED":
            raise ValidationError(
                "new collaborator feedback cannot be recorded against a revoked local handoff package"
            )

        feedback_id = f"feedback_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO collab_handoff_feedback("
                    "id,package_id,artist_id,song_id,version_id,person_id,position_ms,"
                    "body,state,response_version_id,resolution_note) "
                    "VALUES(?,?,?,?,?,?,?,?,'OPEN',NULL,NULL)",
                    (
                        feedback_id,
                        package.id,
                        package.artist_id,
                        package.song_id,
                        package.version_id,
                        package.person_id,
                        position,
                        text,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(
                "cannot record collaborator feedback against this handoff package"
            ) from exc
        feedback = self.get_feedback(feedback_id)
        if feedback is None:
            raise LineageCorruptionError(
                "new collaborator feedback disappeared after creation"
            )
        return feedback

    def get_feedback(self, feedback_id: str) -> CollaboratorFeedback | None:
        feedback_id = self._required_id(feedback_id, "feedback_id")
        row = self._conn.execute(
            "SELECT seq,id,package_id,artist_id,song_id,version_id,person_id,"
            "position_ms,body,state,response_version_id,resolution_note "
            "FROM collab_handoff_feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
        return None if row is None else self._feedback(row)

    def feedback_for_package(
        self, package_id: str
    ) -> tuple[CollaboratorFeedback, ...]:
        package_id = self._required_id(package_id, "package_id")
        if self.get_package(package_id) is None:
            raise NotFoundError(f"handoff package not found: {package_id}")
        rows = self._conn.execute(
            "SELECT seq,id,package_id,artist_id,song_id,version_id,person_id,"
            "position_ms,body,state,response_version_id,resolution_note "
            "FROM collab_handoff_feedback WHERE package_id=? ORDER BY seq",
            (package_id,),
        ).fetchall()
        return tuple(self._feedback(row) for row in rows)

    def resolve_feedback(
        self,
        feedback_id: str,
        *,
        outcome: str,
        note: str,
        response_version_id: str | None = None,
    ) -> CollaboratorFeedback:
        feedback_id = self._required_id(feedback_id, "feedback_id")
        normalized_outcome = self._normalize_feedback_outcome(outcome)
        resolution_note = self._required_text(note, "resolution_note", maximum=2000)
        current = self.get_feedback(feedback_id)
        if current is None:
            raise NotFoundError(f"collaborator feedback not found: {feedback_id}")

        response_id: str | None = None
        if normalized_outcome == "ADDRESSED":
            if response_version_id is None:
                raise ValidationError(
                    "addressed collaborator feedback requires the exact later response Version"
                )
            response_id = self._required_id(
                response_version_id, "response_version_id"
            )
            response = self.store.get_version(response_id)
            source = self.store.get_version(current.version_id)
            if response is None:
                raise NotFoundError(
                    f"response Version not found in active profile: {response_id}"
                )
            if source is None:
                raise LineageCorruptionError(
                    "collaborator feedback source Version disappeared"
                )
            if response.song_id != current.song_id:
                raise ValidationError(
                    "response Version belongs to a different Song"
                )
            if response.ordinal <= source.ordinal:
                raise ValidationError(
                    "addressed feedback requires a Version later than the reviewed Version"
                )
        elif response_version_id is not None:
            raise ValidationError(
                "dismissed collaborator feedback cannot claim a response Version"
            )

        if current.state != "OPEN":
            if (
                current.state == normalized_outcome
                and current.resolution_note == resolution_note
                and current.response_version_id == response_id
            ):
                return current
            raise ValidationError(
                "collaborator feedback was already resolved with a different durable outcome"
            )

        try:
            with self.store._tx():
                self._conn.execute(
                    "UPDATE collab_handoff_feedback "
                    "SET state=?,response_version_id=?,resolution_note=? WHERE id=?",
                    (
                        normalized_outcome,
                        response_id,
                        resolution_note,
                        current.id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError("cannot resolve collaborator feedback") from exc
        resolved = self.get_feedback(current.id)
        if resolved is None or resolved.state != normalized_outcome:
            raise LineageCorruptionError(
                "collaborator feedback resolution did not persist clearly"
            )
        return resolved
