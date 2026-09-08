from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass

from .capabilities import CapabilityCandidate, CapabilityResolutionError, ROUTE_KINDS
from .lineage import LineageCorruptionError, LineageStore, NotFoundError
from .studio import StudioCapabilityProfile
from .workspace import WorkspaceMemory

CAPABILITY_EVIDENCE_SCHEMA_VERSION = 1
CAPABILITY_AVAILABILITY = {"AVAILABLE", "UNAVAILABLE", "UNKNOWN"}
CAPABILITY_EVIDENCE_KINDS = {
    "RUNTIME_PROBE",
    "ADAPTER_TEST",
    "OFFICIAL_FACT",
    "MANUAL_VERIFIED",
    "OTHER",
}


class CapabilityEvidenceError(RuntimeError):
    """Invalid, stale or unsafe capability-environment evidence operation."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise CapabilityEvidenceError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _enum(value: str, field: str, allowed: set[str]) -> str:
    text = _text(value, field).upper().replace("-", "_").replace(" ", "_")
    if text not in allowed:
        raise CapabilityEvidenceError(f"unsupported {field}: {text}")
    return text


def _score(value: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityEvidenceError(f"{field} must be between 0 and 1") from exc
    if not 0.0 <= number <= 1.0:
        raise CapabilityEvidenceError(f"{field} must be between 0 and 1")
    return number


def _nonnegative_int(value: int, field: str) -> int:
    if isinstance(value, bool):
        raise CapabilityEvidenceError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityEvidenceError(f"{field} must be a non-negative integer") from exc
    if number < 0:
        raise CapabilityEvidenceError(f"{field} must be a non-negative integer")
    return number


def _bool(value: bool, field: str) -> bool:
    if type(value) is not bool:
        raise CapabilityEvidenceError(f"{field} must be a real bool")
    return value


def _candidate_id(route_kind: str, route_id: str, capability: str) -> str:
    payload = (
        f"n0te-capability-route/v1\x00{route_kind}\x00{route_id}\x00{capability}"
    ).encode("utf-8")
    return "caproute_" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CapabilityObservation:
    sequence: int
    id: str
    workspace_id: str
    workspace_observation_id: str
    host_runtime_fingerprint: str
    route_id: str
    route_kind: str
    capability: str
    display_name: str
    brand: str | None
    availability: str
    evidence_kind: str
    evidence_ref: str | None
    observed_at_epoch_seconds: int
    task_fit: float
    editability: float
    locality: float
    privacy: float
    latency: float
    reversibility: float
    cost_efficiency: float
    portability: float
    paid: bool

    @property
    def candidate_id(self) -> str:
        return _candidate_id(self.route_kind, self.route_id, self.capability)

    @property
    def verified(self) -> bool:
        return self.availability != "UNKNOWN"

    @property
    def compatible(self) -> bool:
        return self.availability != "UNAVAILABLE"

    def to_candidate(self, *, now_epoch_seconds: int) -> CapabilityCandidate:
        now = _nonnegative_int(now_epoch_seconds, "now_epoch_seconds")
        if now < self.observed_at_epoch_seconds:
            raise CapabilityEvidenceError(
                "now_epoch_seconds predates the capability observation"
            )
        return CapabilityCandidate(
            candidate_id=self.candidate_id,
            route_kind=self.route_kind,
            capability=self.capability,
            display_name=self.display_name,
            brand=self.brand,
            verified=self.verified,
            compatible=self.compatible,
            evidence_ref=self.evidence_ref,
            evidence_age_seconds=now - self.observed_at_epoch_seconds,
            task_fit=self.task_fit,
            editability=self.editability,
            locality=self.locality,
            privacy=self.privacy,
            latency=self.latency,
            reversibility=self.reversibility,
            cost_efficiency=self.cost_efficiency,
            portability=self.portability,
            user_preference=0.5,
            paid=self.paid,
        )


@dataclass(frozen=True)
class CapabilityEnvironmentState:
    workspace_id: str
    workspace_observation_id: str
    host_runtime_fingerprint: str
    host_family: str
    current: tuple[CapabilityObservation, ...]
    stale_count: int

    @property
    def environment_id(self) -> str:
        payload = (
            f"n0te-capability-environment/v1\x00{self.workspace_id}\x00"
            f"{self.workspace_observation_id}\x00{self.host_runtime_fingerprint}"
        ).encode("utf-8")
        return "env_" + hashlib.sha256(payload).hexdigest()


class CapabilityEvidenceMemory:
    """Append-only capability truth bound to one exact current DAW environment.

    This layer does not probe a DAW, infer support from a host name, store artist
    preference, rank routes or execute anything. It accepts explicit probe/test/fact
    results only while they still match the current WorkspaceObservation, preserves
    history, and derives the existing CapabilityCandidate/StudioCapabilityProfile view.
    """

    _TRIGGER_NAMES = {
        "capability_observations_immutable_update",
        "capability_observations_immutable_delete",
        "capability_observation_matches_current_workspace",
        "capability_observation_time_nonregressing",
        "capability_route_kind_stable",
        "activity_capability_observation",
    }

    def __init__(self, store: LineageStore, workspaces: WorkspaceMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("CapabilityEvidenceMemory requires the canonical LineageStore")
        if not isinstance(workspaces, WorkspaceMemory):
            raise TypeError("CapabilityEvidenceMemory requires WorkspaceMemory")
        if workspaces.store is not store:
            raise TypeError(
                "CapabilityEvidenceMemory and WorkspaceMemory must share one LineageStore"
            )
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
            """CREATE TRIGGER capability_observations_immutable_update
            BEFORE UPDATE ON capability_observations
            BEGIN SELECT RAISE(ABORT, 'capability evidence is append-only'); END""",
            """CREATE TRIGGER capability_observations_immutable_delete
            BEFORE DELETE ON capability_observations
            BEGIN SELECT RAISE(ABORT, 'capability evidence is append-only'); END""",
            """CREATE TRIGGER capability_observation_matches_current_workspace
            BEFORE INSERT ON capability_observations
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
                SELECT RAISE(ABORT, 'capability evidence is not bound to the current workspace observation');
            END""",
            """CREATE TRIGGER capability_observation_time_nonregressing
            BEFORE INSERT ON capability_observations
            WHEN EXISTS (
                SELECT 1 FROM capability_observations prior
                WHERE prior.workspace_observation_id=NEW.workspace_observation_id
                  AND prior.capability=NEW.capability
                  AND prior.route_id=NEW.route_id
                  AND prior.observed_at_epoch_seconds>NEW.observed_at_epoch_seconds
            )
            BEGIN
                SELECT RAISE(ABORT, 'capability evidence observation time regressed');
            END""",
            """CREATE TRIGGER capability_route_kind_stable
            BEFORE INSERT ON capability_observations
            WHEN EXISTS (
                SELECT 1 FROM capability_observations prior
                WHERE prior.workspace_observation_id=NEW.workspace_observation_id
                  AND prior.route_id=NEW.route_id
                  AND prior.route_kind<>NEW.route_kind
            )
            BEGIN
                SELECT RAISE(ABORT, 'capability route kind changed within one environment');
            END""",
            """CREATE TRIGGER activity_capability_observation
            AFTER INSERT ON capability_observations
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'CAPABILITY_EVIDENCE_RECORDED',
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    w.song_id,
                    NULL,
                    'CAPABILITY_EVIDENCE',
                    NEW.id,
                    json_object(
                        'availability',NEW.availability,
                        'capability',NEW.capability,
                        'route_kind',NEW.route_kind
                    )
                FROM workspaces w WHERE w.id=NEW.workspace_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        present = self._table_exists("capability_observations")
        version = self._metadata_value("capability_evidence_schema_version")
        if present != (version is not None):
            raise LineageCorruptionError(
                "capability evidence schema metadata/table mismatch"
            )
        if present:
            if version != str(CAPABILITY_EVIDENCE_SCHEMA_VERSION):
                raise LineageCorruptionError(
                    f"unsupported capability evidence schema version: {version}"
                )
            return
        if not self._table_exists("workspace_observations") or not self._table_exists(
            "activity_events"
        ):
            raise LineageCorruptionError(
                "CapabilityEvidenceMemory requires WorkspaceMemory and ActivityLog first"
            )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE capability_observations (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                        workspace_observation_id TEXT NOT NULL REFERENCES workspace_observations(id),
                        host_runtime_fingerprint TEXT NOT NULL
                            CHECK(length(trim(host_runtime_fingerprint)) > 0),
                        route_id TEXT NOT NULL CHECK(length(trim(route_id)) > 0),
                        route_kind TEXT NOT NULL CHECK(route_kind IN (
                            'HOST_NATIVE','N0TE_NATIVE','OWNED_TOOL','PROVIDER','GUIDED'
                        )),
                        capability TEXT NOT NULL CHECK(length(trim(capability)) > 0),
                        display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0),
                        brand TEXT NULL,
                        availability TEXT NOT NULL CHECK(availability IN (
                            'AVAILABLE','UNAVAILABLE','UNKNOWN'
                        )),
                        evidence_kind TEXT NOT NULL CHECK(evidence_kind IN (
                            'RUNTIME_PROBE','ADAPTER_TEST','OFFICIAL_FACT',
                            'MANUAL_VERIFIED','OTHER'
                        )),
                        evidence_ref TEXT NULL,
                        observed_at_epoch_seconds INTEGER NOT NULL
                            CHECK(observed_at_epoch_seconds >= 0),
                        task_fit REAL NOT NULL CHECK(task_fit >= 0.0 AND task_fit <= 1.0),
                        editability REAL NOT NULL CHECK(editability >= 0.0 AND editability <= 1.0),
                        locality REAL NOT NULL CHECK(locality >= 0.0 AND locality <= 1.0),
                        privacy REAL NOT NULL CHECK(privacy >= 0.0 AND privacy <= 1.0),
                        latency REAL NOT NULL CHECK(latency >= 0.0 AND latency <= 1.0),
                        reversibility REAL NOT NULL CHECK(reversibility >= 0.0 AND reversibility <= 1.0),
                        cost_efficiency REAL NOT NULL CHECK(cost_efficiency >= 0.0 AND cost_efficiency <= 1.0),
                        portability REAL NOT NULL CHECK(portability >= 0.0 AND portability <= 1.0),
                        paid INTEGER NOT NULL CHECK(paid IN (0,1)),
                        CHECK(
                            availability='UNKNOWN'
                            OR (evidence_ref IS NOT NULL AND length(trim(evidence_ref)) > 0)
                        )
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX capability_observations_by_workspace "
                    "ON capability_observations(workspace_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX capability_observations_by_environment "
                    "ON capability_observations(workspace_observation_id,capability,route_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('capability_evidence_schema_version',?)",
                    (str(CAPABILITY_EVIDENCE_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError(
                "cannot initialize capability evidence schema"
            ) from exc

    @staticmethod
    def _select_columns() -> str:
        return (
            "seq,id,workspace_id,workspace_observation_id,host_runtime_fingerprint,"
            "route_id,route_kind,capability,display_name,brand,availability,"
            "evidence_kind,evidence_ref,observed_at_epoch_seconds,task_fit,editability,"
            "locality,privacy,latency,reversibility,cost_efficiency,portability,paid"
        )

    @staticmethod
    def _observation(row: sqlite3.Row) -> CapabilityObservation:
        return CapabilityObservation(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_observation_id=str(row["workspace_observation_id"]),
            host_runtime_fingerprint=str(row["host_runtime_fingerprint"]),
            route_id=str(row["route_id"]),
            route_kind=str(row["route_kind"]),
            capability=str(row["capability"]),
            display_name=str(row["display_name"]),
            brand=None if row["brand"] is None else str(row["brand"]),
            availability=str(row["availability"]),
            evidence_kind=str(row["evidence_kind"]),
            evidence_ref=None if row["evidence_ref"] is None else str(row["evidence_ref"]),
            observed_at_epoch_seconds=int(row["observed_at_epoch_seconds"]),
            task_fit=float(row["task_fit"]),
            editability=float(row["editability"]),
            locality=float(row["locality"]),
            privacy=float(row["privacy"]),
            latency=float(row["latency"]),
            reversibility=float(row["reversibility"]),
            cost_efficiency=float(row["cost_efficiency"]),
            portability=float(row["portability"]),
            paid=bool(row["paid"]),
        )

    def _rows_for_workspace(self, workspace_id: str) -> tuple[CapabilityObservation, ...]:
        return tuple(
            self._observation(row)
            for row in self._conn.execute(
                f"SELECT {self._select_columns()} FROM capability_observations "
                "WHERE workspace_id=? ORDER BY seq",
                (workspace_id,),
            )
        )

    def _validate_existing(self) -> None:
        try:
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND (name LIKE 'capability_observation%' "
                    "OR name LIKE 'capability_route_kind%' "
                    "OR name='activity_capability_observation')"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"capability evidence hooks are incomplete: {sorted(missing)}"
                )

            last_time: dict[tuple[str, str, str], int] = {}
            route_kinds: dict[tuple[str, str], str] = {}
            for row in self._conn.execute(
                f"SELECT {self._select_columns()} FROM capability_observations ORDER BY seq"
            ):
                item = self._observation(row)
                if item.route_kind not in ROUTE_KINDS:
                    raise LineageCorruptionError("capability evidence route kind is invalid")
                if item.availability not in CAPABILITY_AVAILABILITY:
                    raise LineageCorruptionError("capability evidence availability is invalid")
                if item.evidence_kind not in CAPABILITY_EVIDENCE_KINDS:
                    raise LineageCorruptionError("capability evidence kind is invalid")
                if item.availability != "UNKNOWN" and not item.evidence_ref:
                    raise LineageCorruptionError(
                        "verified capability evidence is missing its evidence reference"
                    )
                for field_name in (
                    "id",
                    "workspace_id",
                    "workspace_observation_id",
                    "host_runtime_fingerprint",
                    "route_id",
                    "capability",
                    "display_name",
                ):
                    value = str(getattr(item, field_name))
                    if not value.strip() or value != value.strip():
                        raise LineageCorruptionError(
                            f"capability evidence {field_name} is not canonical"
                        )
                if item.brand is not None and (
                    not item.brand.strip() or item.brand != item.brand.strip()
                ):
                    raise LineageCorruptionError("capability evidence brand is not canonical")
                if item.evidence_ref is not None and (
                    not item.evidence_ref.strip()
                    or item.evidence_ref != item.evidence_ref.strip()
                ):
                    raise LineageCorruptionError(
                        "capability evidence reference is not canonical"
                    )
                for value, field_name in (
                    (item.task_fit, "task_fit"),
                    (item.editability, "editability"),
                    (item.locality, "locality"),
                    (item.privacy, "privacy"),
                    (item.latency, "latency"),
                    (item.reversibility, "reversibility"),
                    (item.cost_efficiency, "cost_efficiency"),
                    (item.portability, "portability"),
                ):
                    if not 0.0 <= value <= 1.0:
                        raise LineageCorruptionError(
                            f"capability evidence {field_name} is out of range"
                        )
                binding = self._conn.execute(
                    "SELECT workspace_id,host_runtime_fingerprint "
                    "FROM workspace_observations WHERE id=?",
                    (item.workspace_observation_id,),
                ).fetchone()
                if (
                    binding is None
                    or str(binding["workspace_id"]) != item.workspace_id
                    or str(binding["host_runtime_fingerprint"])
                    != item.host_runtime_fingerprint
                ):
                    raise LineageCorruptionError(
                        "capability evidence crosses a workspace/runtime boundary"
                    )

                time_key = (item.workspace_observation_id, item.capability, item.route_id)
                prior_time = last_time.get(time_key)
                if prior_time is not None and item.observed_at_epoch_seconds < prior_time:
                    raise LineageCorruptionError(
                        "capability evidence observation time regressed"
                    )
                last_time[time_key] = item.observed_at_epoch_seconds

                route_key = (item.workspace_observation_id, item.route_id)
                prior_kind = route_kinds.get(route_key)
                if prior_kind is not None and prior_kind != item.route_kind:
                    raise LineageCorruptionError(
                        "capability route kind changed within one environment"
                    )
                route_kinds[route_key] = item.route_kind

                try:
                    candidate = item.to_candidate(
                        now_epoch_seconds=item.observed_at_epoch_seconds
                    )
                    if candidate.user_preference != 0.5:
                        raise LineageCorruptionError(
                            "capability evidence leaked artist preference"
                        )
                except LineageCorruptionError:
                    raise
                except Exception as exc:
                    raise LineageCorruptionError(
                        "capability evidence cannot reconstruct its candidate fact"
                    ) from exc
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError(
                "capability evidence state is unreadable or corrupt"
            ) from exc

    def history(self, workspace_id: str) -> tuple[CapabilityObservation, ...]:
        if self.workspaces.get(workspace_id) is None:
            raise NotFoundError(f"workspace not found: {workspace_id}")
        return self._rows_for_workspace(workspace_id)

    def state(self, workspace_id: str) -> CapabilityEnvironmentState:
        workspace_state = self.workspaces.state(workspace_id)
        current_observation = workspace_state.current_observation
        history = self._rows_for_workspace(workspace_id)
        current_history = tuple(
            item
            for item in history
            if item.workspace_observation_id == current_observation.id
            and item.host_runtime_fingerprint == current_observation.host_runtime_fingerprint
        )
        latest: dict[tuple[str, str], CapabilityObservation] = {}
        for item in current_history:
            latest[(item.capability, item.route_id)] = item
        return CapabilityEnvironmentState(
            workspace_id=workspace_state.workspace.id,
            workspace_observation_id=current_observation.id,
            host_runtime_fingerprint=current_observation.host_runtime_fingerprint,
            host_family=workspace_state.workspace.host_family,
            current=tuple(latest[key] for key in sorted(latest)),
            stale_count=len(history) - len(current_history),
        )

    def profile(
        self,
        workspace_id: str,
        *,
        now_epoch_seconds: int,
    ) -> StudioCapabilityProfile:
        state = self.state(workspace_id)
        return StudioCapabilityProfile.build(
            environment_id=state.environment_id,
            host_label=state.host_family,
            candidates=tuple(
                item.to_candidate(now_epoch_seconds=now_epoch_seconds)
                for item in state.current
            ),
        )

    def record(
        self,
        workspace_id: str,
        *,
        expected_workspace_observation_id: str,
        expected_host_runtime_fingerprint: str,
        route_id: str,
        route_kind: str,
        capability: str,
        display_name: str,
        availability: str,
        evidence_kind: str,
        observed_at_epoch_seconds: int,
        brand: str | None = None,
        evidence_ref: str | None = None,
        task_fit: float = 0.5,
        editability: float = 0.5,
        locality: float = 0.5,
        privacy: float = 0.5,
        latency: float = 0.5,
        reversibility: float = 0.5,
        cost_efficiency: float = 0.5,
        portability: float = 0.5,
        paid: bool = False,
    ) -> CapabilityObservation:
        state = self.workspaces.state(workspace_id)
        current = state.current_observation
        expected_observation = _text(
            expected_workspace_observation_id, "expected_workspace_observation_id"
        )
        expected_runtime = _text(
            expected_host_runtime_fingerprint, "expected_host_runtime_fingerprint"
        )
        if (
            current.id != expected_observation
            or current.host_runtime_fingerprint != expected_runtime
        ):
            raise CapabilityEvidenceError(
                "capability evidence was produced for a stale workspace/runtime observation"
            )

        route_id = _text(route_id, "route_id")
        route_kind = _enum(route_kind, "route_kind", ROUTE_KINDS)
        capability = _text(capability, "capability")
        display_name = _text(display_name, "display_name")
        brand = _optional_text(brand, "brand")
        availability = _enum(availability, "availability", CAPABILITY_AVAILABILITY)
        evidence_kind = _enum(
            evidence_kind, "evidence_kind", CAPABILITY_EVIDENCE_KINDS
        )
        evidence_ref = _optional_text(evidence_ref, "evidence_ref")
        observed_at = _nonnegative_int(
            observed_at_epoch_seconds, "observed_at_epoch_seconds"
        )
        paid = _bool(paid, "paid")
        scores = {
            "task_fit": _score(task_fit, "task_fit"),
            "editability": _score(editability, "editability"),
            "locality": _score(locality, "locality"),
            "privacy": _score(privacy, "privacy"),
            "latency": _score(latency, "latency"),
            "reversibility": _score(reversibility, "reversibility"),
            "cost_efficiency": _score(cost_efficiency, "cost_efficiency"),
            "portability": _score(portability, "portability"),
        }
        verified = availability != "UNKNOWN"
        compatible = availability != "UNAVAILABLE"
        if verified and evidence_ref is None:
            raise CapabilityEvidenceError(
                "AVAILABLE/UNAVAILABLE capability evidence requires evidence_ref"
            )
        try:
            CapabilityCandidate(
                candidate_id=_candidate_id(route_kind, route_id, capability),
                route_kind=route_kind,
                capability=capability,
                display_name=display_name,
                brand=brand,
                verified=verified,
                compatible=compatible,
                evidence_ref=evidence_ref,
                evidence_age_seconds=0,
                user_preference=0.5,
                paid=paid,
                **scores,
            )
        except CapabilityResolutionError as exc:
            raise CapabilityEvidenceError(
                "capability evidence cannot form a valid candidate fact"
            ) from exc

        prior_time = self._conn.execute(
            "SELECT observed_at_epoch_seconds FROM capability_observations "
            "WHERE workspace_observation_id=? AND capability=? AND route_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (current.id, capability, route_id),
        ).fetchone()
        if (
            prior_time is not None
            and observed_at < int(prior_time["observed_at_epoch_seconds"])
        ):
            raise CapabilityEvidenceError(
                "capability evidence observation time regressed for current route/capability"
            )
        prior_kind = self._conn.execute(
            "SELECT route_kind FROM capability_observations "
            "WHERE workspace_observation_id=? AND route_id=? ORDER BY seq DESC LIMIT 1",
            (current.id, route_id),
        ).fetchone()
        if prior_kind is not None and str(prior_kind["route_kind"]) != route_kind:
            raise CapabilityEvidenceError(
                "capability route kind changed within one environment"
            )

        observation_id = _new_id("capev")
        try:
            with self.store._tx():
                self._conn.execute(
                    "INSERT INTO capability_observations("
                    "id,workspace_id,workspace_observation_id,host_runtime_fingerprint,"
                    "route_id,route_kind,capability,display_name,brand,availability,"
                    "evidence_kind,evidence_ref,observed_at_epoch_seconds,task_fit,"
                    "editability,locality,privacy,latency,reversibility,cost_efficiency,"
                    "portability,paid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        state.workspace.id,
                        current.id,
                        current.host_runtime_fingerprint,
                        route_id,
                        route_kind,
                        capability,
                        display_name,
                        brand,
                        availability,
                        evidence_kind,
                        evidence_ref,
                        observed_at,
                        scores["task_fit"],
                        scores["editability"],
                        scores["locality"],
                        scores["privacy"],
                        scores["latency"],
                        scores["reversibility"],
                        scores["cost_efficiency"],
                        scores["portability"],
                        int(paid),
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise CapabilityEvidenceError(
                "could not persist capability evidence safely"
            ) from exc

        row = self._conn.execute(
            f"SELECT {self._select_columns()} FROM capability_observations WHERE id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise CapabilityEvidenceError(
                "capability evidence disappeared after persistence"
            )
        return self._observation(row)
