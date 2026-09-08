from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .learning import DECISION_KINDS
from .lineage import LineageCorruptionError, LineageStore, ValidationError

VERSION_COMPARE_DECISION_SCHEMA_VERSION = 1


class VersionCompareDecisionError(RuntimeError):
    """An exact Version-pair artist decision cannot be recorded safely."""


class StaleVersionCompareDecisionError(VersionCompareDecisionError):
    """The active Song or exact comparison pair moved after authority was rendered."""


@dataclass(frozen=True)
class VersionCompareDecisionBinding:
    song_id: str
    reference_version_id: str
    current_version_id: str


@dataclass(frozen=True)
class VersionCompareDecision:
    sequence: int
    id: str
    artist_id: str
    song_id: str
    reference_version_id: str
    current_version_id: str
    decision: str
    rationale: str | None


class VersionCompareDecisionMemory:
    """Append-only artist judgment for one exact Song Version comparison pair.

    This memory deliberately does not own Version approval, current-Version changes,
    Learning experiments, audio processing, provider calls, or DAW authority. A
    KEEP/REVERT/REVISE/INCONCLUSIVE record is only the artist's judgment about the
    exact Reference and Current Versions that were compared.

    ``create=False`` is a read-only inspection mode used by GET rendering. It never
    creates schema or product state. ``create=True`` is reserved for an explicit
    artist decision write.
    """

    _TABLE = "version_compare_decisions"
    _METADATA_KEY = "version_compare_decision_schema_version"
    _TRIGGER_NAMES = {
        "version_compare_decision_pair_same_song",
        "version_compare_decision_binding_immutable",
        "version_compare_decision_delete_immutable",
        "version_compare_decision_activity",
    }

    def __init__(self, store: LineageStore, *, create: bool = True):
        if not isinstance(store, LineageStore):
            raise TypeError("VersionCompareDecisionMemory requires canonical LineageStore")
        self.store = store
        self._conn = store._conn
        self._initialized = self._ensure_schema() if create else self._inspect_schema()
        if self._initialized:
            self._validate_existing()

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _metadata_value(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?", (self._METADATA_KEY,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _inspect_schema(self) -> bool:
        table_exists = self._table_exists(self._TABLE)
        version = self._metadata_value()
        if table_exists != (version is not None):
            raise LineageCorruptionError("Version comparison decision schema metadata/table mismatch")
        if not table_exists:
            return False
        if version != str(VERSION_COMPARE_DECISION_SCHEMA_VERSION):
            raise LineageCorruptionError(
                f"unsupported Version comparison decision schema version: {version}"
            )
        return True

    def _ensure_schema(self) -> bool:
        if self._inspect_schema():
            return True
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "VersionCompareDecisionMemory requires canonical Activity chronology first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE version_compare_decisions (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        artist_id TEXT NOT NULL REFERENCES artists(id),
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        reference_version_id TEXT NOT NULL REFERENCES versions(id),
                        current_version_id TEXT NOT NULL REFERENCES versions(id),
                        decision TEXT NOT NULL CHECK(decision IN (
                            'KEEP','REVERT','REVISE','INCONCLUSIVE'
                        )),
                        rationale TEXT NULL,
                        CHECK(reference_version_id <> current_version_id),
                        CHECK(rationale IS NULL OR length(trim(rationale)) > 0)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX version_compare_decisions_pair "
                    "ON version_compare_decisions(song_id,reference_version_id,current_version_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    (self._METADATA_KEY, str(VERSION_COMPARE_DECISION_SCHEMA_VERSION)),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize Version comparison decision memory"
            ) from exc
        return True

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER version_compare_decision_pair_same_song
            BEFORE INSERT ON version_compare_decisions
            WHEN NOT EXISTS (
                SELECT 1
                FROM versions r
                JOIN versions c ON c.id=NEW.current_version_id
                JOIN songs s ON s.id=NEW.song_id
                WHERE r.id=NEW.reference_version_id
                  AND r.song_id=NEW.song_id
                  AND c.song_id=NEW.song_id
                  AND s.artist_id=NEW.artist_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'Version comparison decision pair crosses Song or Artist');
            END""",
            """CREATE TRIGGER version_compare_decision_binding_immutable
            BEFORE UPDATE ON version_compare_decisions
            BEGIN
                SELECT RAISE(ABORT, 'Version comparison decisions are immutable');
            END""",
            """CREATE TRIGGER version_compare_decision_delete_immutable
            BEFORE DELETE ON version_compare_decisions
            BEGIN
                SELECT RAISE(ABORT, 'Version comparison decisions are append-only');
            END""",
            """CREATE TRIGGER version_compare_decision_activity
            AFTER INSERT ON version_compare_decisions
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,
                    object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'VERSION_COMPARE_DECISION_RECORDED',
                    NEW.artist_id,NEW.song_id,NEW.current_version_id,
                    'VERSION_COMPARE_DECISION',NEW.id,
                    '{\"decision\":\"'||NEW.decision||'\"}'
                );
            END""",
        )

    @staticmethod
    def _optional_rationale(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError("comparison decision rationale must be text")
        text = " ".join(value.split())
        if not text:
            return None
        if len(text) > 1200:
            raise ValidationError("comparison decision rationale is too long")
        return text

    @staticmethod
    def _row(row: sqlite3.Row) -> VersionCompareDecision:
        return VersionCompareDecision(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            artist_id=str(row["artist_id"]),
            song_id=str(row["song_id"]),
            reference_version_id=str(row["reference_version_id"]),
            current_version_id=str(row["current_version_id"]),
            decision=str(row["decision"]),
            rationale=None if row["rationale"] is None else str(row["rationale"]),
        )

    def _validate_existing(self) -> None:
        trigger_names = {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'version_compare_decision_%'"
            )
        }
        missing = self._TRIGGER_NAMES - trigger_names
        if missing:
            raise LineageCorruptionError(
                f"Version comparison decision integrity hooks are incomplete: {sorted(missing)}"
            )
        invalid = self._conn.execute(
            "SELECT d.id FROM version_compare_decisions d "
            "LEFT JOIN artists a ON a.id=d.artist_id "
            "LEFT JOIN songs s ON s.id=d.song_id "
            "LEFT JOIN versions r ON r.id=d.reference_version_id "
            "LEFT JOIN versions c ON c.id=d.current_version_id "
            "WHERE a.id IS NULL OR s.id IS NULL OR r.id IS NULL OR c.id IS NULL "
            "OR s.artist_id<>d.artist_id OR r.song_id<>d.song_id OR c.song_id<>d.song_id "
            "OR r.id=c.id OR d.decision NOT IN ('KEEP','REVERT','REVISE','INCONCLUSIVE') "
            "OR (d.rationale IS NOT NULL AND length(trim(d.rationale))=0) "
            "LIMIT 1"
        ).fetchone()
        if invalid is not None:
            raise LineageCorruptionError(
                "Version comparison decision memory contains invalid pair bindings"
            )

    @property
    def initialized(self) -> bool:
        return self._initialized

    def latest_for_pair(
        self,
        song_id: str,
        reference_version_id: str,
        current_version_id: str,
    ) -> VersionCompareDecision | None:
        if not self._initialized:
            return None
        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,reference_version_id,current_version_id,decision,rationale "
            "FROM version_compare_decisions "
            "WHERE song_id=? AND reference_version_id=? AND current_version_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (str(song_id), str(reference_version_id), str(current_version_id)),
        ).fetchone()
        return None if row is None else self._row(row)

    def decisions_for_song(self, song_id: str) -> tuple[VersionCompareDecision, ...]:
        if not self._initialized:
            return ()
        if self.store.get_song(str(song_id)) is None:
            raise ValidationError("comparison decision Song does not exist")
        return tuple(
            self._row(row)
            for row in self._conn.execute(
                "SELECT seq,id,artist_id,song_id,reference_version_id,current_version_id,decision,rationale "
                "FROM version_compare_decisions WHERE song_id=? ORDER BY seq",
                (str(song_id),),
            )
        )

    def record(
        self,
        binding: VersionCompareDecisionBinding,
        *,
        decision: str,
        rationale: str | None = None,
    ) -> VersionCompareDecision:
        if not self._initialized:
            raise VersionCompareDecisionError("comparison decision memory is not initialized")
        if not isinstance(binding, VersionCompareDecisionBinding):
            raise TypeError("binding must be VersionCompareDecisionBinding")
        kind = str(decision).strip().upper()
        if kind not in DECISION_KINDS:
            raise ValidationError(f"unsupported comparison decision: {kind}")
        note = self._optional_rationale(rationale)
        decision_id = f"vcd_{uuid.uuid4().hex}"

        try:
            with self.store._tx():
                active = self._conn.execute(
                    "SELECT value FROM metadata WHERE key='active_song_id'"
                ).fetchone()
                if active is None or str(active["value"]) != binding.song_id:
                    raise StaleVersionCompareDecisionError(
                        "The active Song changed after this A/B decision was prepared."
                    )
                song = self._conn.execute(
                    "SELECT id,artist_id,current_version_id,approved_version_id "
                    "FROM songs WHERE id=?",
                    (binding.song_id,),
                ).fetchone()
                if song is None:
                    raise StaleVersionCompareDecisionError(
                        "The compared Song no longer exists."
                    )
                if song["current_version_id"] != binding.current_version_id:
                    raise StaleVersionCompareDecisionError(
                        "The current Version changed after this A/B decision was prepared."
                    )
                if binding.reference_version_id == binding.current_version_id:
                    raise VersionCompareDecisionError(
                        "A comparison decision requires two distinct Versions."
                    )
                pair_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) AS n FROM versions "
                        "WHERE song_id=? AND id IN (?,?)",
                        (
                            binding.song_id,
                            binding.reference_version_id,
                            binding.current_version_id,
                        ),
                    ).fetchone()["n"]
                )
                if pair_count != 2:
                    raise StaleVersionCompareDecisionError(
                        "The exact A/B pair is no longer valid for this Song."
                    )
                self._conn.execute(
                    "INSERT INTO version_compare_decisions("
                    "id,artist_id,song_id,reference_version_id,current_version_id,decision,rationale"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        str(song["artist_id"]),
                        binding.song_id,
                        binding.reference_version_id,
                        binding.current_version_id,
                        kind,
                        note,
                    ),
                )
        except StaleVersionCompareDecisionError:
            raise
        except VersionCompareDecisionError:
            raise
        except sqlite3.IntegrityError as exc:
            raise VersionCompareDecisionError(
                f"Version comparison decision was rejected safely: {exc}"
            ) from exc

        row = self._conn.execute(
            "SELECT seq,id,artist_id,song_id,reference_version_id,current_version_id,decision,rationale "
            "FROM version_compare_decisions WHERE id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("Version comparison decision disappeared after commit")
        result = self._row(row)
        if result.decision != kind:
            raise LineageCorruptionError("Version comparison decision changed after commit")
        return result
