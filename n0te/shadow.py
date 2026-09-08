from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .lineage import LineageCorruptionError, LineageStore, NotFoundError
from .workspace import WorkspaceMemory

SHADOW_SCHEMA_VERSION = 1
SHADOW_COVERAGE = {"FULL", "INCREMENTAL"}
SHADOW_ACTORS = {"HUMAN", "N0TE", "EXTERNAL"}
SHADOW_OBJECT_KINDS = {
    "TRACK",
    "CLIP_REGION",
    "DEVICE_PLUGIN",
    "AUTOMATION",
    "ROUTING",
    "TEMPO",
    "MARKER",
    "TRANSPORT",
}
SHADOW_ACTIONS = {"SET", "REMOVE"}
SHADOW_STATES = {"EMPTY", "CURRENT", "STALE"}


class HostShadowError(RuntimeError):
    """Invalid, stale or unsafe Host Shadow operation."""


@dataclass(frozen=True)
class ShadowEventInput:
    object_kind: str
    object_ref: str
    field: str
    action: str
    value: Any = None
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        kind = _enum(self.object_kind, "object_kind", SHADOW_OBJECT_KINDS)
        object_ref = _text(self.object_ref, "object_ref")
        field = _text(self.field, "field")
        action = _enum(self.action, "action", SHADOW_ACTIONS)
        evidence = _optional_text(self.evidence_ref, "evidence_ref")
        if action == "REMOVE" and self.value is not None:
            raise HostShadowError("REMOVE events must not carry a value")
        if action == "SET":
            _canonical_json(self.value)
        object.__setattr__(self, "object_kind", kind)
        object.__setattr__(self, "object_ref", object_ref)
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "evidence_ref", evidence)


@dataclass(frozen=True)
class ShadowBatch:
    sequence: int
    id: str
    workspace_id: str
    workspace_observation_id: str
    host_runtime_fingerprint: str
    coverage: str
    actor: str
    evidence_ref: str
    verified: bool


@dataclass(frozen=True)
class ShadowFact:
    object_kind: str
    object_ref: str
    field: str
    value: Any
    batch_id: str
    actor: str
    evidence_ref: str


@dataclass(frozen=True)
class HostShadowState:
    status: str
    workspace_id: str
    current_workspace_observation_id: str
    baseline_batch_id: str | None
    latest_batch_id: str | None
    facts: tuple[ShadowFact, ...]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise HostShadowError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _enum(value: str, field: str, allowed: set[str]) -> str:
    text = _text(value, field).upper().replace("-", "_").replace(" ", "_")
    if text not in allowed:
        raise HostShadowError(f"unsupported {field}: {text}")
    return text


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HostShadowError("shadow SET value must be canonical JSON data") from exc


class HostShadow:
    """Append-only verified Technical Twin of one bounded current DAW workspace."""

    _TRIGGER_NAMES = {
        "host_shadow_batches_immutable_update",
        "host_shadow_batches_immutable_delete",
        "host_shadow_events_immutable_update",
        "host_shadow_events_immutable_delete",
        "host_shadow_batch_matches_workspace_observation",
        "host_shadow_incremental_requires_full",
        "activity_host_shadow_batch",
    }

    def __init__(self, store: LineageStore, workspaces: WorkspaceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("HostShadow requires the canonical LineageStore")
        if not isinstance(workspaces, WorkspaceMemory):
            raise TypeError("HostShadow requires WorkspaceMemory")
        if workspaces.store is not store:
            raise TypeError("HostShadow and WorkspaceMemory must share one LineageStore")
        self.store = store
        self.workspaces = workspaces
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

    @staticmethod
    def _trigger_statements() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER host_shadow_batches_immutable_update
            BEFORE UPDATE ON host_shadow_batches
            BEGIN SELECT RAISE(ABORT, 'Host Shadow batches are append-only'); END""",
            """CREATE TRIGGER host_shadow_batches_immutable_delete
            BEFORE DELETE ON host_shadow_batches
            BEGIN SELECT RAISE(ABORT, 'Host Shadow batches are append-only'); END""",
            """CREATE TRIGGER host_shadow_events_immutable_update
            BEFORE UPDATE ON host_shadow_events
            BEGIN SELECT RAISE(ABORT, 'Host Shadow events are append-only'); END""",
            """CREATE TRIGGER host_shadow_events_immutable_delete
            BEFORE DELETE ON host_shadow_events
            BEGIN SELECT RAISE(ABORT, 'Host Shadow events are append-only'); END""",
            """CREATE TRIGGER host_shadow_batch_matches_workspace_observation
            BEFORE INSERT ON host_shadow_batches
            WHEN NOT EXISTS (
                SELECT 1
                FROM workspace_observations o
                WHERE o.id=NEW.workspace_observation_id
                  AND o.workspace_id=NEW.workspace_id
                  AND o.host_runtime_fingerprint=NEW.host_runtime_fingerprint
                  AND o.seq=(
                      SELECT MAX(latest.seq)
                      FROM workspace_observations latest
                      WHERE latest.workspace_id=NEW.workspace_id
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'Host Shadow batch is not bound to the current workspace observation');
            END""",
            """CREATE TRIGGER host_shadow_incremental_requires_full
            BEFORE INSERT ON host_shadow_batches
            WHEN NEW.coverage='INCREMENTAL' AND NOT EXISTS (
                SELECT 1
                FROM host_shadow_batches full_batch
                WHERE full_batch.workspace_id=NEW.workspace_id
                  AND full_batch.workspace_observation_id=NEW.workspace_observation_id
                  AND full_batch.coverage='FULL'
                  AND full_batch.verified=1
            )
            BEGIN
                SELECT RAISE(ABORT, 'incremental Host Shadow requires a current FULL baseline');
            END""",
            """CREATE TRIGGER activity_host_shadow_batch
            AFTER INSERT ON host_shadow_batches
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'HOST_SHADOW_'||NEW.coverage,
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    w.song_id,
                    NULL,
                    'HOST_SHADOW_BATCH',
                    NEW.id,
                    json_object('actor',NEW.actor,'coverage',NEW.coverage)
                FROM workspaces w WHERE w.id=NEW.workspace_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        table_names = ("host_shadow_batches", "host_shadow_events")
        present = [self._table_exists(name) for name in table_names]
        version = self._metadata_value("host_shadow_schema_version")
        if len(set(present)) != 1 or present[0] != (version is not None):
            raise LineageCorruptionError("Host Shadow schema metadata/table mismatch")
        if present[0]:
            if version != str(SHADOW_SCHEMA_VERSION):
                raise LineageCorruptionError(
                    f"unsupported Host Shadow schema version: {version}"
                )
            return
        if not self._table_exists("workspace_observations") or not self._table_exists(
            "activity_events"
        ):
            raise LineageCorruptionError(
                "HostShadow requires WorkspaceMemory and ActivityLog first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE host_shadow_batches (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                        workspace_observation_id TEXT NOT NULL REFERENCES workspace_observations(id),
                        host_runtime_fingerprint TEXT NOT NULL
                            CHECK(length(trim(host_runtime_fingerprint)) > 0),
                        coverage TEXT NOT NULL CHECK(coverage IN ('FULL','INCREMENTAL')),
                        actor TEXT NOT NULL CHECK(actor IN ('HUMAN','N0TE','EXTERNAL')),
                        evidence_ref TEXT NOT NULL CHECK(length(trim(evidence_ref)) > 0),
                        verified INTEGER NOT NULL CHECK(verified=1)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE host_shadow_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        batch_id TEXT NOT NULL REFERENCES host_shadow_batches(id),
                        object_kind TEXT NOT NULL CHECK(object_kind IN (
                            'TRACK','CLIP_REGION','DEVICE_PLUGIN','AUTOMATION',
                            'ROUTING','TEMPO','MARKER','TRANSPORT'
                        )),
                        object_ref TEXT NOT NULL CHECK(length(trim(object_ref)) > 0),
                        field TEXT NOT NULL CHECK(length(trim(field)) > 0),
                        action TEXT NOT NULL CHECK(action IN ('SET','REMOVE')),
                        value_json TEXT NULL,
                        evidence_ref TEXT NULL,
                        CHECK(
                            (action='SET' AND value_json IS NOT NULL)
                            OR
                            (action='REMOVE' AND value_json IS NULL)
                        ),
                        UNIQUE(batch_id,object_kind,object_ref,field)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX host_shadow_batches_by_workspace "
                    "ON host_shadow_batches(workspace_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX host_shadow_events_by_batch "
                    "ON host_shadow_events(batch_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('host_shadow_schema_version',?)",
                    (str(SHADOW_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Host Shadow schema") from exc

    @staticmethod
    def _batch(row: sqlite3.Row) -> ShadowBatch:
        return ShadowBatch(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_observation_id=str(row["workspace_observation_id"]),
            host_runtime_fingerprint=str(row["host_runtime_fingerprint"]),
            coverage=str(row["coverage"]),
            actor=str(row["actor"]),
            evidence_ref=str(row["evidence_ref"]),
            verified=bool(row["verified"]),
        )

    def _validate_existing(self) -> None:
        try:
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND (name LIKE 'host_shadow_%' OR name='activity_host_shadow_batch')"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"Host Shadow hooks are incomplete: {sorted(missing)}"
                )

            seen_full: set[tuple[str, str]] = set()
            for row in self._conn.execute(
                "SELECT seq,id,workspace_id,workspace_observation_id,"
                "host_runtime_fingerprint,coverage,actor,evidence_ref,verified "
                "FROM host_shadow_batches ORDER BY seq"
            ):
                batch = self._batch(row)
                if batch.coverage not in SHADOW_COVERAGE:
                    raise LineageCorruptionError("Host Shadow coverage is invalid")
                if batch.actor not in SHADOW_ACTORS or not batch.verified:
                    raise LineageCorruptionError("Host Shadow actor/verification is invalid")
                obs = self._conn.execute(
                    "SELECT workspace_id,host_runtime_fingerprint "
                    "FROM workspace_observations WHERE id=?",
                    (batch.workspace_observation_id,),
                ).fetchone()
                if (
                    obs is None
                    or str(obs["workspace_id"]) != batch.workspace_id
                    or str(obs["host_runtime_fingerprint"])
                    != batch.host_runtime_fingerprint
                ):
                    raise LineageCorruptionError(
                        "Host Shadow batch crosses workspace observation boundary"
                    )
                key = (batch.workspace_id, batch.workspace_observation_id)
                if batch.coverage == "FULL":
                    seen_full.add(key)
                elif key not in seen_full:
                    raise LineageCorruptionError(
                        "Host Shadow incremental predates its FULL baseline"
                    )

            for row in self._conn.execute(
                """SELECT e.id,e.batch_id,e.object_kind,e.object_ref,e.field,
                          e.action,e.value_json,e.evidence_ref
                   FROM host_shadow_events e ORDER BY e.seq"""
            ):
                kind = str(row["object_kind"])
                action = str(row["action"])
                if kind not in SHADOW_OBJECT_KINDS or action not in SHADOW_ACTIONS:
                    raise LineageCorruptionError("Host Shadow event kind/action is invalid")
                _text(str(row["object_ref"]), "object_ref")
                _text(str(row["field"]), "field")
                if row["evidence_ref"] is not None:
                    _text(str(row["evidence_ref"]), "evidence_ref")
                if action == "REMOVE":
                    if row["value_json"] is not None:
                        raise LineageCorruptionError(
                            "Host Shadow REMOVE event contains a value"
                        )
                    continue
                if row["value_json"] is None:
                    raise LineageCorruptionError("Host Shadow SET event has no value")
                try:
                    value = json.loads(str(row["value_json"]))
                    canonical = _canonical_json(value)
                except Exception as exc:
                    raise LineageCorruptionError(
                        "Host Shadow event JSON is invalid"
                    ) from exc
                if canonical != str(row["value_json"]):
                    raise LineageCorruptionError(
                        "Host Shadow event JSON is not canonical"
                    )
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError(
                "Host Shadow state is unreadable or corrupt"
            ) from exc

    def _current_binding(self, workspace_id: str) -> tuple[str, str]:
        state = self.workspaces.state(workspace_id)
        return (
            state.current_observation.id,
            state.current_observation.host_runtime_fingerprint,
        )

    def record_batch(
        self,
        workspace_id: str,
        *,
        workspace_observation_id: str,
        host_runtime_fingerprint: str,
        coverage: str,
        actor: str,
        evidence_ref: str,
        verified: bool,
        events: Iterable[ShadowEventInput] = (),
    ) -> ShadowBatch:
        workspace = self.workspaces._require(workspace_id)
        observation_id = _text(
            workspace_observation_id, "workspace_observation_id"
        )
        runtime_fingerprint = _text(
            host_runtime_fingerprint, "host_runtime_fingerprint"
        )
        coverage_name = _enum(coverage, "coverage", SHADOW_COVERAGE)
        actor_name = _enum(actor, "actor", SHADOW_ACTORS)
        evidence = _text(evidence_ref, "evidence_ref")
        if verified is not True:
            raise HostShadowError("Host Shadow accepts only verified observations")
        current_observation_id, current_runtime_fingerprint = self._current_binding(
            workspace.id
        )
        if (
            observation_id != current_observation_id
            or runtime_fingerprint != current_runtime_fingerprint
        ):
            raise HostShadowError(
                "Host Shadow batch is stale relative to the current workspace observation"
            )

        normalized = tuple(events)
        if not all(isinstance(item, ShadowEventInput) for item in normalized):
            raise TypeError("events must contain ShadowEventInput values")
        if coverage_name == "INCREMENTAL" and not normalized:
            raise HostShadowError("INCREMENTAL Host Shadow batch must contain changes")
        keys = [
            (item.object_kind, item.object_ref, item.field) for item in normalized
        ]
        if len(keys) != len(set(keys)):
            raise HostShadowError(
                "one Host Shadow batch may update each object field at most once"
            )

        if coverage_name == "INCREMENTAL":
            full = self._conn.execute(
                """SELECT 1 FROM host_shadow_batches
                   WHERE workspace_id=? AND workspace_observation_id=?
                     AND coverage='FULL' AND verified=1
                   LIMIT 1""",
                (workspace.id, observation_id),
            ).fetchone()
            if full is None:
                raise HostShadowError(
                    "INCREMENTAL Host Shadow requires a FULL baseline "
                    "for the current workspace observation"
                )

        batch_id = _new_id("shb")
        with self.store._tx():
            self._conn.execute(
                """INSERT INTO host_shadow_batches(
                    id,workspace_id,workspace_observation_id,host_runtime_fingerprint,
                    coverage,actor,evidence_ref,verified
                ) VALUES(?,?,?,?,?,?,?,1)""",
                (
                    batch_id,
                    workspace.id,
                    observation_id,
                    runtime_fingerprint,
                    coverage_name,
                    actor_name,
                    evidence,
                ),
            )
            for item in normalized:
                value_json = (
                    None if item.action == "REMOVE" else _canonical_json(item.value)
                )
                self._conn.execute(
                    """INSERT INTO host_shadow_events(
                        id,batch_id,object_kind,object_ref,field,action,value_json,evidence_ref
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        _new_id("she"),
                        batch_id,
                        item.object_kind,
                        item.object_ref,
                        item.field,
                        item.action,
                        value_json,
                        item.evidence_ref,
                    ),
                )
        row = self._conn.execute(
            "SELECT seq,id,workspace_id,workspace_observation_id,"
            "host_runtime_fingerprint,coverage,actor,evidence_ref,verified "
            "FROM host_shadow_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("Host Shadow batch disappeared")
        return self._batch(row)

    def history(self, workspace_id: str) -> tuple[ShadowBatch, ...]:
        self.workspaces._require(workspace_id)
        return tuple(
            self._batch(row)
            for row in self._conn.execute(
                "SELECT seq,id,workspace_id,workspace_observation_id,"
                "host_runtime_fingerprint,coverage,actor,evidence_ref,verified "
                "FROM host_shadow_batches WHERE workspace_id=? ORDER BY seq",
                (workspace_id,),
            )
        )

    def state(self, workspace_id: str) -> HostShadowState:
        workspace = self.workspaces._require(workspace_id)
        current_observation_id, current_runtime_fingerprint = self._current_binding(
            workspace.id
        )
        any_batch = self._conn.execute(
            "SELECT 1 FROM host_shadow_batches WHERE workspace_id=? LIMIT 1",
            (workspace.id,),
        ).fetchone()
        if any_batch is None:
            return HostShadowState(
                status="EMPTY",
                workspace_id=workspace.id,
                current_workspace_observation_id=current_observation_id,
                baseline_batch_id=None,
                latest_batch_id=None,
                facts=(),
            )

        baseline_row = self._conn.execute(
            """SELECT seq,id,workspace_id,workspace_observation_id,
                      host_runtime_fingerprint,coverage,actor,evidence_ref,verified
               FROM host_shadow_batches
               WHERE workspace_id=? AND workspace_observation_id=?
                 AND host_runtime_fingerprint=?
                 AND coverage='FULL' AND verified=1
               ORDER BY seq DESC LIMIT 1""",
            (workspace.id, current_observation_id, current_runtime_fingerprint),
        ).fetchone()
        if baseline_row is None:
            return HostShadowState(
                status="STALE",
                workspace_id=workspace.id,
                current_workspace_observation_id=current_observation_id,
                baseline_batch_id=None,
                latest_batch_id=None,
                facts=(),
            )
        baseline = self._batch(baseline_row)

        batch_rows = self._conn.execute(
            """SELECT seq,id,workspace_id,workspace_observation_id,
                      host_runtime_fingerprint,coverage,actor,evidence_ref,verified
               FROM host_shadow_batches
               WHERE workspace_id=? AND workspace_observation_id=? AND seq>=?
               ORDER BY seq""",
            (workspace.id, current_observation_id, baseline.sequence),
        ).fetchall()
        batches = tuple(self._batch(row) for row in batch_rows)
        latest = batches[-1]

        projection: dict[tuple[str, str, str], ShadowFact] = {}
        for batch in batches:
            rows = self._conn.execute(
                """SELECT object_kind,object_ref,field,action,value_json,evidence_ref
                   FROM host_shadow_events WHERE batch_id=? ORDER BY seq""",
                (batch.id,),
            ).fetchall()
            for row in rows:
                key = (
                    str(row["object_kind"]),
                    str(row["object_ref"]),
                    str(row["field"]),
                )
                if str(row["action"]) == "REMOVE":
                    projection.pop(key, None)
                    continue
                event_evidence = (
                    batch.evidence_ref
                    if row["evidence_ref"] is None
                    else str(row["evidence_ref"])
                )
                projection[key] = ShadowFact(
                    object_kind=key[0],
                    object_ref=key[1],
                    field=key[2],
                    value=json.loads(str(row["value_json"])),
                    batch_id=batch.id,
                    actor=batch.actor,
                    evidence_ref=event_evidence,
                )

        facts = tuple(projection[key] for key in sorted(projection))
        return HostShadowState(
            status="CURRENT",
            workspace_id=workspace.id,
            current_workspace_observation_id=current_observation_id,
            baseline_batch_id=baseline.id,
            latest_batch_id=latest.id,
            facts=facts,
        )

    def require_current(self, workspace_id: str) -> HostShadowState:
        state = self.state(workspace_id)
        if state.status != "CURRENT":
            raise HostShadowError(
                f"Host Shadow is {state.status}; a verified FULL baseline is required"
            )
        return state
