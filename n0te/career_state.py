from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .lineage import LineageCorruptionError, LineageStore, ValidationError

CAREER_STATE_SCHEMA_VERSION = 1
CAREER_STATE_TRUTH_TYPE = "USER_DECLARED"
CAREER_STATES = (
    "SURVIVAL",
    "BUILDING",
    "CREATING",
    "RELEASING",
    "GROWING",
    "TOURING",
    "CLIENT_HEAVY",
    "RECOVERY",
    "EXPERIMENTING",
)
RECOMMENDATION_WEIGHTS = {"FAVOR", "PROTECT", "NORMAL", "DEFER_OPTIONAL"}
CAREER_TOPICS = {
    "ESSENTIAL_INCOME",
    "OBLIGATIONS",
    "CREATION",
    "FINISHING",
    "RELEASE",
    "AUDIENCE",
    "OPPORTUNITY",
    "LIVE",
    "LOGISTICS",
    "CLIENT_WORK",
    "RECOVERY",
    "LEARNING",
    "EXPERIMENTATION",
    "SYSTEMS",
    "PORTFOLIO",
    "OPTIONAL_EXPANSION",
    "HIGH_LOAD_WORK",
}


class StaleCareerStateError(ValidationError):
    """A caller tried to replace a Career State it did not actually review."""


@dataclass(frozen=True)
class CareerStateEntry:
    sequence: int
    id: str
    artist_id: str
    state: str
    rationale: str | None
    truth_type: str


@dataclass(frozen=True)
class CareerStateDefinition:
    state: str
    label: str
    summary: str
    favor: tuple[str, ...]
    protect: tuple[str, ...]
    defer_optional: tuple[str, ...]

    def weight_for(self, topic: str) -> str:
        normalized = str(topic).strip().upper().replace("-", "_").replace(" ", "_")
        if normalized not in CAREER_TOPICS:
            raise ValidationError(f"unsupported Career State recommendation topic: {normalized}")
        if normalized in self.favor:
            return "FAVOR"
        if normalized in self.protect:
            return "PROTECT"
        if normalized in self.defer_optional:
            return "DEFER_OPTIONAL"
        return "NORMAL"


_DEFINITIONS = {
    "SURVIVAL": CareerStateDefinition(
        "SURVIVAL",
        "Survival",
        "Keep essentials and dependable income visible while protecting enough creative continuity to avoid losing the Artist thread.",
        ("ESSENTIAL_INCOME", "OBLIGATIONS"),
        ("RECOVERY", "CREATION"),
        ("OPTIONAL_EXPANSION",),
    ),
    "BUILDING": CareerStateDefinition(
        "BUILDING",
        "Building",
        "Strengthen capability, systems and portfolio evidence before treating expansion as the main job.",
        ("LEARNING", "SYSTEMS", "PORTFOLIO"),
        ("CREATION", "RECOVERY"),
        (),
    ),
    "CREATING": CareerStateDefinition(
        "CREATING",
        "Creating",
        "Give original work, exploration and finishing momentum more room than optional expansion noise.",
        ("CREATION", "EXPERIMENTATION", "FINISHING"),
        ("RECOVERY",),
        ("OPTIONAL_EXPANSION",),
    ),
    "RELEASING": CareerStateDefinition(
        "RELEASING",
        "Releasing",
        "Favor the decisions and audience work needed to move finished work through a release cycle without abandoning creation or recovery.",
        ("FINISHING", "RELEASE", "AUDIENCE"),
        ("CREATION", "RECOVERY"),
        ("OPTIONAL_EXPANSION",),
    ),
    "GROWING": CareerStateDefinition(
        "GROWING",
        "Growing",
        "Put more attention on audience, opportunity and repeatable release momentum while keeping the creative engine and operating system intact.",
        ("AUDIENCE", "OPPORTUNITY", "RELEASE"),
        ("CREATION", "SYSTEMS"),
        (),
    ),
    "TOURING": CareerStateDefinition(
        "TOURING",
        "Touring",
        "Favor live delivery and logistics while actively protecting recovery and a minimum viable creative thread.",
        ("LIVE", "LOGISTICS", "OBLIGATIONS"),
        ("RECOVERY", "CREATION"),
        ("OPTIONAL_EXPANSION",),
    ),
    "CLIENT_HEAVY": CareerStateDefinition(
        "CLIENT_HEAVY",
        "Client-heavy",
        "Keep client commitments and dependable income prominent without silently replacing the Artist's own work.",
        ("CLIENT_WORK", "OBLIGATIONS", "ESSENTIAL_INCOME"),
        ("CREATION", "RECOVERY"),
        ("OPTIONAL_EXPANSION",),
    ),
    "RECOVERY": CareerStateDefinition(
        "RECOVERY",
        "Recovery",
        "Reduce optional load and protect recovery while retaining only the continuity that remains useful and explicitly wanted.",
        ("RECOVERY",),
        ("CREATION", "OBLIGATIONS"),
        ("HIGH_LOAD_WORK", "OPTIONAL_EXPANSION"),
    ),
    "EXPERIMENTING": CareerStateDefinition(
        "EXPERIMENTING",
        "Experimenting",
        "Favor learning, prototypes and creative exploration without pretending every experiment must become a release or career commitment.",
        ("EXPERIMENTATION", "LEARNING", "CREATION"),
        ("RECOVERY",),
        (),
    ),
}


def normalize_career_state(value: str) -> str:
    state = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if state not in _DEFINITIONS:
        raise ValidationError(f"unsupported Career State: {state}")
    return state


def career_state_definition(value: str) -> CareerStateDefinition:
    return _DEFINITIONS[normalize_career_state(value)]


def career_state_definitions() -> tuple[CareerStateDefinition, ...]:
    return tuple(_DEFINITIONS[state] for state in CAREER_STATES)


class CareerStateMemory:
    """Reviewable Artist-level career context with append-only history.

    Career State describes a broader working season. It is not identity,
    competence, diagnosis, Focus, authority, or an instruction to perform an
    external action. This increment records only explicit Artist declaration.
    """

    _TRIGGER_NAMES = {
        "career_state_entry_immutable",
        "career_state_delete_immutable",
        "career_state_activity",
    }
    _INDEX_NAMES = {"career_state_by_artist"}

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("CareerStateMemory requires the canonical LineageStore")
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
    def _optional_rationale(value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        if not text:
            return None
        if len(text) > 1000:
            raise ValidationError("Career State context is too long")
        return text

    def _ensure_schema(self) -> None:
        exists = self._table_exists("career_state_entries")
        version = self._metadata_value("career_state_schema_version")
        if exists or version is not None:
            if not exists or version != str(CAREER_STATE_SCHEMA_VERSION):
                raise LineageCorruptionError("Career State schema metadata/table mismatch")
            return
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "CareerStateMemory requires canonical Activity chronology first"
            )
        state_sql = ",".join(f"'{state}'" for state in CAREER_STATES)
        try:
            with self.store._tx():
                self._conn.execute(
                    f"""CREATE TABLE career_state_entries (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        state TEXT NOT NULL CHECK(state IN ({state_sql})),
                        rationale TEXT NULL CHECK(
                            rationale IS NULL OR length(trim(rationale))>0
                        ),
                        truth_type TEXT NOT NULL CHECK(truth_type='USER_DECLARED')
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX career_state_by_artist "
                    "ON career_state_entries(artist_id,seq)"
                )
                self._conn.execute(
                    """CREATE TRIGGER career_state_entry_immutable
                    BEFORE UPDATE ON career_state_entries
                    BEGIN
                        SELECT RAISE(ABORT, 'Career State history is immutable');
                    END"""
                )
                self._conn.execute(
                    """CREATE TRIGGER career_state_delete_immutable
                    BEFORE DELETE ON career_state_entries
                    BEGIN
                        SELECT RAISE(ABORT, 'Career State history is immutable');
                    END"""
                )
                self._conn.execute(
                    """CREATE TRIGGER career_state_activity
                    AFTER INSERT ON career_state_entries
                    BEGIN
                        INSERT INTO activity_events(
                            id,event_type,artist_id,song_id,version_id,
                            object_type,object_id,payload_json
                        ) VALUES(
                            'act_'||lower(hex(randomblob(16))),
                            'CAREER_STATE_RECORDED',NEW.artist_id,NULL,NULL,
                            'CAREER_STATE',NEW.id,
                            '{\"state\":\"'||NEW.state||'\"}'
                        );
                    END"""
                )
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('career_state_schema_version',?)",
                    (str(CAREER_STATE_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Career State memory") from exc

    @staticmethod
    def _entry(row: sqlite3.Row) -> CareerStateEntry:
        return CareerStateEntry(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            state=str(row["state"]),
            rationale=None if row["rationale"] is None else str(row["rationale"]),
            truth_type=str(row["truth_type"]),
        )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("career_state_schema_version") != str(
                CAREER_STATE_SCHEMA_VERSION
            ):
                raise LineageCorruptionError("unsupported Career State schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'career_state_%'"
                )
            }
            missing_triggers = self._TRIGGER_NAMES - trigger_names
            if missing_triggers:
                raise LineageCorruptionError(
                    f"Career State integrity hooks are incomplete: {sorted(missing_triggers)}"
                )
            index_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'career_state_%'"
                )
            }
            missing_indexes = self._INDEX_NAMES - index_names
            if missing_indexes:
                raise LineageCorruptionError(
                    f"Career State indexes are incomplete: {sorted(missing_indexes)}"
                )
            for entry in self.history():
                if entry.artist_id != self.store.primary_artist_id:
                    raise LineageCorruptionError(
                        "Career State entry belongs to a different Artist"
                    )
                if entry.state not in _DEFINITIONS:
                    raise LineageCorruptionError("Career State entry contains invalid state")
                if entry.truth_type != CAREER_STATE_TRUTH_TYPE:
                    raise LineageCorruptionError("Career State truth type is invalid")
                if entry.rationale is not None and not entry.rationale.strip():
                    raise LineageCorruptionError("Career State context is blank")
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise LineageCorruptionError("Career State memory is unreadable or corrupt") from exc

    def history(self) -> tuple[CareerStateEntry, ...]:
        return tuple(
            self._entry(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,state,rationale,truth_type "
                "FROM career_state_entries WHERE artist_id=? ORDER BY seq",
                (self.store.primary_artist_id,),
            )
        )

    def current_state(self) -> CareerStateEntry | None:
        row = self._conn.execute(
            "SELECT seq,id,artist_id,state,rationale,truth_type "
            "FROM career_state_entries WHERE artist_id=? ORDER BY seq DESC LIMIT 1",
            (self.store.primary_artist_id,),
        ).fetchone()
        return None if row is None else self._entry(row)

    def record_state(
        self,
        state: str,
        *,
        rationale: str | None = None,
        expected_current_id: str | None,
    ) -> CareerStateEntry:
        normalized_state = normalize_career_state(state)
        normalized_rationale = self._optional_rationale(rationale)
        current = self.current_state()
        actual_current_id = None if current is None else current.id
        if actual_current_id != expected_current_id:
            raise StaleCareerStateError(
                "Career State changed after it was reviewed; reload before replacing newer context"
            )
        if (
            current is not None
            and current.state == normalized_state
            and current.rationale == normalized_rationale
        ):
            return current

        entry_id = f"career_state_{uuid.uuid4().hex}"
        try:
            with self.store._tx():
                latest = self.current_state()
                latest_id = None if latest is None else latest.id
                if latest_id != expected_current_id:
                    raise StaleCareerStateError(
                        "Career State changed while the new context was being recorded"
                    )
                self._conn.execute(
                    "INSERT INTO career_state_entries("
                    "id,artist_id,state,rationale,truth_type) VALUES(?,?,?,?,?)",
                    (
                        entry_id,
                        self.store.primary_artist_id,
                        normalized_state,
                        normalized_rationale,
                        CAREER_STATE_TRUTH_TYPE,
                    ),
                )
        except StaleCareerStateError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"cannot record Career State: {exc}") from exc
        result = self.current_state()
        if result is None or result.id != entry_id:
            raise LineageCorruptionError("new Career State did not become current")
        return result

    def current_definition(self) -> CareerStateDefinition | None:
        current = self.current_state()
        return None if current is None else career_state_definition(current.state)

    def recommendation_weight(self, topic: str) -> str:
        normalized = str(topic).strip().upper().replace("-", "_").replace(" ", "_")
        if normalized not in CAREER_TOPICS:
            raise ValidationError(f"unsupported Career State recommendation topic: {normalized}")
        definition = self.current_definition()
        return "NORMAL" if definition is None else definition.weight_for(normalized)
