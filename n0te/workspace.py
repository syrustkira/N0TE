from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from .hosts import HostRuntimeIdentity, normalize_host_family
from .platforms import PlatformEnvironment, target_tier
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError

WORKSPACE_SCHEMA_VERSION = 1
WORKSPACE_RELATIONS = {"DUPLICATE", "FORK"}
EXISTING_RECONCILIATIONS = {"SAME_OR_MOVED", "RECOVERED"}
OBSERVATION_KINDS = {"CREATED", "SAME_OR_MOVED", "RECOVERED", "DUPLICATED", "FORKED"}


class WorkspaceError(RuntimeError):
    """Invalid workspace identity or reconciliation operation."""


@dataclass(frozen=True)
class WorkspaceIdentity:
    id: str
    song_id: str
    host_family: str
    source_workspace_id: str | None
    source_relation: str | None


@dataclass(frozen=True)
class WorkspaceObservation:
    sequence: int
    id: str
    workspace_id: str
    observation_kind: str
    location_ref: str
    display_name: str | None
    host_runtime_fingerprint: str
    runtime_identity: dict[str, object]
    state_fingerprint: str | None


@dataclass(frozen=True)
class WorkspaceState:
    workspace: WorkspaceIdentity
    current_observation: WorkspaceObservation
    history: tuple[WorkspaceObservation, ...]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise WorkspaceError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _runtime_json(runtime: HostRuntimeIdentity) -> str:
    payload = runtime.identity_payload()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class WorkspaceMemory:
    """Durable project/workspace identity above paths and host-specific project mechanics."""

    _TRIGGER_NAMES = {
        "workspace_source_same_song",
        "workspaces_immutable_update",
        "workspaces_immutable_delete",
        "workspace_observations_immutable_update",
        "workspace_observations_immutable_delete",
        "workspace_location_current_unique",
        "activity_workspace_observation",
    }

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("WorkspaceMemory requires the canonical LineageStore")
        self.store = store
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
        return (
            """CREATE TRIGGER workspace_source_same_song
            BEFORE INSERT ON workspaces
            WHEN NEW.source_workspace_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM workspaces source
                WHERE source.id=NEW.source_workspace_id AND source.song_id=NEW.song_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'source workspace belongs to a different Song or is missing');
            END""",
            """CREATE TRIGGER workspaces_immutable_update
            BEFORE UPDATE ON workspaces
            BEGIN SELECT RAISE(ABORT, 'workspace identity is immutable'); END""",
            """CREATE TRIGGER workspaces_immutable_delete
            BEFORE DELETE ON workspaces
            BEGIN SELECT RAISE(ABORT, 'workspace identity is immutable'); END""",
            """CREATE TRIGGER workspace_observations_immutable_update
            BEFORE UPDATE ON workspace_observations
            BEGIN SELECT RAISE(ABORT, 'workspace history is append-only'); END""",
            """CREATE TRIGGER workspace_observations_immutable_delete
            BEFORE DELETE ON workspace_observations
            BEGIN SELECT RAISE(ABORT, 'workspace history is append-only'); END""",
            """CREATE TRIGGER workspace_location_current_unique
            BEFORE INSERT ON workspace_observations
            WHEN EXISTS (
                SELECT 1
                FROM workspace_observations prior
                WHERE prior.workspace_id<>NEW.workspace_id
                  AND prior.location_ref=NEW.location_ref
                  AND prior.seq=(
                      SELECT MAX(latest.seq)
                      FROM workspace_observations latest
                      WHERE latest.workspace_id=prior.workspace_id
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'location is currently associated with another workspace');
            END""",
            """CREATE TRIGGER activity_workspace_observation
            AFTER INSERT ON workspace_observations
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'WORKSPACE_'||NEW.observation_kind,
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    w.song_id,
                    NULL,
                    'WORKSPACE',
                    NEW.workspace_id,
                    '{}'
                FROM workspaces w WHERE w.id=NEW.workspace_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        table_names = ("workspaces", "workspace_observations")
        present = [self._table_exists(name) for name in table_names]
        version = self._metadata_value("workspace_schema_version")
        if len(set(present)) != 1 or present[0] != (version is not None):
            raise LineageCorruptionError("workspace schema metadata/table mismatch")
        if present[0]:
            if version != str(WORKSPACE_SCHEMA_VERSION):
                raise LineageCorruptionError(f"unsupported workspace schema version: {version}")
            return
        if not self._table_exists("songs") or not self._table_exists("activity_events"):
            raise LineageCorruptionError(
                "WorkspaceMemory requires canonical Song identity and ActivityLog first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE workspaces (
                        id TEXT PRIMARY KEY,
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        host_family TEXT NOT NULL CHECK(length(trim(host_family)) > 0),
                        source_workspace_id TEXT NULL REFERENCES workspaces(id),
                        source_relation TEXT NULL CHECK(source_relation IN ('DUPLICATE','FORK')),
                        CHECK(
                            (source_workspace_id IS NULL AND source_relation IS NULL)
                            OR
                            (source_workspace_id IS NOT NULL AND source_relation IS NOT NULL)
                        )
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE workspace_observations (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                        observation_kind TEXT NOT NULL CHECK(observation_kind IN (
                            'CREATED','SAME_OR_MOVED','RECOVERED','DUPLICATED','FORKED'
                        )),
                        location_ref TEXT NOT NULL CHECK(length(trim(location_ref)) > 0),
                        display_name TEXT NULL,
                        host_runtime_fingerprint TEXT NOT NULL
                            CHECK(length(trim(host_runtime_fingerprint)) > 0),
                        runtime_identity_json TEXT NOT NULL
                            CHECK(length(trim(runtime_identity_json)) > 0),
                        state_fingerprint TEXT NULL
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX workspace_observations_by_workspace "
                    "ON workspace_observations(workspace_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX workspace_observations_by_location "
                    "ON workspace_observations(location_ref,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('workspace_schema_version',?)",
                    (str(WORKSPACE_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize workspace identity schema") from exc

    @staticmethod
    def _workspace(row: sqlite3.Row) -> WorkspaceIdentity:
        return WorkspaceIdentity(
            id=str(row["id"]),
            song_id=str(row["song_id"]),
            host_family=str(row["host_family"]),
            source_workspace_id=None if row["source_workspace_id"] is None else str(row["source_workspace_id"]),
            source_relation=None if row["source_relation"] is None else str(row["source_relation"]),
        )

    @staticmethod
    def _observation(row: sqlite3.Row) -> WorkspaceObservation:
        try:
            payload = json.loads(str(row["runtime_identity_json"]))
        except Exception as exc:
            raise LineageCorruptionError("workspace runtime identity JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise LineageCorruptionError("workspace runtime identity must be a JSON object")
        return WorkspaceObservation(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            observation_kind=str(row["observation_kind"]),
            location_ref=str(row["location_ref"]),
            display_name=None if row["display_name"] is None else str(row["display_name"]),
            host_runtime_fingerprint=str(row["host_runtime_fingerprint"]),
            runtime_identity=payload,
            state_fingerprint=None if row["state_fingerprint"] is None else str(row["state_fingerprint"]),
        )

    def _history(self, workspace_id: str) -> tuple[WorkspaceObservation, ...]:
        return tuple(
            self._observation(row)
            for row in self._conn.execute(
                "SELECT seq,id,workspace_id,observation_kind,location_ref,display_name,"
                "host_runtime_fingerprint,runtime_identity_json,state_fingerprint "
                "FROM workspace_observations WHERE workspace_id=? ORDER BY seq",
                (workspace_id,),
            )
        )

    def _validate_existing(self) -> None:
        try:
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND (name LIKE 'workspace_%' OR name LIKE 'workspaces_%' "
                    "OR name='activity_workspace_observation')"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(f"workspace hooks are incomplete: {sorted(missing)}")
            collision = self._conn.execute(
                """WITH latest AS (
                    SELECT o.workspace_id,o.location_ref
                    FROM workspace_observations o
                    JOIN (
                        SELECT workspace_id,MAX(seq) AS max_seq
                        FROM workspace_observations GROUP BY workspace_id
                    ) x ON x.workspace_id=o.workspace_id AND x.max_seq=o.seq
                )
                SELECT location_ref FROM latest
                GROUP BY location_ref HAVING COUNT(*)>1 LIMIT 1"""
            ).fetchone()
            if collision is not None:
                raise LineageCorruptionError("multiple workspaces claim the same current location")
            for row in self._conn.execute(
                "SELECT id,song_id,host_family,source_workspace_id,source_relation "
                "FROM workspaces ORDER BY id"
            ):
                workspace = self._workspace(row)
                try:
                    canonical_family = normalize_host_family(workspace.host_family)
                except Exception as exc:
                    raise LineageCorruptionError("workspace contains invalid host family") from exc
                if canonical_family != workspace.host_family:
                    raise LineageCorruptionError("workspace host family is not canonical")
                if workspace.source_workspace_id == workspace.id:
                    raise LineageCorruptionError("workspace cannot derive from itself")
                if workspace.source_workspace_id is not None:
                    source = self._conn.execute(
                        "SELECT song_id FROM workspaces WHERE id=?",
                        (workspace.source_workspace_id,),
                    ).fetchone()
                    if source is None or str(source["song_id"]) != workspace.song_id:
                        raise LineageCorruptionError("workspace source lineage crosses Song boundary")
                    if workspace.source_relation not in WORKSPACE_RELATIONS:
                        raise LineageCorruptionError("workspace source relation is invalid")
                elif workspace.source_relation is not None:
                    raise LineageCorruptionError("workspace source relation is missing source identity")
                history = self._history(workspace.id)
                if not history:
                    raise LineageCorruptionError("workspace has no observation history")
                expected_first = (
                    "CREATED" if workspace.source_relation is None
                    else "DUPLICATED" if workspace.source_relation == "DUPLICATE"
                    else "FORKED"
                )
                if history[0].observation_kind != expected_first:
                    raise LineageCorruptionError("workspace first observation does not match lineage")
                for observation in history:
                    if observation.observation_kind not in OBSERVATION_KINDS:
                        raise LineageCorruptionError("workspace observation kind is invalid")
                    payload = observation.runtime_identity
                    required = {
                        "family","version","edition","os_family","architecture",
                        "translation_mode","generic_host_label","fingerprint",
                    }
                    if set(payload) != required:
                        raise LineageCorruptionError("workspace runtime identity payload shape is invalid")
                    if payload["family"] != workspace.host_family:
                        raise LineageCorruptionError("workspace runtime host family changed")
                    if payload["fingerprint"] != observation.host_runtime_fingerprint:
                        raise LineageCorruptionError("workspace runtime fingerprint mismatch")
                    try:
                        platform = PlatformEnvironment(
                            os_family=str(payload["os_family"]),
                            architecture=str(payload["architecture"]),
                            raw_os_name=str(payload["os_family"]),
                            raw_machine=str(payload["architecture"]),
                            target_tier=target_tier(
                                str(payload["os_family"]), str(payload["architecture"])
                            ),
                        )
                        rebuilt = HostRuntimeIdentity(
                            family=str(payload["family"]),
                            version=str(payload["version"]),
                            edition=str(payload["edition"]),
                            platform=platform,
                            translation_mode=str(payload["translation_mode"]),
                            generic_host_label=(
                                None
                                if payload["generic_host_label"] is None
                                else str(payload["generic_host_label"])
                            ),
                        )
                    except Exception as exc:
                        raise LineageCorruptionError(
                            "workspace runtime identity payload is invalid"
                        ) from exc
                    if rebuilt.fingerprint != observation.host_runtime_fingerprint:
                        raise LineageCorruptionError(
                            "workspace runtime identity fingerprint cannot be reproduced"
                        )
                for observation in history[1:]:
                    if observation.observation_kind not in EXISTING_RECONCILIATIONS:
                        raise LineageCorruptionError(
                            "workspace history contains an invalid post-creation relation"
                        )
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError("workspace identity state is unreadable or corrupt") from exc

    def get(self, workspace_id: str) -> WorkspaceIdentity | None:
        row = self._conn.execute(
            "SELECT id,song_id,host_family,source_workspace_id,source_relation "
            "FROM workspaces WHERE id=?",
            (str(workspace_id),),
        ).fetchone()
        return None if row is None else self._workspace(row)

    def _require(self, workspace_id: str) -> WorkspaceIdentity:
        workspace = self.get(workspace_id)
        if workspace is None:
            raise NotFoundError(f"workspace not found: {workspace_id}")
        return workspace

    def history(self, workspace_id: str) -> tuple[WorkspaceObservation, ...]:
        self._require(workspace_id)
        return self._history(workspace_id)

    def state(self, workspace_id: str) -> WorkspaceState:
        workspace = self._require(workspace_id)
        history = self._history(workspace_id)
        if not history:
            raise LineageCorruptionError("workspace has no observation history")
        return WorkspaceState(workspace, history[-1], history)

    def current_candidates_at_location(self, location_ref: str) -> tuple[WorkspaceIdentity, ...]:
        location = _text(location_ref, "location_ref")
        rows = self._conn.execute(
            """SELECT w.id,w.song_id,w.host_family,w.source_workspace_id,w.source_relation
            FROM workspaces w
            JOIN workspace_observations o ON o.workspace_id=w.id
            WHERE o.location_ref=?
              AND o.seq=(SELECT MAX(x.seq) FROM workspace_observations x WHERE x.workspace_id=w.id)
            ORDER BY w.id""",
            (location,),
        ).fetchall()
        return tuple(self._workspace(row) for row in rows)

    def _append_observation(
        self,
        workspace_id: str,
        *,
        kind: str,
        runtime: HostRuntimeIdentity,
        location_ref: str,
        display_name: str | None,
        state_fingerprint: str | None,
    ) -> None:
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        kind = str(kind).strip().upper()
        if kind not in OBSERVATION_KINDS:
            raise WorkspaceError(f"unsupported observation kind: {kind}")
        location = _text(location_ref, "location_ref")
        display = _optional_text(display_name, "display_name")
        state = _optional_text(state_fingerprint, "state_fingerprint")
        self._conn.execute(
            """INSERT INTO workspace_observations(
                id,workspace_id,observation_kind,location_ref,display_name,
                host_runtime_fingerprint,runtime_identity_json,state_fingerprint
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                _new_id("wobs"), workspace_id, kind, location, display,
                runtime.fingerprint, _runtime_json(runtime), state,
            ),
        )

    def create(
        self,
        song_id: str,
        *,
        runtime: HostRuntimeIdentity,
        location_ref: str,
        display_name: str | None = None,
        state_fingerprint: str | None = None,
    ) -> WorkspaceIdentity:
        self.store._require_song(song_id)
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        if self.current_candidates_at_location(location_ref):
            raise WorkspaceError(
                "location is already claimed by a current workspace; reconcile explicitly"
            )
        workspace_id = _new_id("wsp")
        with self.store._tx():
            self._conn.execute(
                "INSERT INTO workspaces(id,song_id,host_family) VALUES(?,?,?)",
                (workspace_id, song_id, runtime.family),
            )
            self._append_observation(
                workspace_id, kind="CREATED", runtime=runtime,
                location_ref=location_ref, display_name=display_name,
                state_fingerprint=state_fingerprint,
            )
        return self._require(workspace_id)

    def reconcile_existing(
        self,
        workspace_id: str,
        *,
        song_id: str,
        relation: str,
        runtime: HostRuntimeIdentity,
        location_ref: str,
        display_name: str | None = None,
        state_fingerprint: str | None = None,
    ) -> WorkspaceIdentity:
        workspace = self._require(workspace_id)
        if workspace.song_id != str(song_id):
            raise ValidationError("workspace belongs to a different Song")
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        relation = str(relation).strip().upper()
        if relation not in EXISTING_RECONCILIATIONS:
            raise WorkspaceError(
                "existing workspace reconciliation requires SAME_OR_MOVED or RECOVERED"
            )
        if runtime.family != workspace.host_family:
            raise WorkspaceError(
                "an existing workspace cannot silently change host family; create a fork instead"
            )
        candidates = self.current_candidates_at_location(location_ref)
        if any(candidate.id != workspace.id for candidate in candidates):
            raise WorkspaceError("target location is currently claimed by another workspace")
        with self.store._tx():
            self._append_observation(
                workspace.id, kind=relation, runtime=runtime,
                location_ref=location_ref, display_name=display_name,
                state_fingerprint=state_fingerprint,
            )
        return workspace

    def derive(
        self,
        source_workspace_id: str,
        *,
        song_id: str,
        relation: str,
        runtime: HostRuntimeIdentity,
        location_ref: str,
        display_name: str | None = None,
        state_fingerprint: str | None = None,
    ) -> WorkspaceIdentity:
        source = self._require(source_workspace_id)
        if source.song_id != str(song_id):
            raise ValidationError("source workspace belongs to a different Song")
        if not isinstance(runtime, HostRuntimeIdentity):
            raise TypeError("runtime must be HostRuntimeIdentity")
        relation = str(relation).strip().upper()
        if relation not in WORKSPACE_RELATIONS:
            raise WorkspaceError("derived workspace requires explicit DUPLICATE or FORK relation")
        if self.current_candidates_at_location(location_ref):
            raise WorkspaceError(
                "derived workspace location is already claimed; reconcile the existing workspace instead"
            )
        workspace_id = _new_id("wsp")
        kind = "DUPLICATED" if relation == "DUPLICATE" else "FORKED"
        with self.store._tx():
            self._conn.execute(
                """INSERT INTO workspaces(
                    id,song_id,host_family,source_workspace_id,source_relation
                ) VALUES(?,?,?,?,?)""",
                (workspace_id, song_id, runtime.family, source.id, relation),
            )
            self._append_observation(
                workspace_id, kind=kind, runtime=runtime,
                location_ref=location_ref, display_name=display_name,
                state_fingerprint=state_fingerprint,
            )
        return self._require(workspace_id)
