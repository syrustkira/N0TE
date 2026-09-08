from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .shadow import HostShadow, HostShadowError, ShadowFact, SHADOW_OBJECT_KINDS
from .twins import TwinEvidenceService
from .workspace import WorkspaceMemory

RECONCILIATION_SCHEMA_VERSION = 1
COMPARISON_STATUSES = {"MATCH", "HOST_ONLY", "SONG_ONLY", "CONFLICT", "UNRESOLVED"}
CASE_STATES = {"OPEN", "DECIDED", "STALE"}
RECONCILIATION_CHOICES = {
    "UPDATE_SONG",
    "RESTORE_HOST",
    "KEEP_WORKSPACE_SPECIFIC",
    "DO_NOTHING",
}


class ReconciliationError(RuntimeError):
    """Invalid or unsafe Song↔workspace reconciliation operation."""


@dataclass(frozen=True)
class ReconciliationTarget:
    canonical_key: str
    shadow_object_kind: str
    shadow_object_ref: str
    shadow_field: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_key", _text(self.canonical_key, "canonical_key"))
        kind = _text(self.shadow_object_kind, "shadow_object_kind").upper()
        if kind not in SHADOW_OBJECT_KINDS:
            raise ReconciliationError(f"unsupported Host Shadow object kind: {kind}")
        object.__setattr__(self, "shadow_object_kind", kind)
        object.__setattr__(
            self, "shadow_object_ref", _text(self.shadow_object_ref, "shadow_object_ref")
        )
        object.__setattr__(self, "shadow_field", _text(self.shadow_field, "shadow_field"))


@dataclass(frozen=True)
class ReconciliationComparison:
    status: str
    song_id: str
    workspace_id: str
    version_id: str | None
    target: ReconciliationTarget
    canonical_claim_ids: tuple[str, ...]
    canonical_values: tuple[Any, ...]
    host_fact: ShadowFact | None
    host_baseline_batch_id: str
    host_latest_batch_id: str

    @property
    def needs_decision(self) -> bool:
        return self.status != "MATCH"


@dataclass(frozen=True)
class ReconciliationCase:
    id: str
    sequence: int
    song_id: str
    workspace_id: str
    version_id: str | None
    comparison_status: str
    target: ReconciliationTarget
    canonical_claim_ids: tuple[str, ...]
    canonical_values: tuple[Any, ...]
    host_fact: ShadowFact | None
    host_baseline_batch_id: str
    host_latest_batch_id: str


@dataclass(frozen=True)
class ReconciliationDecision:
    id: str
    sequence: int
    case_id: str
    choice: str
    evidence_ref: str
    rationale: str | None


@dataclass(frozen=True)
class ReconciliationCaseState:
    status: str
    case: ReconciliationCase
    decisions: tuple[ReconciliationDecision, ...]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ReconciliationError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


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
        raise ReconciliationError("reconciliation value must be canonical JSON data") from exc


def _canonical_unique(values: tuple[Any, ...]) -> tuple[Any, ...]:
    selected: dict[str, Any] = {}
    for value in values:
        selected.setdefault(_canonical_json(value), value)
    return tuple(selected[key] for key in sorted(selected))


class ReconciliationService:
    """Compare canonical Technical Twin evidence with CURRENT Host Shadow and record choices."""

    _TRIGGER_NAMES = {
        "reconciliation_cases_immutable_update",
        "reconciliation_cases_immutable_delete",
        "reconciliation_decisions_immutable_update",
        "reconciliation_decisions_immutable_delete",
        "reconciliation_case_workspace_song_match",
        "activity_reconciliation_case",
        "activity_reconciliation_decision",
    }

    def __init__(
        self,
        store: LineageStore,
        twins: TwinEvidenceService,
        shadow: HostShadow,
    ):
        if not isinstance(store, LineageStore):
            raise TypeError("ReconciliationService requires the canonical LineageStore")
        if not isinstance(twins, TwinEvidenceService):
            raise TypeError("ReconciliationService requires TwinEvidenceService")
        if not isinstance(shadow, HostShadow):
            raise TypeError("ReconciliationService requires HostShadow")
        if twins.store is not store or shadow.store is not store:
            raise TypeError("reconciliation dependencies must share one LineageStore")
        self.store = store
        self.twins = twins
        self.shadow = shadow
        self.workspaces: WorkspaceMemory = shadow.workspaces
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
            """CREATE TRIGGER reconciliation_cases_immutable_update
            BEFORE UPDATE ON reconciliation_cases
            BEGIN SELECT RAISE(ABORT, 'reconciliation cases are append-only'); END""",
            """CREATE TRIGGER reconciliation_cases_immutable_delete
            BEFORE DELETE ON reconciliation_cases
            BEGIN SELECT RAISE(ABORT, 'reconciliation cases are append-only'); END""",
            """CREATE TRIGGER reconciliation_decisions_immutable_update
            BEFORE UPDATE ON reconciliation_decisions
            BEGIN SELECT RAISE(ABORT, 'reconciliation decisions are append-only'); END""",
            """CREATE TRIGGER reconciliation_decisions_immutable_delete
            BEFORE DELETE ON reconciliation_decisions
            BEGIN SELECT RAISE(ABORT, 'reconciliation decisions are append-only'); END""",
            """CREATE TRIGGER reconciliation_case_workspace_song_match
            BEFORE INSERT ON reconciliation_cases
            WHEN NOT EXISTS (
                SELECT 1 FROM workspaces w
                WHERE w.id=NEW.workspace_id AND w.song_id=NEW.song_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'reconciliation workspace belongs to a different Song');
            END""",
            """CREATE TRIGGER activity_reconciliation_case
            AFTER INSERT ON reconciliation_cases
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                VALUES(
                    'act_'||lower(hex(randomblob(16))),
                    'RECONCILIATION_CASE_OPENED',
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    NEW.song_id,
                    NEW.version_id,
                    'RECONCILIATION_CASE',
                    NEW.id,
                    json_object('comparison_status',NEW.comparison_status)
                );
            END""",
            """CREATE TRIGGER activity_reconciliation_decision
            AFTER INSERT ON reconciliation_decisions
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'RECONCILIATION_DECISION_RECORDED',
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    c.song_id,
                    c.version_id,
                    'RECONCILIATION_CASE',
                    c.id,
                    json_object('choice',NEW.choice)
                FROM reconciliation_cases c WHERE c.id=NEW.case_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        table_names = ("reconciliation_cases", "reconciliation_decisions")
        present = [self._table_exists(name) for name in table_names]
        version = self._metadata_value("reconciliation_schema_version")
        if len(set(present)) != 1 or present[0] != (version is not None):
            raise LineageCorruptionError("reconciliation schema metadata/table mismatch")
        if present[0]:
            if version != str(RECONCILIATION_SCHEMA_VERSION):
                raise LineageCorruptionError(
                    f"unsupported reconciliation schema version: {version}"
                )
            return
        for required in (
            "workspaces",
            "host_shadow_batches",
            "evidence_claims",
            "activity_events",
        ):
            if not self._table_exists(required):
                raise LineageCorruptionError(
                    f"ReconciliationService requires {required} first"
                )
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE reconciliation_cases (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        song_id TEXT NOT NULL REFERENCES songs(id),
                        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                        version_id TEXT NULL REFERENCES versions(id),
                        comparison_status TEXT NOT NULL CHECK(comparison_status IN (
                            'HOST_ONLY','SONG_ONLY','CONFLICT','UNRESOLVED'
                        )),
                        canonical_key TEXT NOT NULL CHECK(length(trim(canonical_key)) > 0),
                        shadow_object_kind TEXT NOT NULL CHECK(length(trim(shadow_object_kind)) > 0),
                        shadow_object_ref TEXT NOT NULL CHECK(length(trim(shadow_object_ref)) > 0),
                        shadow_field TEXT NOT NULL CHECK(length(trim(shadow_field)) > 0),
                        canonical_claim_ids_json TEXT NOT NULL,
                        canonical_values_json TEXT NOT NULL,
                        host_fact_json TEXT NULL,
                        host_baseline_batch_id TEXT NOT NULL REFERENCES host_shadow_batches(id),
                        host_latest_batch_id TEXT NOT NULL REFERENCES host_shadow_batches(id)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE reconciliation_decisions (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        case_id TEXT NOT NULL REFERENCES reconciliation_cases(id),
                        choice TEXT NOT NULL CHECK(choice IN (
                            'UPDATE_SONG','RESTORE_HOST',
                            'KEEP_WORKSPACE_SPECIFIC','DO_NOTHING'
                        )),
                        evidence_ref TEXT NOT NULL CHECK(length(trim(evidence_ref)) > 0),
                        rationale TEXT NULL
                    )"""
                )
                self._conn.execute(
                    "CREATE INDEX reconciliation_cases_by_workspace "
                    "ON reconciliation_cases(workspace_id,seq)"
                )
                self._conn.execute(
                    "CREATE INDEX reconciliation_decisions_by_case "
                    "ON reconciliation_decisions(case_id,seq)"
                )
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('reconciliation_schema_version',?)",
                    (str(RECONCILIATION_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize reconciliation schema") from exc

    @staticmethod
    def _host_fact_payload(fact: ShadowFact | None) -> dict[str, Any] | None:
        if fact is None:
            return None
        return {
            "object_kind": fact.object_kind,
            "object_ref": fact.object_ref,
            "field": fact.field,
            "value": fact.value,
            "batch_id": fact.batch_id,
            "actor": fact.actor,
            "evidence_ref": fact.evidence_ref,
        }

    @staticmethod
    def _host_fact_from_payload(payload: dict[str, Any] | None) -> ShadowFact | None:
        if payload is None:
            return None
        required = {
            "object_kind",
            "object_ref",
            "field",
            "value",
            "batch_id",
            "actor",
            "evidence_ref",
        }
        if set(payload) != required:
            raise LineageCorruptionError("reconciliation host fact snapshot shape is invalid")
        return ShadowFact(
            object_kind=str(payload["object_kind"]),
            object_ref=str(payload["object_ref"]),
            field=str(payload["field"]),
            value=payload["value"],
            batch_id=str(payload["batch_id"]),
            actor=str(payload["actor"]),
            evidence_ref=str(payload["evidence_ref"]),
        )

    def _comparison_inputs(
        self,
        *,
        song_id: str,
        workspace_id: str,
        version_id: str | None,
        target: ReconciliationTarget,
    ) -> tuple[tuple[Any, ...], tuple[str, ...], ShadowFact | None, str, str]:
        if not isinstance(target, ReconciliationTarget):
            raise TypeError("target must be ReconciliationTarget")
        workspace = self.workspaces._require(workspace_id)
        if workspace.song_id != str(song_id):
            raise ValidationError("workspace belongs to a different Song")
        shadow_state = self.shadow.require_current(workspace.id)

        twin_view = self.twins.for_song(song_id=song_id, version_id=version_id)
        claims = tuple(
            claim
            for claim in twin_view.technical_claims
            if claim.key == target.canonical_key
        )
        claim_ids = tuple(claim.id for claim in claims)
        canonical_values = _canonical_unique(tuple(claim.value for claim in claims))

        host_fact = next(
            (
                fact
                for fact in shadow_state.facts
                if (
                    fact.object_kind == target.shadow_object_kind
                    and fact.object_ref == target.shadow_object_ref
                    and fact.field == target.shadow_field
                )
            ),
            None,
        )
        assert shadow_state.baseline_batch_id is not None
        assert shadow_state.latest_batch_id is not None
        return (
            canonical_values,
            claim_ids,
            host_fact,
            shadow_state.baseline_batch_id,
            shadow_state.latest_batch_id,
        )

    def compare(
        self,
        *,
        song_id: str,
        workspace_id: str,
        target: ReconciliationTarget,
        version_id: str | None = None,
    ) -> ReconciliationComparison:
        (
            canonical_values,
            claim_ids,
            host_fact,
            baseline_batch_id,
            latest_batch_id,
        ) = self._comparison_inputs(
            song_id=song_id,
            workspace_id=workspace_id,
            version_id=version_id,
            target=target,
        )

        if len(canonical_values) > 1:
            status = "UNRESOLVED"
        elif not canonical_values and host_fact is None:
            status = "MATCH"
        elif not canonical_values:
            status = "HOST_ONLY"
        elif host_fact is None:
            status = "SONG_ONLY"
        elif _canonical_json(canonical_values[0]) == _canonical_json(host_fact.value):
            status = "MATCH"
        else:
            status = "CONFLICT"

        return ReconciliationComparison(
            status=status,
            song_id=str(song_id),
            workspace_id=str(workspace_id),
            version_id=None if version_id is None else str(version_id),
            target=target,
            canonical_claim_ids=claim_ids,
            canonical_values=canonical_values,
            host_fact=host_fact,
            host_baseline_batch_id=baseline_batch_id,
            host_latest_batch_id=latest_batch_id,
        )

    @staticmethod
    def _case(row: sqlite3.Row) -> ReconciliationCase:
        try:
            claim_ids = tuple(json.loads(str(row["canonical_claim_ids_json"])))
            canonical_values = tuple(json.loads(str(row["canonical_values_json"])))
            host_payload = (
                None
                if row["host_fact_json"] is None
                else json.loads(str(row["host_fact_json"]))
            )
        except Exception as exc:
            raise LineageCorruptionError("reconciliation case snapshot JSON is invalid") from exc
        if not all(isinstance(item, str) for item in claim_ids):
            raise LineageCorruptionError("reconciliation claim snapshot is invalid")
        target = ReconciliationTarget(
            canonical_key=str(row["canonical_key"]),
            shadow_object_kind=str(row["shadow_object_kind"]),
            shadow_object_ref=str(row["shadow_object_ref"]),
            shadow_field=str(row["shadow_field"]),
        )
        return ReconciliationCase(
            id=str(row["id"]),
            sequence=int(row["seq"]),
            song_id=str(row["song_id"]),
            workspace_id=str(row["workspace_id"]),
            version_id=None if row["version_id"] is None else str(row["version_id"]),
            comparison_status=str(row["comparison_status"]),
            target=target,
            canonical_claim_ids=claim_ids,
            canonical_values=canonical_values,
            host_fact=ReconciliationService._host_fact_from_payload(host_payload),
            host_baseline_batch_id=str(row["host_baseline_batch_id"]),
            host_latest_batch_id=str(row["host_latest_batch_id"]),
        )

    @staticmethod
    def _decision(row: sqlite3.Row) -> ReconciliationDecision:
        return ReconciliationDecision(
            id=str(row["id"]),
            sequence=int(row["seq"]),
            case_id=str(row["case_id"]),
            choice=str(row["choice"]),
            evidence_ref=str(row["evidence_ref"]),
            rationale=None if row["rationale"] is None else str(row["rationale"]),
        )

    def open_case(
        self,
        *,
        song_id: str,
        workspace_id: str,
        target: ReconciliationTarget,
        version_id: str | None = None,
    ) -> ReconciliationCase:
        comparison = self.compare(
            song_id=song_id,
            workspace_id=workspace_id,
            version_id=version_id,
            target=target,
        )
        if comparison.status == "MATCH":
            raise ReconciliationError("matching Song/workspace facts do not require a reconciliation case")
        case_id = _new_id("rcase")
        host_json = (
            None
            if comparison.host_fact is None
            else _canonical_json(self._host_fact_payload(comparison.host_fact))
        )
        with self.store._tx():
            self._conn.execute(
                """INSERT INTO reconciliation_cases(
                    id,song_id,workspace_id,version_id,comparison_status,
                    canonical_key,shadow_object_kind,shadow_object_ref,shadow_field,
                    canonical_claim_ids_json,canonical_values_json,host_fact_json,
                    host_baseline_batch_id,host_latest_batch_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    case_id,
                    comparison.song_id,
                    comparison.workspace_id,
                    comparison.version_id,
                    comparison.status,
                    comparison.target.canonical_key,
                    comparison.target.shadow_object_kind,
                    comparison.target.shadow_object_ref,
                    comparison.target.shadow_field,
                    _canonical_json(list(comparison.canonical_claim_ids)),
                    _canonical_json(list(comparison.canonical_values)),
                    host_json,
                    comparison.host_baseline_batch_id,
                    comparison.host_latest_batch_id,
                ),
            )
        return self.get_case(case_id)

    def get_case(self, case_id: str) -> ReconciliationCase:
        row = self._conn.execute(
            """SELECT seq,id,song_id,workspace_id,version_id,comparison_status,
                      canonical_key,shadow_object_kind,shadow_object_ref,shadow_field,
                      canonical_claim_ids_json,canonical_values_json,host_fact_json,
                      host_baseline_batch_id,host_latest_batch_id
               FROM reconciliation_cases WHERE id=?""",
            (str(case_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"reconciliation case not found: {case_id}")
        return self._case(row)

    def decisions(self, case_id: str) -> tuple[ReconciliationDecision, ...]:
        self.get_case(case_id)
        return tuple(
            self._decision(row)
            for row in self._conn.execute(
                """SELECT seq,id,case_id,choice,evidence_ref,rationale
                   FROM reconciliation_decisions WHERE case_id=? ORDER BY seq""",
                (case_id,),
            )
        )

    def state(self, case_id: str) -> ReconciliationCaseState:
        case = self.get_case(case_id)
        try:
            current = self.compare(
                song_id=case.song_id,
                workspace_id=case.workspace_id,
                version_id=case.version_id,
                target=case.target,
            )
        except HostShadowError:
            return ReconciliationCaseState(
                status="STALE",
                case=case,
                decisions=self.decisions(case.id),
            )
        if (
            current.canonical_claim_ids != case.canonical_claim_ids
            or current.host_baseline_batch_id != case.host_baseline_batch_id
            or current.host_latest_batch_id != case.host_latest_batch_id
        ):
            status = "STALE"
        else:
            status = "DECIDED" if self.decisions(case.id) else "OPEN"
        return ReconciliationCaseState(
            status=status,
            case=case,
            decisions=self.decisions(case.id),
        )

    def record_decision(
        self,
        case_id: str,
        *,
        choice: str,
        evidence_ref: str,
        rationale: str | None = None,
    ) -> ReconciliationDecision:
        current = self.state(case_id)
        if current.status == "STALE":
            raise ReconciliationError("stale reconciliation case must be reopened before deciding")
        choice_name = _text(choice, "choice").upper().replace("-", "_").replace(" ", "_")
        if choice_name not in RECONCILIATION_CHOICES:
            raise ReconciliationError(f"unsupported reconciliation choice: {choice_name}")
        case = current.case
        if choice_name == "UPDATE_SONG" and case.host_fact is None:
            raise ReconciliationError("UPDATE_SONG requires a snapped host fact")
        if choice_name == "RESTORE_HOST" and len(case.canonical_values) != 1:
            raise ReconciliationError(
                "RESTORE_HOST requires exactly one snapped canonical technical value"
            )
        evidence = _text(evidence_ref, "evidence_ref")
        rationale_text = _optional_text(rationale, "rationale")
        decision_id = _new_id("rdec")
        with self.store._tx():
            self._conn.execute(
                """INSERT INTO reconciliation_decisions(
                    id,case_id,choice,evidence_ref,rationale
                ) VALUES(?,?,?,?,?)""",
                (decision_id, case_id, choice_name, evidence, rationale_text),
            )
        row = self._conn.execute(
            """SELECT seq,id,case_id,choice,evidence_ref,rationale
               FROM reconciliation_decisions WHERE id=?""",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("reconciliation decision disappeared")
        return self._decision(row)

    def unresolved_for_workspace(
        self, workspace_id: str
    ) -> tuple[ReconciliationCaseState, ...]:
        self.workspaces._require(workspace_id)
        rows = self._conn.execute(
            "SELECT id FROM reconciliation_cases WHERE workspace_id=? ORDER BY seq",
            (workspace_id,),
        ).fetchall()
        return tuple(
            state
            for state in (self.state(str(row["id"])) for row in rows)
            if state.status in {"OPEN", "DECIDED", "STALE"}
        )

    def _validate_existing(self) -> None:
        try:
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND (name LIKE 'reconciliation_%' OR name LIKE 'activity_reconciliation_%')"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(
                    f"reconciliation hooks are incomplete: {sorted(missing)}"
                )

            case_rows = self._conn.execute(
                """SELECT seq,id,song_id,workspace_id,version_id,comparison_status,
                          canonical_key,shadow_object_kind,shadow_object_ref,shadow_field,
                          canonical_claim_ids_json,canonical_values_json,host_fact_json,
                          host_baseline_batch_id,host_latest_batch_id
                   FROM reconciliation_cases ORDER BY seq"""
            ).fetchall()
            for row in case_rows:
                case = self._case(row)
                if case.comparison_status not in COMPARISON_STATUSES - {"MATCH"}:
                    raise LineageCorruptionError("reconciliation case status is invalid")
                workspace = self.workspaces._require(case.workspace_id)
                if workspace.song_id != case.song_id:
                    raise LineageCorruptionError("reconciliation case crosses Song boundary")
                if case.version_id is not None:
                    version = self.store.get_version(case.version_id)
                    if version is None or version.song_id != case.song_id:
                        raise LineageCorruptionError("reconciliation case version crosses Song boundary")
                for claim_id in case.canonical_claim_ids:
                    claim = self.twins.memory.get_claim(claim_id)
                    if claim is None:
                        raise LineageCorruptionError(
                            "reconciliation case references missing evidence claim"
                        )
                    if claim.key != case.target.canonical_key:
                        raise LineageCorruptionError(
                            "reconciliation case claim key does not match its target"
                        )
                    if getattr(claim, "twin_domain", "TECHNICAL") != "TECHNICAL":
                        raise LineageCorruptionError(
                            "reconciliation case may snapshot only TECHNICAL claims"
                        )
                    if claim.scope_kind == "SONG" and claim.scope_id != case.song_id:
                        raise LineageCorruptionError(
                            "reconciliation case claim crosses Song boundary"
                        )
                    if claim.scope_kind == "VERSION":
                        version = self.store.get_version(claim.scope_id)
                        if version is None or version.song_id != case.song_id:
                            raise LineageCorruptionError(
                                "reconciliation case version claim crosses Song boundary"
                            )
                if _canonical_json(list(case.canonical_claim_ids)) != str(
                    row["canonical_claim_ids_json"]
                ):
                    raise LineageCorruptionError(
                        "reconciliation claim snapshot JSON is not canonical"
                    )
                if _canonical_json(list(case.canonical_values)) != str(
                    row["canonical_values_json"]
                ):
                    raise LineageCorruptionError(
                        "reconciliation value snapshot JSON is not canonical"
                    )
                if case.host_fact is not None:
                    if _canonical_json(self._host_fact_payload(case.host_fact)) != str(
                        row["host_fact_json"]
                    ):
                        raise LineageCorruptionError(
                            "reconciliation host fact snapshot JSON is not canonical"
                        )
                    fact_batch = self._conn.execute(
                        "SELECT workspace_id FROM host_shadow_batches WHERE id=?",
                        (case.host_fact.batch_id,),
                    ).fetchone()
                    if (
                        fact_batch is None
                        or str(fact_batch["workspace_id"]) != case.workspace_id
                    ):
                        raise LineageCorruptionError(
                            "reconciliation host fact references a foreign Host Shadow batch"
                        )
                for batch_id in (
                    case.host_baseline_batch_id,
                    case.host_latest_batch_id,
                ):
                    batch = self._conn.execute(
                        "SELECT workspace_id FROM host_shadow_batches WHERE id=?",
                        (batch_id,),
                    ).fetchone()
                    if batch is None or str(batch["workspace_id"]) != case.workspace_id:
                        raise LineageCorruptionError(
                            "reconciliation case references a foreign Host Shadow batch"
                        )

            decision_rows = self._conn.execute(
                """SELECT seq,id,case_id,choice,evidence_ref,rationale
                   FROM reconciliation_decisions ORDER BY seq"""
            ).fetchall()
            for row in decision_rows:
                decision = self._decision(row)
                if decision.choice not in RECONCILIATION_CHOICES:
                    raise LineageCorruptionError("reconciliation decision choice is invalid")
                _text(decision.evidence_ref, "evidence_ref")
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError(
                "reconciliation state is unreadable or corrupt"
            ) from exc
