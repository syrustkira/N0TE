from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Protocol

from .lineage import LineageCorruptionError
from .operations import OperationJournal, OperationRecord

TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_STATUSES = {"COMPLETE", "COMPENSATED", "RECOVERY_REQUIRED", "UNKNOWN"}
STEP_EXECUTION_STATUSES = {"SUCCEEDED", "FAILED", "UNKNOWN"}
CHANGE_STATES = {"APPLIED", "NOT_APPLIED", "UNKNOWN"}
POSTCONDITION_STATUSES = {"SATISFIED", "FAILED", "UNKNOWN"}
COMPENSATION_STATUSES = {"RESTORED", "FAILED", "UNKNOWN"}
TRANSACTION_EVENT_TYPES = {
    "PLAN_REGISTERED",
    "SNAPSHOT_CAPTURED",
    "SAFE_FAILURE",
    "STEP_EXECUTION_STARTED",
    "STEP_EXECUTION_RECORDED",
    "POSTCONDITION_RECORDED",
    "COMPENSATION_STARTED",
    "COMPENSATION_RECORDED",
    "RECOVERY_REQUIRED",
}


class TransactionError(RuntimeError):
    """Invalid transaction plan or transaction lifecycle."""


def _text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise TransactionError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _bool(value: bool, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field} must be a real bool")
    return value


def _enum(value: str, allowed: set[str], field: str) -> str:
    text = str(value).strip().upper()
    if text not in allowed:
        raise TransactionError(f"unsupported {field}: {text}")
    return text


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class TransactionStep:
    step_id: str
    description: str
    postcondition_ref: str
    compensatable: bool
    depends_on_step_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "step.step_id"))
        object.__setattr__(self, "description", _text(self.description, "step.description"))
        object.__setattr__(self, "postcondition_ref", _text(self.postcondition_ref, "step.postcondition_ref"))
        object.__setattr__(self, "compensatable", _bool(self.compensatable, "step.compensatable"))
        deps = tuple(_text(value, "step.depends_on_step_ids") for value in self.depends_on_step_ids)
        if len(deps) != len(set(deps)):
            raise TransactionError("step dependencies must be unique")
        if self.step_id in deps:
            raise TransactionError("step cannot depend on itself")
        object.__setattr__(self, "depends_on_step_ids", deps)


@dataclass(frozen=True)
class TransactionPlan:
    transaction_id: str
    operation_id: str
    steps: tuple[TransactionStep, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", _text(self.transaction_id, "plan.transaction_id"))
        object.__setattr__(self, "operation_id", _text(self.operation_id, "plan.operation_id"))
        steps = tuple(self.steps)
        if not steps:
            raise TransactionError("transaction plan requires at least one step")
        if not all(isinstance(step, TransactionStep) for step in steps):
            raise TypeError("steps must contain TransactionStep values")
        ids = tuple(step.step_id for step in steps)
        if len(ids) != len(set(ids)):
            raise TransactionError("transaction step IDs must be unique")
        seen: set[str] = set()
        for step in steps:
            missing = [dep for dep in step.depends_on_step_ids if dep not in seen]
            if missing:
                raise TransactionError(
                    f"step {step.step_id} has unknown or forward dependencies: {missing}"
                )
            seen.add(step.step_id)
        object.__setattr__(self, "steps", steps)

    @property
    def plan_fingerprint(self) -> str:
        payload = {
            "transaction_id": self.transaction_id,
            "operation_id": self.operation_id,
            "steps": [
                {
                    "step_id": step.step_id,
                    "description": step.description,
                    "postcondition_ref": step.postcondition_ref,
                    "compensatable": step.compensatable,
                    "depends_on_step_ids": list(step.depends_on_step_ids),
                }
                for step in self.steps
            ],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class TransactionSnapshot:
    transaction_id: str
    operation_id: str
    snapshot_ref: str
    snapshot_fingerprint: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", _text(self.transaction_id, "snapshot.transaction_id"))
        object.__setattr__(self, "operation_id", _text(self.operation_id, "snapshot.operation_id"))
        object.__setattr__(self, "snapshot_ref", _text(self.snapshot_ref, "snapshot.snapshot_ref"))
        object.__setattr__(self, "snapshot_fingerprint", _text(self.snapshot_fingerprint, "snapshot.snapshot_fingerprint"))
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "snapshot.evidence_ref"))


@dataclass(frozen=True)
class StepExecution:
    step_id: str
    status: str
    change_state: str
    evidence_ref: str
    result_fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "execution.step_id"))
        object.__setattr__(self, "status", _enum(self.status, STEP_EXECUTION_STATUSES, "step execution status"))
        object.__setattr__(self, "change_state", _enum(self.change_state, CHANGE_STATES, "step change state"))
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "execution.evidence_ref"))
        object.__setattr__(self, "result_fingerprint", _optional_text(self.result_fingerprint, "execution.result_fingerprint"))
        if self.status == "SUCCEEDED" and self.change_state == "UNKNOWN":
            raise TransactionError("successful execution cannot have UNKNOWN change state")


@dataclass(frozen=True)
class PostconditionResult:
    step_id: str
    postcondition_ref: str
    status: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "postcondition.step_id"))
        object.__setattr__(self, "postcondition_ref", _text(self.postcondition_ref, "postcondition.postcondition_ref"))
        object.__setattr__(self, "status", _enum(self.status, POSTCONDITION_STATUSES, "postcondition status"))
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "postcondition.evidence_ref"))


@dataclass(frozen=True)
class CompensationResult:
    step_id: str
    snapshot_ref: str
    status: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "compensation.step_id"))
        object.__setattr__(self, "snapshot_ref", _text(self.snapshot_ref, "compensation.snapshot_ref"))
        object.__setattr__(self, "status", _enum(self.status, COMPENSATION_STATUSES, "compensation status"))
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "compensation.evidence_ref"))


@dataclass(frozen=True)
class TransactionReceipt:
    transaction_id: str
    operation_id: str
    snapshot_ref: str
    receipt_ref: str
    evidence_ref: str
    result_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", _text(self.transaction_id, "receipt.transaction_id"))
        object.__setattr__(self, "operation_id", _text(self.operation_id, "receipt.operation_id"))
        object.__setattr__(self, "snapshot_ref", _text(self.snapshot_ref, "receipt.snapshot_ref"))
        object.__setattr__(self, "receipt_ref", _text(self.receipt_ref, "receipt.receipt_ref"))
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "receipt.evidence_ref"))
        object.__setattr__(self, "result_fingerprint", _text(self.result_fingerprint, "receipt.result_fingerprint"))


@dataclass(frozen=True)
class TransactionEvent:
    sequence: int
    event_id: str
    transaction_id: str
    step_id: str | None
    event_type: str
    status: str | None
    change_state: str | None
    snapshot_ref: str | None
    snapshot_fingerprint: str | None
    evidence_ref: str
    receipt_ref: str | None
    result_fingerprint: str | None


@dataclass(frozen=True)
class TransactionHistory:
    transaction_id: str
    operation_id: str
    plan_fingerprint: str
    steps: tuple[TransactionStep, ...]
    events: tuple[TransactionEvent, ...]
    operation: OperationRecord
    unresolved_execution_step_ids: tuple[str, ...]
    unresolved_compensation_step_ids: tuple[str, ...]
    requires_recovery_review: bool


@dataclass(frozen=True)
class TransactionResult:
    status: str
    transaction_id: str
    operation_id: str
    snapshot_ref: str | None
    executed_step_ids: tuple[str, ...]
    compensated_step_ids: tuple[str, ...]
    failed_step_id: str | None
    evidence_refs: tuple[str, ...]
    operation: OperationRecord

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _enum(self.status, TRANSACTION_STATUSES, "transaction status"))
        object.__setattr__(self, "transaction_id", _text(self.transaction_id, "result.transaction_id"))
        object.__setattr__(self, "operation_id", _text(self.operation_id, "result.operation_id"))
        object.__setattr__(self, "snapshot_ref", _optional_text(self.snapshot_ref, "result.snapshot_ref"))
        object.__setattr__(self, "executed_step_ids", tuple(self.executed_step_ids))
        object.__setattr__(self, "compensated_step_ids", tuple(self.compensated_step_ids))
        object.__setattr__(self, "failed_step_id", _optional_text(self.failed_step_id, "result.failed_step_id"))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


class TransactionDriver(Protocol):
    def prepare_snapshot(self, plan: TransactionPlan) -> TransactionSnapshot: ...
    def execute_step(self, step: TransactionStep) -> StepExecution: ...
    def verify_postcondition(self, step: TransactionStep, execution: StepExecution) -> PostconditionResult: ...
    def compensate_step(self, step: TransactionStep, snapshot: TransactionSnapshot) -> CompensationResult: ...
    def success_receipt(self, plan: TransactionPlan, snapshot: TransactionSnapshot) -> TransactionReceipt: ...


class TransactionCoordinator:
    """Bounded multi-step coordinator with durable local plan/step evidence.

    Whole-operation success/failure/UNKNOWN remains owned by OperationJournal.
    This owner persists only transaction choreography and recovery evidence in the
    same canonical profile database so a process restart never erases which step
    was attempted, observed or compensated.
    """

    _DRIVER_METHODS = (
        "prepare_snapshot",
        "execute_step",
        "verify_postcondition",
        "compensate_step",
        "success_receipt",
    )
    _TRIGGER_NAMES = {
        "transactions_immutable_update",
        "transactions_immutable_delete",
        "transaction_steps_immutable_update",
        "transaction_steps_immutable_delete",
        "transaction_events_immutable_update",
        "transaction_events_immutable_delete",
        "activity_transaction_event",
    }

    def __init__(self, journal: OperationJournal):
        if not isinstance(journal, OperationJournal):
            raise TypeError("TransactionCoordinator requires OperationJournal")
        self.journal = journal
        self.store = journal.store
        self._conn = self.store._conn
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
            """CREATE TRIGGER transactions_immutable_update BEFORE UPDATE ON transactions
            BEGIN SELECT RAISE(ABORT, 'transaction identity is immutable'); END""",
            """CREATE TRIGGER transactions_immutable_delete BEFORE DELETE ON transactions
            BEGIN SELECT RAISE(ABORT, 'transaction identity is immutable'); END""",
            """CREATE TRIGGER transaction_steps_immutable_update BEFORE UPDATE ON transaction_steps
            BEGIN SELECT RAISE(ABORT, 'transaction plan is immutable'); END""",
            """CREATE TRIGGER transaction_steps_immutable_delete BEFORE DELETE ON transaction_steps
            BEGIN SELECT RAISE(ABORT, 'transaction plan is immutable'); END""",
            """CREATE TRIGGER transaction_events_immutable_update BEFORE UPDATE ON transaction_events
            BEGIN SELECT RAISE(ABORT, 'transaction evidence is append-only'); END""",
            """CREATE TRIGGER transaction_events_immutable_delete BEFORE DELETE ON transaction_events
            BEGIN SELECT RAISE(ABORT, 'transaction evidence is append-only'); END""",
            """CREATE TRIGGER activity_transaction_event AFTER INSERT ON transaction_events
            BEGIN
                INSERT INTO activity_events(
                    id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json
                )
                SELECT
                    'act_'||lower(hex(randomblob(16))),
                    'TRANSACTION_'||NEW.event_type,
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    o.song_id,
                    o.version_id,
                    'TRANSACTION',
                    NEW.transaction_id,
                    '{}'
                FROM transactions t JOIN operations o ON o.id=t.operation_id
                WHERE t.id=NEW.transaction_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        tables = [self._table_exists(name) for name in ("transactions", "transaction_steps", "transaction_events")]
        version = self._metadata_value("transaction_schema_version")
        if len(set(tables)) != 1 or tables[0] != (version is not None):
            raise LineageCorruptionError("transaction schema metadata/table mismatch")
        if tables[0]:
            if version != str(TRANSACTION_SCHEMA_VERSION):
                raise LineageCorruptionError(f"unsupported transaction schema version: {version}")
            return
        if not self._table_exists("operations") or not self._table_exists("activity_events"):
            raise LineageCorruptionError("TransactionCoordinator requires OperationJournal and ActivityLog first")
        try:
            with self.store._tx():
                self._conn.execute(
                    """CREATE TABLE transactions (
                        id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id),
                        plan_fingerprint TEXT NOT NULL CHECK(length(trim(plan_fingerprint)) > 0)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE transaction_steps (
                        transaction_id TEXT NOT NULL REFERENCES transactions(id),
                        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                        step_id TEXT NOT NULL CHECK(length(trim(step_id)) > 0),
                        description TEXT NOT NULL CHECK(length(trim(description)) > 0),
                        postcondition_ref TEXT NOT NULL CHECK(length(trim(postcondition_ref)) > 0),
                        compensatable INTEGER NOT NULL CHECK(compensatable IN (0,1)),
                        dependencies_json TEXT NOT NULL,
                        PRIMARY KEY(transaction_id, ordinal),
                        UNIQUE(transaction_id, step_id)
                    )"""
                )
                self._conn.execute(
                    """CREATE TABLE transaction_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        transaction_id TEXT NOT NULL REFERENCES transactions(id),
                        step_id TEXT NULL,
                        event_type TEXT NOT NULL CHECK(event_type IN (
                            'PLAN_REGISTERED','SNAPSHOT_CAPTURED','SAFE_FAILURE',
                            'STEP_EXECUTION_STARTED','STEP_EXECUTION_RECORDED',
                            'POSTCONDITION_RECORDED','COMPENSATION_STARTED',
                            'COMPENSATION_RECORDED','RECOVERY_REQUIRED'
                        )),
                        status TEXT NULL,
                        change_state TEXT NULL,
                        snapshot_ref TEXT NULL,
                        snapshot_fingerprint TEXT NULL,
                        evidence_ref TEXT NOT NULL CHECK(length(trim(evidence_ref)) > 0),
                        receipt_ref TEXT NULL,
                        result_fingerprint TEXT NULL,
                        FOREIGN KEY(transaction_id,step_id)
                            REFERENCES transaction_steps(transaction_id,step_id)
                    )"""
                )
                self._conn.execute("CREATE INDEX transaction_events_by_transaction ON transaction_events(transaction_id,seq)")
                for statement in self._trigger_statements():
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('transaction_schema_version',?)",
                    (str(TRANSACTION_SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize transaction journal") from exc

    @staticmethod
    def _event(row: sqlite3.Row) -> TransactionEvent:
        return TransactionEvent(
            sequence=int(row["seq"]),
            event_id=str(row["id"]),
            transaction_id=str(row["transaction_id"]),
            step_id=None if row["step_id"] is None else str(row["step_id"]),
            event_type=str(row["event_type"]),
            status=None if row["status"] is None else str(row["status"]),
            change_state=None if row["change_state"] is None else str(row["change_state"]),
            snapshot_ref=None if row["snapshot_ref"] is None else str(row["snapshot_ref"]),
            snapshot_fingerprint=None if row["snapshot_fingerprint"] is None else str(row["snapshot_fingerprint"]),
            evidence_ref=str(row["evidence_ref"]),
            receipt_ref=None if row["receipt_ref"] is None else str(row["receipt_ref"]),
            result_fingerprint=None if row["result_fingerprint"] is None else str(row["result_fingerprint"]),
        )

    def _events(self, transaction_id: str) -> tuple[TransactionEvent, ...]:
        return tuple(
            self._event(row)
            for row in self._conn.execute(
                "SELECT seq,id,transaction_id,step_id,event_type,status,change_state,snapshot_ref,snapshot_fingerprint,evidence_ref,receipt_ref,result_fingerprint "
                "FROM transaction_events WHERE transaction_id=? ORDER BY seq",
                (transaction_id,),
            )
        )

    def _steps(self, transaction_id: str) -> tuple[TransactionStep, ...]:
        result = []
        for row in self._conn.execute(
            "SELECT step_id,description,postcondition_ref,compensatable,dependencies_json "
            "FROM transaction_steps WHERE transaction_id=? ORDER BY ordinal",
            (transaction_id,),
        ):
            deps = json.loads(str(row["dependencies_json"]))
            if not isinstance(deps, list) or not all(isinstance(value, str) for value in deps):
                raise LineageCorruptionError("transaction dependency JSON is invalid")
            result.append(
                TransactionStep(
                    str(row["step_id"]),
                    str(row["description"]),
                    str(row["postcondition_ref"]),
                    bool(int(row["compensatable"])),
                    tuple(deps),
                )
            )
        return tuple(result)

    @staticmethod
    def _unresolved(events: tuple[TransactionEvent, ...], started: str, recorded: str) -> tuple[str, ...]:
        counts: dict[str, int] = {}
        for event in events:
            if event.step_id is None:
                continue
            if event.event_type == started:
                counts[event.step_id] = counts.get(event.step_id, 0) + 1
            elif event.event_type == recorded:
                counts[event.step_id] = counts.get(event.step_id, 0) - 1
        return tuple(sorted(step_id for step_id, count in counts.items() if count > 0))

    def history(self, transaction_id: str) -> TransactionHistory:
        transaction_id = _text(transaction_id, "transaction_id")
        row = self._conn.execute(
            "SELECT id,operation_id,plan_fingerprint FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise TransactionError(f"transaction not found: {transaction_id}")
        steps = self._steps(transaction_id)
        events = self._events(transaction_id)
        operation = self.journal.get(str(row["operation_id"]))
        unresolved_execution = self._unresolved(events, "STEP_EXECUTION_STARTED", "STEP_EXECUTION_RECORDED")
        unresolved_compensation = self._unresolved(events, "COMPENSATION_STARTED", "COMPENSATION_RECORDED")
        requires_review = (
            operation.recorded_state == "EXECUTING"
            or (
                operation.recorded_state == "UNKNOWN"
                and not bool(getattr(operation, "reconciled", False))
            )
        )
        return TransactionHistory(
            transaction_id=transaction_id,
            operation_id=str(row["operation_id"]),
            plan_fingerprint=str(row["plan_fingerprint"]),
            steps=steps,
            events=events,
            operation=operation,
            unresolved_execution_step_ids=unresolved_execution,
            unresolved_compensation_step_ids=unresolved_compensation,
            requires_recovery_review=requires_review,
        )

    def _validate_existing(self) -> None:
        try:
            trigger_names = {
                str(row["name"])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND "
                    "(name LIKE 'transaction_%' OR name='activity_transaction_event')"
                )
            }
            missing = self._TRIGGER_NAMES - trigger_names
            if missing:
                raise LineageCorruptionError(f"transaction hooks are incomplete: {sorted(missing)}")
            invalid = self._conn.execute(
                "SELECT t.id FROM transactions t LEFT JOIN operations o ON o.id=t.operation_id "
                "WHERE o.id IS NULL LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise LineageCorruptionError("transaction references a missing operation")
            for row in self._conn.execute("SELECT id,operation_id,plan_fingerprint FROM transactions ORDER BY id"):
                transaction_id = str(row["id"])
                steps = self._steps(transaction_id)
                if not steps:
                    raise LineageCorruptionError("transaction plan unexpectedly has no steps")
                reconstructed = TransactionPlan(transaction_id, str(row["operation_id"]), steps)
                if reconstructed.plan_fingerprint != str(row["plan_fingerprint"]):
                    raise LineageCorruptionError("transaction plan fingerprint mismatch")
                events = self._events(transaction_id)
                if not events or events[0].event_type != "PLAN_REGISTERED":
                    raise LineageCorruptionError("transaction history must begin with PLAN_REGISTERED")
                if sum(event.event_type == "PLAN_REGISTERED" for event in events) != 1:
                    raise LineageCorruptionError("transaction history must contain one PLAN_REGISTERED event")
                step_ids = {step.step_id for step in steps}
                step_order = {step.step_id: index for index, step in enumerate(steps)}
                execution_balance: dict[str, int] = {}
                compensation_balance: dict[str, int] = {}
                last_execution_ordinal = -1
                snapshot_seen = False
                for event in events:
                    if event.event_type not in TRANSACTION_EVENT_TYPES:
                        raise LineageCorruptionError("transaction history contains unknown event type")
                    if event.step_id is not None and event.step_id not in step_ids:
                        raise LineageCorruptionError("transaction event references an unknown step")
                    if event.event_type == "PLAN_REGISTERED":
                        if event.step_id is not None or event.status is not None or event.change_state is not None:
                            raise LineageCorruptionError("PLAN_REGISTERED event shape is invalid")
                    elif event.event_type == "SNAPSHOT_CAPTURED":
                        if snapshot_seen or event.step_id is not None or not event.snapshot_ref or not event.snapshot_fingerprint:
                            raise LineageCorruptionError("SNAPSHOT_CAPTURED event shape is invalid")
                        snapshot_seen = True
                    elif event.event_type == "SAFE_FAILURE":
                        if event.step_id is not None or snapshot_seen:
                            raise LineageCorruptionError("SAFE_FAILURE must occur before snapshot/mutation")
                    elif event.event_type == "STEP_EXECUTION_STARTED":
                        if not snapshot_seen or event.step_id is None or not event.snapshot_ref:
                            raise LineageCorruptionError("STEP_EXECUTION_STARTED event shape is invalid")
                        ordinal = step_order[event.step_id]
                        if ordinal < last_execution_ordinal:
                            raise LineageCorruptionError("forward execution order regressed")
                        last_execution_ordinal = ordinal
                        execution_balance[event.step_id] = execution_balance.get(event.step_id, 0) + 1
                        if execution_balance[event.step_id] != 1:
                            raise LineageCorruptionError("transaction step execution started more than once")
                    elif event.event_type == "STEP_EXECUTION_RECORDED":
                        if event.step_id is None or event.status not in STEP_EXECUTION_STATUSES or event.change_state not in CHANGE_STATES:
                            raise LineageCorruptionError("STEP_EXECUTION_RECORDED event shape is invalid")
                        execution_balance[event.step_id] = execution_balance.get(event.step_id, 0) - 1
                        if execution_balance[event.step_id] != 0:
                            raise LineageCorruptionError("step execution recorded without exactly one start")
                    elif event.event_type == "POSTCONDITION_RECORDED":
                        if event.step_id is None or event.status not in POSTCONDITION_STATUSES or event.change_state is not None:
                            raise LineageCorruptionError("POSTCONDITION_RECORDED event shape is invalid")
                        if execution_balance.get(event.step_id, 0) != 0:
                            raise LineageCorruptionError("postcondition recorded before execution result")
                    elif event.event_type == "COMPENSATION_STARTED":
                        if event.step_id is None or not event.snapshot_ref:
                            raise LineageCorruptionError("COMPENSATION_STARTED event shape is invalid")
                        compensation_balance[event.step_id] = compensation_balance.get(event.step_id, 0) + 1
                        if compensation_balance[event.step_id] != 1:
                            raise LineageCorruptionError("compensation started more than once for a step")
                    elif event.event_type == "COMPENSATION_RECORDED":
                        if event.step_id is None or event.status not in COMPENSATION_STATUSES or not event.snapshot_ref:
                            raise LineageCorruptionError("COMPENSATION_RECORDED event shape is invalid")
                        compensation_balance[event.step_id] = compensation_balance.get(event.step_id, 0) - 1
                        if compensation_balance[event.step_id] != 0:
                            raise LineageCorruptionError("compensation recorded without exactly one start")
                    elif event.event_type == "RECOVERY_REQUIRED":
                        if not event.snapshot_ref:
                            raise LineageCorruptionError("RECOVERY_REQUIRED requires snapshot reference")
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError("transaction journal is unreadable or corrupt") from exc

    def _append_event(
        self,
        transaction_id: str,
        event_type: str,
        *,
        step_id: str | None = None,
        status: str | None = None,
        change_state: str | None = None,
        snapshot_ref: str | None = None,
        snapshot_fingerprint: str | None = None,
        evidence_ref: str,
        receipt_ref: str | None = None,
        result_fingerprint: str | None = None,
    ) -> None:
        if event_type not in TRANSACTION_EVENT_TYPES:
            raise TransactionError(f"unsupported transaction event: {event_type}")
        self._conn.execute(
            "INSERT INTO transaction_events(id,transaction_id,step_id,event_type,status,change_state,snapshot_ref,snapshot_fingerprint,evidence_ref,receipt_ref,result_fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                _new_id("txevt"), transaction_id, step_id, event_type, status, change_state,
                snapshot_ref, snapshot_fingerprint, _text(evidence_ref, "event.evidence_ref"),
                receipt_ref, result_fingerprint,
            ),
        )

    def _register_plan(self, plan: TransactionPlan) -> None:
        existing = self._conn.execute(
            "SELECT operation_id,plan_fingerprint FROM transactions WHERE id=?",
            (plan.transaction_id,),
        ).fetchone()
        operation_existing = self._conn.execute(
            "SELECT id FROM transactions WHERE operation_id=?",
            (plan.operation_id,),
        ).fetchone()
        if existing is not None or operation_existing is not None:
            raise TransactionError(
                "transaction or operation is already registered; inspect recovery state instead of rerunning"
            )
        with self.store._tx():
            self._conn.execute(
                "INSERT INTO transactions(id,operation_id,plan_fingerprint) VALUES(?,?,?)",
                (plan.transaction_id, plan.operation_id, plan.plan_fingerprint),
            )
            for ordinal, step in enumerate(plan.steps):
                self._conn.execute(
                    "INSERT INTO transaction_steps(transaction_id,ordinal,step_id,description,postcondition_ref,compensatable,dependencies_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        plan.transaction_id, ordinal, step.step_id, step.description,
                        step.postcondition_ref, 1 if step.compensatable else 0,
                        json.dumps(list(step.depends_on_step_ids), separators=(",", ":")),
                    ),
                )
            self._append_event(
                plan.transaction_id,
                "PLAN_REGISTERED",
                evidence_ref=f"transaction:{plan.transaction_id}:plan:{plan.plan_fingerprint}",
            )

    @staticmethod
    def _driver_methods(driver: object) -> dict[str, object]:
        methods: dict[str, object] = {}
        for name in TransactionCoordinator._DRIVER_METHODS:
            method = getattr(driver, name, None)
            if not callable(method):
                raise TransactionError(f"transaction driver must implement {name}()")
            methods[name] = method
        return methods

    @staticmethod
    def _exception_ref(plan: TransactionPlan, phase: str, step_id: str | None, exc: Exception) -> str:
        suffix = "none" if step_id is None else step_id
        return f"transaction:{plan.transaction_id}:{phase}:{suffix}:exception:{type(exc).__name__}"

    @staticmethod
    def _require_snapshot_identity(plan: TransactionPlan, snapshot: TransactionSnapshot) -> None:
        if snapshot.transaction_id != plan.transaction_id or snapshot.operation_id != plan.operation_id:
            raise TransactionError("recovery snapshot belongs to a different transaction/operation")

    @staticmethod
    def _require_execution_identity(step: TransactionStep, execution: StepExecution) -> None:
        if execution.step_id != step.step_id:
            raise TransactionError("step execution belongs to a different step")

    @staticmethod
    def _require_postcondition_identity(step: TransactionStep, post: PostconditionResult) -> None:
        if post.step_id != step.step_id or post.postcondition_ref != step.postcondition_ref:
            raise TransactionError("postcondition result belongs to a different step/condition")

    @staticmethod
    def _require_compensation_identity(
        step: TransactionStep,
        snapshot: TransactionSnapshot,
        result: CompensationResult,
    ) -> None:
        if result.step_id != step.step_id or result.snapshot_ref != snapshot.snapshot_ref:
            raise TransactionError("compensation result belongs to a different step/snapshot")

    @staticmethod
    def _require_receipt_identity(
        plan: TransactionPlan,
        snapshot: TransactionSnapshot,
        receipt: TransactionReceipt,
    ) -> None:
        if (
            receipt.transaction_id != plan.transaction_id
            or receipt.operation_id != plan.operation_id
            or receipt.snapshot_ref != snapshot.snapshot_ref
        ):
            raise TransactionError("transaction receipt belongs to a different transaction/operation/snapshot")

    def _safe_failure(self, plan: TransactionPlan, evidence_ref: str) -> TransactionResult:
        with self.store._tx():
            self._append_event(plan.transaction_id, "SAFE_FAILURE", evidence_ref=evidence_ref)
        operation = self.journal.complete_failure(plan.operation_id, evidence_ref=evidence_ref)
        return TransactionResult(
            "COMPENSATED", plan.transaction_id, plan.operation_id, None,
            (), (), None, (evidence_ref,), operation,
        )

    def _compensate(
        self,
        plan: TransactionPlan,
        snapshot: TransactionSnapshot,
        compensate: object,
        changed_steps: list[TransactionStep],
        evidence: list[str],
        *,
        root_unknown: bool,
        failed_step_id: str | None,
    ) -> TransactionResult:
        root_failure_ref = evidence[-1]
        recovery_evidence_ref = root_failure_ref if root_unknown else None
        restored: list[str] = []
        recovery_required = root_unknown

        for step in reversed(changed_steps):
            if not step.compensatable:
                recovery_required = True
                ref = f"transaction:{plan.transaction_id}:noncompensatable:{step.step_id}"
                evidence.append(ref)
                recovery_evidence_ref = ref
                continue
            start_ref = f"transaction:{plan.transaction_id}:compensation-started:{step.step_id}"
            with self.store._tx():
                self._append_event(
                    plan.transaction_id, "COMPENSATION_STARTED",
                    step_id=step.step_id, snapshot_ref=snapshot.snapshot_ref,
                    evidence_ref=start_ref,
                )
            try:
                result = compensate(step, snapshot)  # type: ignore[misc]
                if not isinstance(result, CompensationResult):
                    raise TypeError("compensate_step must return CompensationResult")
                self._require_compensation_identity(step, snapshot, result)
            except Exception as exc:
                ref = self._exception_ref(plan, "compensate", step.step_id, exc)
                result = CompensationResult(step.step_id, snapshot.snapshot_ref, "UNKNOWN", ref)
            evidence.append(result.evidence_ref)
            with self.store._tx():
                self._append_event(
                    plan.transaction_id, "COMPENSATION_RECORDED",
                    step_id=step.step_id, status=result.status,
                    snapshot_ref=result.snapshot_ref, evidence_ref=result.evidence_ref,
                )
            if result.status == "RESTORED":
                restored.append(step.step_id)
            else:
                recovery_required = True
                recovery_evidence_ref = result.evidence_ref

        if recovery_required:
            recovery_ref = recovery_evidence_ref or root_failure_ref
            with self.store._tx():
                self._append_event(
                    plan.transaction_id, "RECOVERY_REQUIRED",
                    step_id=failed_step_id, snapshot_ref=snapshot.snapshot_ref,
                    evidence_ref=recovery_ref,
                )
            operation = self.journal.mark_unknown(plan.operation_id, evidence_ref=recovery_ref)
            return TransactionResult(
                "RECOVERY_REQUIRED", plan.transaction_id, plan.operation_id,
                snapshot.snapshot_ref, tuple(step.step_id for step in changed_steps),
                tuple(restored), failed_step_id, tuple(evidence), operation,
            )

        operation = self.journal.complete_failure(
            plan.operation_id,
            evidence_ref=root_failure_ref,
            result_fingerprint=f"compensated:{snapshot.snapshot_fingerprint}",
        )
        return TransactionResult(
            "COMPENSATED", plan.transaction_id, plan.operation_id,
            snapshot.snapshot_ref, tuple(step.step_id for step in changed_steps),
            tuple(restored), failed_step_id, tuple(evidence), operation,
        )

    def run(self, plan: TransactionPlan, driver: TransactionDriver) -> TransactionResult:
        if not isinstance(plan, TransactionPlan):
            raise TypeError("plan must be TransactionPlan")
        operation = self.journal.get(plan.operation_id)
        if operation.recorded_state != "EXECUTING":
            raise TransactionError(
                f"transaction requires already-claimed EXECUTING operation, got {operation.recorded_state}"
            )

        try:
            methods = self._driver_methods(driver)
        except Exception as exc:
            self._register_plan(plan)
            return self._safe_failure(plan, self._exception_ref(plan, "driver-contract", None, exc))

        self._register_plan(plan)

        try:
            snapshot = methods["prepare_snapshot"](plan)  # type: ignore[operator]
            if not isinstance(snapshot, TransactionSnapshot):
                raise TypeError("prepare_snapshot must return TransactionSnapshot")
            self._require_snapshot_identity(plan, snapshot)
        except Exception as exc:
            return self._safe_failure(plan, self._exception_ref(plan, "snapshot", None, exc))

        with self.store._tx():
            self._append_event(
                plan.transaction_id, "SNAPSHOT_CAPTURED",
                snapshot_ref=snapshot.snapshot_ref,
                snapshot_fingerprint=snapshot.snapshot_fingerprint,
                evidence_ref=snapshot.evidence_ref,
            )

        evidence: list[str] = [snapshot.evidence_ref]
        changed: list[TransactionStep] = []

        for step in plan.steps:
            start_ref = f"transaction:{plan.transaction_id}:execution-started:{step.step_id}"
            with self.store._tx():
                self._append_event(
                    plan.transaction_id, "STEP_EXECUTION_STARTED",
                    step_id=step.step_id, snapshot_ref=snapshot.snapshot_ref,
                    evidence_ref=start_ref,
                )
            try:
                execution = methods["execute_step"](step)  # type: ignore[operator]
                if not isinstance(execution, StepExecution):
                    raise TypeError("execute_step must return StepExecution")
                self._require_execution_identity(step, execution)
            except Exception as exc:
                ref = self._exception_ref(plan, "execute", step.step_id, exc)
                execution = StepExecution(step.step_id, "UNKNOWN", "UNKNOWN", ref)
            evidence.append(execution.evidence_ref)
            with self.store._tx():
                self._append_event(
                    plan.transaction_id, "STEP_EXECUTION_RECORDED",
                    step_id=step.step_id, status=execution.status,
                    change_state=execution.change_state,
                    snapshot_ref=snapshot.snapshot_ref,
                    evidence_ref=execution.evidence_ref,
                    result_fingerprint=execution.result_fingerprint,
                )

            if execution.change_state == "APPLIED":
                changed.append(step)
            if execution.change_state == "UNKNOWN":
                return self._compensate(
                    plan, snapshot, methods["compensate_step"], changed, evidence,
                    root_unknown=True, failed_step_id=step.step_id,
                )
            if execution.status != "SUCCEEDED":
                return self._compensate(
                    plan, snapshot, methods["compensate_step"], changed, evidence,
                    root_unknown=execution.status == "UNKNOWN", failed_step_id=step.step_id,
                )

            try:
                post = methods["verify_postcondition"](step, execution)  # type: ignore[operator]
                if not isinstance(post, PostconditionResult):
                    raise TypeError("verify_postcondition must return PostconditionResult")
                self._require_postcondition_identity(step, post)
            except Exception as exc:
                ref = self._exception_ref(plan, "verify", step.step_id, exc)
                post = PostconditionResult(step.step_id, step.postcondition_ref, "UNKNOWN", ref)
            evidence.append(post.evidence_ref)
            with self.store._tx():
                self._append_event(
                    plan.transaction_id, "POSTCONDITION_RECORDED",
                    step_id=step.step_id, status=post.status,
                    snapshot_ref=snapshot.snapshot_ref,
                    evidence_ref=post.evidence_ref,
                )
            if post.status != "SATISFIED":
                return self._compensate(
                    plan, snapshot, methods["compensate_step"], changed, evidence,
                    root_unknown=post.status == "UNKNOWN", failed_step_id=step.step_id,
                )

        try:
            receipt = methods["success_receipt"](plan, snapshot)  # type: ignore[operator]
            if not isinstance(receipt, TransactionReceipt):
                raise TypeError("success_receipt must return TransactionReceipt")
            self._require_receipt_identity(plan, snapshot, receipt)
        except Exception as exc:
            evidence.append(self._exception_ref(plan, "receipt", None, exc))
            return self._compensate(
                plan, snapshot, methods["compensate_step"], changed, evidence,
                root_unknown=True, failed_step_id=None,
            )

        evidence.append(receipt.evidence_ref)
        operation = self.journal.complete_success(
            plan.operation_id,
            receipt_ref=receipt.receipt_ref,
            evidence_ref=receipt.evidence_ref,
            result_fingerprint=receipt.result_fingerprint,
        )
        return TransactionResult(
            "COMPLETE", plan.transaction_id, plan.operation_id,
            snapshot.snapshot_ref, tuple(step.step_id for step in changed), (),
            None, tuple(evidence), operation,
        )
