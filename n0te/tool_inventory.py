from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Iterable

from .capabilities import CapabilityCandidate
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .tools import (
    SemanticToolProfile,
    ToolCapabilityBinding,
    ToolEndpoint,
    ToolIdentityError,
    ToolParameterBinding,
    ToolStateBinding,
)

TOOL_INVENTORY_SCHEMA_VERSION = 1
TOOL_INVENTORY_STATES = {"ACTIVE", "RETIRED"}
TOOL_INVENTORY_SOURCE_KINDS = {
    "ARTIST_DECLARED",
    "EXPLICIT_IMPORT",
    "ARTIST_CORRECTION",
}


class ToolInventoryError(ValidationError):
    """Semantic Tool inventory state is invalid or cannot change safely."""


@dataclass(frozen=True)
class ToolInventoryRevision:
    sequence: int
    id: str
    tool_id: str
    state: str
    source_kind: str
    source_ref: str
    reason: str | None
    profile: SemanticToolProfile


@dataclass(frozen=True)
class ToolInventoryState:
    tool_id: str
    state: str
    profile: SemanticToolProfile
    latest_revision: ToolInventoryRevision


def _new_id() -> str:
    return "tir_" + uuid.uuid4().hex


def _text(value: object, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ToolInventoryError(f"{field} must not be empty")
    return text


def _source_kind(value: object) -> str:
    text = _text(value, "source_kind").upper().replace("-", "_").replace(" ", "_")
    if text not in TOOL_INVENTORY_SOURCE_KINDS:
        raise ToolInventoryError(f"unsupported tool inventory source_kind: {text}")
    return text


def _profile_payload(profile: SemanticToolProfile) -> dict[str, object]:
    if not isinstance(profile, SemanticToolProfile):
        raise TypeError("profile must be SemanticToolProfile")
    return {
        "tool_id": profile.tool_id,
        "display_name": profile.display_name,
        "endpoints": [
            {
                "endpoint_id": item.endpoint_id,
                "format_kind": item.format_kind,
                "native_identity": item.native_identity,
                "evidence_ref": item.evidence_ref,
            }
            for item in profile.endpoints
        ],
        "capabilities": [
            {
                "endpoint_id": item.endpoint_id,
                "candidate": {
                    "candidate_id": item.candidate.candidate_id,
                    "route_kind": item.candidate.route_kind,
                    "capability": item.candidate.capability,
                    "display_name": item.candidate.display_name,
                    "brand": item.candidate.brand,
                    "verified": item.candidate.verified,
                    "compatible": item.candidate.compatible,
                    "evidence_ref": item.candidate.evidence_ref,
                    "evidence_age_seconds": item.candidate.evidence_age_seconds,
                    "task_fit": item.candidate.task_fit,
                    "editability": item.candidate.editability,
                    "locality": item.candidate.locality,
                    "privacy": item.candidate.privacy,
                    "latency": item.candidate.latency,
                    "reversibility": item.candidate.reversibility,
                    "cost_efficiency": item.candidate.cost_efficiency,
                    "portability": item.candidate.portability,
                    "user_preference": item.candidate.user_preference,
                    "paid": item.candidate.paid,
                },
            }
            for item in profile.capabilities
        ],
        "parameters": [
            {
                "endpoint_id": item.endpoint_id,
                "semantic_key": item.semantic_key,
                "native_parameter_ref": item.native_parameter_ref,
                "readable": item.readable,
                "writable": item.writable,
                "evidence_ref": item.evidence_ref,
            }
            for item in profile.parameters
        ],
        "state_bindings": [
            {
                "endpoint_id": item.endpoint_id,
                "readable": item.readable,
                "writable": item.writable,
                "evidence_ref": item.evidence_ref,
            }
            for item in profile.state_bindings
        ],
    }


def _canonical_profile_json(profile: SemanticToolProfile) -> str:
    return json.dumps(
        _profile_payload(profile),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _exact_keys(value: object, expected: set[str], field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{field} shape is invalid")
    return value


def _profile_from_json(raw: str) -> SemanticToolProfile:
    payload = _exact_keys(
        json.loads(raw),
        {"tool_id", "display_name", "endpoints", "capabilities", "parameters", "state_bindings"},
        "profile",
    )
    collections = (
        payload["endpoints"],
        payload["capabilities"],
        payload["parameters"],
        payload["state_bindings"],
    )
    if not all(isinstance(item, list) for item in collections):
        raise ValueError("tool profile collections must be arrays")

    endpoints = []
    for raw_item in payload["endpoints"]:
        item = _exact_keys(
            raw_item,
            {"endpoint_id", "format_kind", "native_identity", "evidence_ref"},
            "endpoint",
        )
        endpoints.append(ToolEndpoint(**item))

    candidate_fields = {
        "candidate_id",
        "route_kind",
        "capability",
        "display_name",
        "brand",
        "verified",
        "compatible",
        "evidence_ref",
        "evidence_age_seconds",
        "task_fit",
        "editability",
        "locality",
        "privacy",
        "latency",
        "reversibility",
        "cost_efficiency",
        "portability",
        "user_preference",
        "paid",
    }
    capabilities = []
    for raw_item in payload["capabilities"]:
        item = _exact_keys(raw_item, {"endpoint_id", "candidate"}, "capability")
        candidate = CapabilityCandidate(
            **_exact_keys(item["candidate"], candidate_fields, "candidate")
        )
        capabilities.append(
            ToolCapabilityBinding(endpoint_id=item["endpoint_id"], candidate=candidate)
        )

    parameters = []
    for raw_item in payload["parameters"]:
        item = _exact_keys(
            raw_item,
            {"endpoint_id", "semantic_key", "native_parameter_ref", "readable", "writable", "evidence_ref"},
            "parameter",
        )
        parameters.append(ToolParameterBinding(**item))

    state_bindings = []
    for raw_item in payload["state_bindings"]:
        item = _exact_keys(
            raw_item,
            {"endpoint_id", "readable", "writable", "evidence_ref"},
            "state binding",
        )
        state_bindings.append(ToolStateBinding(**item))

    profile = SemanticToolProfile(
        tool_id=payload["tool_id"],
        display_name=payload["display_name"],
        endpoints=tuple(endpoints),
        capabilities=tuple(capabilities),
        parameters=tuple(parameters),
        state_bindings=tuple(state_bindings),
    )
    if _canonical_profile_json(profile) != raw:
        raise ValueError("tool profile JSON is not canonical")
    return profile


class ToolInventoryMemory:
    """Append-only explicit semantic Tool registry.

    DAW observations and filesystem discovery are not registration authority.
    They remain separate evidence surfaces until an artist explicitly declares,
    imports, or corrects semantic identity.
    """

    _TRIGGER_NAMES = {
        "tool_inventory_revisions_immutable_update",
        "tool_inventory_revisions_immutable_delete",
        "tool_inventory_revision_activity",
    }

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("ToolInventoryMemory requires the canonical LineageStore")
        self.store = store
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
            """CREATE TRIGGER tool_inventory_revisions_immutable_update
            BEFORE UPDATE ON tool_inventory_revisions
            BEGIN SELECT RAISE(ABORT, 'Tool inventory revisions are append-only'); END""",
            """CREATE TRIGGER tool_inventory_revisions_immutable_delete
            BEFORE DELETE ON tool_inventory_revisions
            BEGIN SELECT RAISE(ABORT, 'Tool inventory revisions are append-only'); END""",
            """CREATE TRIGGER tool_inventory_revision_activity
            AFTER INSERT ON tool_inventory_revisions
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                ) VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'TOOL_INVENTORY_'||NEW.state,
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    NULL,
                    NULL,
                    'SEMANTIC_TOOL_REVISION',
                    NEW.id,
                    json_object('tool_id',NEW.tool_id,'source_kind',NEW.source_kind,'state',NEW.state)
                );
            END""",
        )

    def _ensure_schema(self) -> None:
        present = self._table_exists("tool_inventory_revisions")
        version = self._metadata_value("tool_inventory_schema_version")
        if present or version is not None:
            if not present or version != str(TOOL_INVENTORY_SCHEMA_VERSION):
                raise LineageCorruptionError("Tool inventory schema metadata/table mismatch")
            return
        if not self._table_exists("activity_events"):
            raise LineageCorruptionError("ToolInventoryMemory requires ActivityLog first")
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE tool_inventory_revisions (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        tool_id TEXT NOT NULL CHECK(length(trim(tool_id)) > 0),
                        state TEXT NOT NULL CHECK(state IN ('ACTIVE','RETIRED')),
                        source_kind TEXT NOT NULL CHECK(source_kind IN (
                            'ARTIST_DECLARED','EXPLICIT_IMPORT','ARTIST_CORRECTION'
                        )),
                        source_ref TEXT NOT NULL CHECK(length(trim(source_ref)) > 0),
                        reason TEXT NULL,
                        profile_json TEXT NOT NULL CHECK(length(profile_json) > 2)
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX tool_inventory_history ON tool_inventory_revisions(tool_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('tool_inventory_schema_version',?)",
                    (str(TOOL_INVENTORY_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Tool inventory") from exc

    def _revision(self, row: sqlite3.Row) -> ToolInventoryRevision:
        profile = _profile_from_json(str(row["profile_json"]))
        if profile.tool_id != str(row["tool_id"]):
            raise LineageCorruptionError("Tool inventory revision/profile identity mismatch")
        return ToolInventoryRevision(
            sequence=int(row["seq"]),
            id=str(row["id"]),
            tool_id=str(row["tool_id"]),
            state=str(row["state"]),
            source_kind=str(row["source_kind"]),
            source_ref=str(row["source_ref"]),
            reason=None if row["reason"] is None else str(row["reason"]),
            profile=profile,
        )

    def _latest_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._conn.execute(
                """SELECT r.seq,r.id,r.tool_id,r.state,r.source_kind,r.source_ref,r.reason,r.profile_json
                   FROM tool_inventory_revisions r
                   JOIN (
                       SELECT tool_id,MAX(seq) AS max_seq
                       FROM tool_inventory_revisions GROUP BY tool_id
                   ) latest ON latest.tool_id=r.tool_id AND latest.max_seq=r.seq
                   ORDER BY r.tool_id"""
            ).fetchall()
        )

    @staticmethod
    def _validate_active_uniqueness(
        revisions: Iterable[ToolInventoryRevision],
        *,
        corruption: bool = False,
    ) -> None:
        error = LineageCorruptionError if corruption else ToolInventoryError
        endpoint_ids: dict[str, str] = {}
        native_keys: dict[tuple[str, str], str] = {}
        candidate_ids: dict[str, str] = {}
        for revision in revisions:
            if revision.state != "ACTIVE":
                continue
            profile = revision.profile
            for endpoint in profile.endpoints:
                owner = endpoint_ids.setdefault(endpoint.endpoint_id, profile.tool_id)
                if owner != profile.tool_id:
                    raise error(f"active tool endpoint_id collision: {endpoint.endpoint_id}")
                key = (endpoint.format_kind, endpoint.native_identity)
                owner = native_keys.setdefault(key, profile.tool_id)
                if owner != profile.tool_id:
                    raise error("active tools claim the same format/native endpoint identity")
            for binding in profile.capabilities:
                candidate_id = binding.candidate.candidate_id
                owner = candidate_ids.setdefault(candidate_id, profile.tool_id)
                if owner != profile.tool_id:
                    raise error(
                        f"active tool capability candidate collision: {candidate_id}"
                    )

    def _validate_existing(self) -> None:
        try:
            if self._metadata_value("tool_inventory_schema_version") != str(
                TOOL_INVENTORY_SCHEMA_VERSION
            ):
                raise LineageCorruptionError("unsupported Tool inventory schema version")
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'tool_inventory_%'"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"Tool inventory integrity hooks are incomplete: {sorted(missing)}"
                )
            for row in self._conn.execute(
                """SELECT seq,id,tool_id,state,source_kind,source_ref,reason,profile_json
                   FROM tool_inventory_revisions ORDER BY seq"""
            ):
                revision = self._revision(row)
                if revision.state not in TOOL_INVENTORY_STATES:
                    raise LineageCorruptionError("Tool inventory revision state is invalid")
                if revision.source_kind not in TOOL_INVENTORY_SOURCE_KINDS:
                    raise LineageCorruptionError("Tool inventory revision source is invalid")
                if revision.source_kind == "ARTIST_CORRECTION" and not revision.reason:
                    raise LineageCorruptionError("Tool inventory correction lacks reason")
            latest = tuple(self._revision(row) for row in self._latest_rows())
            self._validate_active_uniqueness(latest, corruption=True)
        except LineageCorruptionError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError, ToolIdentityError) as exc:
            raise LineageCorruptionError("Tool inventory is unreadable or corrupt") from exc

    def _rows_for(self, tool_id: str) -> tuple[sqlite3.Row, ...]:
        tool_id = _text(tool_id, "tool_id")
        return tuple(
            self._conn.execute(
                """SELECT seq,id,tool_id,state,source_kind,source_ref,reason,profile_json
                   FROM tool_inventory_revisions WHERE tool_id=? ORDER BY seq""",
                (tool_id,),
            ).fetchall()
        )

    def history(self, tool_id: str) -> tuple[ToolInventoryRevision, ...]:
        return tuple(self._revision(row) for row in self._rows_for(tool_id))

    def state(self, tool_id: str) -> ToolInventoryState:
        rows = self._rows_for(tool_id)
        if not rows:
            raise NotFoundError(f"unknown semantic tool: {str(tool_id).strip()}")
        latest = self._revision(rows[-1])
        return ToolInventoryState(
            tool_id=latest.tool_id,
            state=latest.state,
            profile=latest.profile,
            latest_revision=latest,
        )

    def active_profiles(self) -> tuple[SemanticToolProfile, ...]:
        revisions = tuple(self._revision(row) for row in self._latest_rows())
        return tuple(item.profile for item in revisions if item.state == "ACTIVE")

    def _validate_candidate_state(
        self,
        profile: SemanticToolProfile,
        *,
        replacing_tool_id: str | None = None,
    ) -> None:
        if not isinstance(profile, SemanticToolProfile):
            raise TypeError("profile must be SemanticToolProfile")
        revisions = []
        for row in self._latest_rows():
            revision = self._revision(row)
            if replacing_tool_id is not None and revision.tool_id == replacing_tool_id:
                continue
            revisions.append(revision)
        revisions.append(
            ToolInventoryRevision(
                sequence=0,
                id="candidate",
                tool_id=profile.tool_id,
                state="ACTIVE",
                source_kind="ARTIST_DECLARED",
                source_ref="validation",
                reason=None,
                profile=profile,
            )
        )
        self._validate_active_uniqueness(revisions)

    def _append(
        self,
        profile: SemanticToolProfile,
        *,
        state: str,
        source_kind: str,
        source_ref: str,
        reason: str | None,
    ) -> ToolInventoryRevision:
        revision_id = _new_id()
        profile_json = _canonical_profile_json(profile)
        try:
            with self.store._tx():
                self._conn.execute(
                    """INSERT INTO tool_inventory_revisions(
                        id,tool_id,state,source_kind,source_ref,reason,profile_json
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        revision_id,
                        profile.tool_id,
                        state,
                        source_kind,
                        source_ref,
                        reason,
                        profile_json,
                    ),
                )
        except sqlite3.DatabaseError as exc:
            raise ToolInventoryError("cannot append Tool inventory revision") from exc
        row = self._conn.execute(
            """SELECT seq,id,tool_id,state,source_kind,source_ref,reason,profile_json
               FROM tool_inventory_revisions WHERE id=?""",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("Tool inventory revision disappeared")
        return self._revision(row)

    def register(
        self,
        profile: SemanticToolProfile,
        *,
        source_kind: str,
        source_ref: str,
    ) -> ToolInventoryRevision:
        if not isinstance(profile, SemanticToolProfile):
            raise TypeError("profile must be SemanticToolProfile")
        source = _source_kind(source_kind)
        if source == "ARTIST_CORRECTION":
            raise ToolInventoryError(
                "ARTIST_CORRECTION requires correct(), not initial registration"
            )
        if self._rows_for(profile.tool_id):
            raise ToolInventoryError(
                f"semantic tool already exists: {profile.tool_id}; use correct()"
            )
        source_ref = _text(source_ref, "source_ref")
        self._validate_candidate_state(profile)
        return self._append(
            profile,
            state="ACTIVE",
            source_kind=source,
            source_ref=source_ref,
            reason=None,
        )

    def correct(
        self,
        profile: SemanticToolProfile,
        *,
        source_ref: str,
        reason: str,
    ) -> ToolInventoryRevision:
        if not isinstance(profile, SemanticToolProfile):
            raise TypeError("profile must be SemanticToolProfile")
        if not self._rows_for(profile.tool_id):
            raise NotFoundError(f"unknown semantic tool: {profile.tool_id}")
        source_ref = _text(source_ref, "source_ref")
        reason = _text(reason, "reason")
        self._validate_candidate_state(profile, replacing_tool_id=profile.tool_id)
        return self._append(
            profile,
            state="ACTIVE",
            source_kind="ARTIST_CORRECTION",
            source_ref=source_ref,
            reason=reason,
        )

    def retire(
        self,
        tool_id: str,
        *,
        source_ref: str,
        reason: str,
    ) -> ToolInventoryRevision:
        current = self.state(tool_id)
        if current.state != "ACTIVE":
            raise ToolInventoryError(f"semantic tool is already retired: {current.tool_id}")
        return self._append(
            current.profile,
            state="RETIRED",
            source_kind="ARTIST_CORRECTION",
            source_ref=_text(source_ref, "source_ref"),
            reason=_text(reason, "reason"),
        )

    def by_native_identity(
        self,
        format_kind: str,
        native_identity: str,
    ) -> tuple[SemanticToolProfile, ToolEndpoint] | None:
        format_name = _text(format_kind, "format_kind").upper()
        native = _text(native_identity, "native_identity")
        for profile in self.active_profiles():
            for endpoint in profile.endpoints:
                if endpoint.format_kind == format_name and endpoint.native_identity == native:
                    return profile, endpoint
        return None

    def candidates(self) -> tuple[CapabilityCandidate, ...]:
        candidates = [
            candidate
            for profile in self.active_profiles()
            for candidate in profile.candidates()
        ]
        return tuple(sorted(candidates, key=lambda item: item.candidate_id))
