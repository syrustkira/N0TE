from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .activity import ActivityLog
from .authority import ActionIntent, ApprovalBinding, AuthorityService
from .eligibility import ExecutionEligibilityDecision
from .lineage import LineageCorruptionError, LineageStore, NotFoundError, ValidationError
from .network import TransportDecision

OPERATION_SCHEMA_VERSION = 2
OPERATION_EVENTS = {
    "PREPARED", "EXECUTION_CLAIMED", "SUCCEEDED", "FAILED", "UNKNOWN",
    "RECONCILED_SUCCEEDED", "RECONCILED_FAILED",
}
RECORDED_STATES = {"PREPARED", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN"}
EFFECTIVE_OUTCOMES = {"SUCCEEDED", "FAILED", "UNKNOWN"}


class OperationError(RuntimeError):
    """Operation lifecycle or execute-once contract violation."""


class DuplicateExecutionError(OperationError):
    """An operation has already been claimed or reached an outcome."""


def _text(value: str, field: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValidationError(f"{field} must not be empty")
    return value


def _optional(value: str | None, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class OperationEvent:
    sequence: int
    id: str
    operation_id: str
    event_type: str
    evidence_ref: str | None
    receipt_ref: str | None
    result_fingerprint: str | None


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    idempotency_key: str
    intent_fingerprint: str
    approval_id: str
    approval_source_ref: str
    song_id: str | None
    version_id: str | None
    transport_route_id: str | None
    eligibility_subject_id: str | None
    eligibility_capability: str | None
    recorded_state: str
    effective_outcome: str | None
    attempt_count: int
    receipt_ref: str | None
    evidence_ref: str | None
    result_fingerprint: str | None
    reconciled: bool
    reconciliation_evidence_ref: str | None


class OperationJournal:
    """Durable execute-once identity, receipt, UNKNOWN and reconciliation journal."""

    _TRIGGERS = {
        "operation_version_matches_song",
        "operations_immutable_update", "operations_immutable_delete",
        "operation_events_immutable_update", "operation_events_immutable_delete",
        "activity_operation_event",
    }

    def __init__(self, store: LineageStore, activity: ActivityLog):
        if not isinstance(store, LineageStore):
            raise TypeError("OperationJournal requires the canonical LineageStore")
        if not isinstance(activity, ActivityLog) or activity.store is not store:
            raise TypeError("OperationJournal requires ActivityLog for the same store")
        self.store, self.activity, self._conn = store, activity, store._conn
        self._ensure_schema()
        self._validate_existing()

    def _table(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _trigger_sql() -> tuple[str, ...]:
        return (
            """CREATE TRIGGER operation_version_matches_song BEFORE INSERT ON operations
            WHEN NEW.version_id IS NOT NULL AND (NEW.song_id IS NULL OR NOT EXISTS(
                SELECT 1 FROM versions v WHERE v.id=NEW.version_id AND v.song_id=NEW.song_id))
            BEGIN SELECT RAISE(ABORT,'operation version belongs to a different Song'); END""",
            """CREATE TRIGGER operations_immutable_update BEFORE UPDATE ON operations
            BEGIN SELECT RAISE(ABORT,'operation identity is immutable'); END""",
            """CREATE TRIGGER operations_immutable_delete BEFORE DELETE ON operations
            BEGIN SELECT RAISE(ABORT,'operation identity is immutable'); END""",
            """CREATE TRIGGER operation_events_immutable_update BEFORE UPDATE ON operation_events
            BEGIN SELECT RAISE(ABORT,'operation history is append-only'); END""",
            """CREATE TRIGGER operation_events_immutable_delete BEFORE DELETE ON operation_events
            BEGIN SELECT RAISE(ABORT,'operation history is append-only'); END""",
            """CREATE TRIGGER activity_operation_event AFTER INSERT ON operation_events BEGIN
                INSERT INTO activity_events(id,event_type,artist_id,song_id,version_id,object_type,object_id,payload_json)
                SELECT 'act_'||lower(hex(randomblob(16))), 'OPERATION_'||NEW.event_type,
                    (SELECT value FROM metadata WHERE key='primary_artist_id'),
                    o.song_id,o.version_id,'OPERATION',o.id,'{}'
                FROM operations o WHERE o.id=NEW.operation_id;
            END""",
        )

    def _ensure_schema(self) -> None:
        ops, events, version = self._table("operations"), self._table("operation_events"), self._meta("operation_schema_version")
        if ops != events or ops != (version is not None):
            raise LineageCorruptionError("operation schema metadata/table mismatch")
        if ops:
            if version == "1":
                try:
                    with self.store._tx():
                        self._conn.execute("ALTER TABLE operations ADD COLUMN eligibility_subject_id TEXT NULL")
                        self._conn.execute("ALTER TABLE operations ADD COLUMN eligibility_capability TEXT NULL")
                        self._conn.execute("UPDATE metadata SET value=? WHERE key='operation_schema_version'", (str(OPERATION_SCHEMA_VERSION),))
                except sqlite3.DatabaseError as exc:
                    raise LineageCorruptionError("cannot migrate Operation Journal eligibility identity") from exc
                return
            if version != str(OPERATION_SCHEMA_VERSION):
                raise LineageCorruptionError(f"unsupported operation schema version: {version}")
            return
        if not self._table("activity_events"):
            raise LineageCorruptionError("OperationJournal requires ActivityLog to initialize first")
        try:
            with self.store._tx():
                self._conn.execute("""CREATE TABLE operations(
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key))>0),
                    intent_fingerprint TEXT NOT NULL CHECK(length(trim(intent_fingerprint))>0),
                    approval_id TEXT NOT NULL CHECK(length(trim(approval_id))>0),
                    approval_source_ref TEXT NOT NULL CHECK(length(trim(approval_source_ref))>0),
                    song_id TEXT NULL REFERENCES songs(id),
                    version_id TEXT NULL REFERENCES versions(id),
                    transport_route_id TEXT NULL CHECK(transport_route_id IS NULL OR length(trim(transport_route_id))>0),
                    eligibility_subject_id TEXT NULL CHECK(eligibility_subject_id IS NULL OR length(trim(eligibility_subject_id))>0),
                    eligibility_capability TEXT NULL CHECK(eligibility_capability IS NULL OR length(trim(eligibility_capability))>0)
                )""")
                self._conn.execute("""CREATE TABLE operation_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL REFERENCES operations(id),
                    event_type TEXT NOT NULL CHECK(event_type IN('PREPARED','EXECUTION_CLAIMED','SUCCEEDED','FAILED','UNKNOWN','RECONCILED_SUCCEEDED','RECONCILED_FAILED')),
                    evidence_ref TEXT NULL, receipt_ref TEXT NULL, result_fingerprint TEXT NULL
                )""")
                self._conn.execute("CREATE INDEX operation_events_by_operation ON operation_events(operation_id,seq)")
                for sql in self._trigger_sql():
                    self._conn.execute(sql)
                self._conn.execute("INSERT INTO metadata(key,value) VALUES('operation_schema_version',?)", (str(OPERATION_SCHEMA_VERSION),))
        except sqlite3.DatabaseError as exc:
            raise LineageCorruptionError("cannot initialize Operation Journal") from exc

    @staticmethod
    def _event(row: sqlite3.Row) -> OperationEvent:
        return OperationEvent(int(row["seq"]), str(row["id"]), str(row["operation_id"]), str(row["event_type"]),
            None if row["evidence_ref"] is None else str(row["evidence_ref"]),
            None if row["receipt_ref"] is None else str(row["receipt_ref"]),
            None if row["result_fingerprint"] is None else str(row["result_fingerprint"]))

    def _events(self, operation_id: str) -> tuple[OperationEvent, ...]:
        return tuple(self._event(r) for r in self._conn.execute(
            "SELECT seq,id,operation_id,event_type,evidence_ref,receipt_ref,result_fingerprint FROM operation_events WHERE operation_id=? ORDER BY seq",
            (operation_id,)))

    @staticmethod
    def _derive(events: tuple[OperationEvent, ...]) -> tuple[str, str | None, int, bool]:
        if not events or events[0].event_type != "PREPARED":
            raise LineageCorruptionError("operation history must begin with PREPARED")
        state, effective, attempts, reconciled = "PREPARED", None, 0, False
        for i, event in enumerate(events[1:], 1):
            kind = event.event_type
            if kind == "EXECUTION_CLAIMED" and state == "PREPARED" and attempts == 0:
                state, attempts = "EXECUTING", 1
            elif kind in {"SUCCEEDED", "FAILED", "UNKNOWN"} and state == "EXECUTING" and effective is None:
                state, effective = kind, kind
            elif kind in {"RECONCILED_SUCCEEDED", "RECONCILED_FAILED"} and state == "UNKNOWN" and effective == "UNKNOWN" and not reconciled:
                effective, reconciled = kind.removeprefix("RECONCILED_"), True
            else:
                raise LineageCorruptionError("operation lifecycle sequence is invalid")
            if (reconciled or state in {"SUCCEEDED", "FAILED"}) and i != len(events)-1:
                raise LineageCorruptionError("operation history continues after terminal outcome")
        return state, effective, attempts, reconciled

    @staticmethod
    def _validate_evidence(events: tuple[OperationEvent, ...]) -> None:
        for event in events:
            if event.event_type in {"PREPARED", "EXECUTION_CLAIMED", "FAILED", "UNKNOWN", "RECONCILED_FAILED"} and not event.evidence_ref:
                raise LineageCorruptionError(f"{event.event_type} operation event requires evidence")
            if event.event_type in {"SUCCEEDED", "RECONCILED_SUCCEEDED"} and not (event.receipt_ref and event.evidence_ref and event.result_fingerprint):
                raise LineageCorruptionError(f"{event.event_type} requires receipt, evidence and result fingerprint")

    def _validate_existing(self) -> None:
        try:
            triggers = {str(r["name"]) for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'operation_%' OR name='activity_operation_event')")}
            missing = self._TRIGGERS - triggers
            if missing:
                raise LineageCorruptionError(f"operation hooks are incomplete: {sorted(missing)}")
            invalid = self._conn.execute("""SELECT o.id FROM operations o
                LEFT JOIN songs s ON s.id=o.song_id LEFT JOIN versions v ON v.id=o.version_id
                WHERE (o.song_id IS NOT NULL AND s.id IS NULL)
                   OR (o.version_id IS NOT NULL AND (v.id IS NULL OR o.song_id IS NULL OR v.song_id<>o.song_id))
                   OR (o.transport_route_id IS NOT NULL AND length(trim(o.transport_route_id))=0)
                   OR (o.eligibility_subject_id IS NOT NULL AND length(trim(o.eligibility_subject_id))=0)
                   OR (o.eligibility_capability IS NOT NULL AND length(trim(o.eligibility_capability))=0)
                LIMIT 1""").fetchone()
            if invalid is not None:
                raise LineageCorruptionError("operation history contains invalid identity references")
            for row in self._conn.execute("SELECT id FROM operations ORDER BY id"):
                events = self._events(str(row["id"])); self._derive(events); self._validate_evidence(events)
        except LineageCorruptionError:
            raise
        except Exception as exc:
            raise LineageCorruptionError("operation history is unreadable or corrupt") from exc

    def _identity(self, operation_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT id,idempotency_key,intent_fingerprint,approval_id,approval_source_ref,song_id,version_id,transport_route_id,eligibility_subject_id,eligibility_capability FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"operation not found: {operation_id}")
        return row

    def _record(self, row: sqlite3.Row) -> OperationRecord:
        events = self._events(str(row["id"])); state, effective, attempts, reconciled = self._derive(events); terminal = events[-1]
        return OperationRecord(str(row["id"]), str(row["idempotency_key"]), str(row["intent_fingerprint"]), str(row["approval_id"]), str(row["approval_source_ref"]),
            None if row["song_id"] is None else str(row["song_id"]), None if row["version_id"] is None else str(row["version_id"]),
            None if row["transport_route_id"] is None else str(row["transport_route_id"]), None if row["eligibility_subject_id"] is None else str(row["eligibility_subject_id"]),
            None if row["eligibility_capability"] is None else str(row["eligibility_capability"]), state, effective, attempts,
            terminal.receipt_ref, terminal.evidence_ref, terminal.result_fingerprint, reconciled,
            terminal.evidence_ref if terminal.event_type.startswith("RECONCILED_") else None)

    def get(self, operation_id: str) -> OperationRecord:
        return self._record(self._identity(operation_id))

    def by_idempotency_key(self, key: str) -> OperationRecord | None:
        row = self._conn.execute("SELECT id,idempotency_key,intent_fingerprint,approval_id,approval_source_ref,song_id,version_id,transport_route_id,eligibility_subject_id,eligibility_capability FROM operations WHERE idempotency_key=?", (_text(key,"idempotency_key"),)).fetchone()
        return None if row is None else self._record(row)

    def events(self, operation_id: str) -> tuple[OperationEvent, ...]:
        self._identity(operation_id); return self._events(operation_id)

    def _song_version(self, song_id: str | None, version_id: str | None) -> tuple[str | None, str | None]:
        if song_id is None and version_id is not None:
            raise ValidationError("version_id requires song_id")
        if song_id is None:
            return None, None
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        if version_id is None:
            return song.id, None
        version = self.store.get_version(version_id)
        if version is None:
            raise NotFoundError(f"version not found: {version_id}")
        if version.song_id != song.id:
            raise ValidationError("version belongs to a different Song")
        return song.id, version.id

    @staticmethod
    def _approval(intent: ActionIntent, approval: ApprovalBinding) -> None:
        if AuthorityService.validate(intent, approval).status != "VALID":
            raise OperationError("approval is stale for the current action intent")

    @staticmethod
    def _execution_identity(intent: ActionIntent, route: str | None, subject: str | None, capability: str | None) -> tuple[str | None, str | None, str | None]:
        route, subject, capability = _optional(route,"transport_route_id"), _optional(subject,"eligibility_subject_id"), _optional(capability,"eligibility_capability")
        if intent.destination is None:
            if any(x is not None for x in (route, subject, capability)):
                raise OperationError("local operation cannot bind outbound execution eligibility identity")
            return None, None, None
        if any(x is None for x in (route, subject, capability)):
            raise OperationError("outbound operation requires transport_route_id, eligibility_subject_id and eligibility_capability")
        return route, subject, capability

    @staticmethod
    def _gates(operation: OperationRecord, intent: ActionIntent, transport: TransportDecision | None, eligibility: ExecutionEligibilityDecision | None) -> None:
        if operation.transport_route_id is None:
            if intent.destination is not None:
                raise OperationError("outbound intent is missing its prepared transport route")
            if transport is not None or eligibility is not None:
                raise OperationError("local operation has no bound outbound execution gates")
            return
        if intent.destination is None:
            raise OperationError("transport-bound operation cannot claim a local intent")
        if operation.eligibility_subject_id is None or operation.eligibility_capability is None:
            raise OperationError("legacy outbound operation lacks eligibility identity and must be prepared again")
        if not isinstance(transport, TransportDecision):
            raise OperationError("outbound operation requires an explicit transport decision")
        if transport.status != "ALLOW" or transport.route_id != operation.transport_route_id or transport.action_authority_granted is not False:
            raise OperationError("transport decision does not allow this exact route without authority")
        if not isinstance(eligibility, ExecutionEligibilityDecision):
            raise OperationError("outbound operation requires an explicit eligibility decision")
        if eligibility.status != "ALLOW":
            raise OperationError("execution eligibility denies or marks this route stale")
        if (eligibility.job_id, eligibility.route_id, eligibility.subject_id, eligibility.capability) != (intent.job_id, operation.transport_route_id, operation.eligibility_subject_id, operation.eligibility_capability):
            raise OperationError("eligibility decision does not match the exact prepared execution identity")
        if eligibility.action_authority_granted is not False:
            raise OperationError("eligibility decision must not grant action authority")

    def _append(self, operation_id: str, event_type: str, evidence: str | None = None, receipt: str | None = None, result: str | None = None) -> None:
        if event_type not in OPERATION_EVENTS:
            raise OperationError(f"unsupported operation event: {event_type}")
        self._conn.execute("INSERT INTO operation_events(id,operation_id,event_type,evidence_ref,receipt_ref,result_fingerprint) VALUES(?,?,?,?,?,?)", (_id("opevt"), operation_id, event_type, evidence, receipt, result))

    def prepare(self, *, idempotency_key: str, intent: ActionIntent, approval: ApprovalBinding, song_id: str | None = None, version_id: str | None = None,
                transport_route_id: str | None = None, eligibility_subject_id: str | None = None, eligibility_capability: str | None = None) -> OperationRecord:
        if not isinstance(intent, ActionIntent): raise TypeError("intent must be ActionIntent")
        if not isinstance(approval, ApprovalBinding): raise TypeError("approval must be ApprovalBinding")
        key = _text(idempotency_key,"idempotency_key"); self._approval(intent, approval); song_id, version_id = self._song_version(song_id,version_id)
        route, subject, capability = self._execution_identity(intent,transport_route_id,eligibility_subject_id,eligibility_capability)
        existing = self.by_idempotency_key(key)
        exact = lambda x: (x.intent_fingerprint,x.approval_id,x.approval_source_ref,x.song_id,x.version_id,x.transport_route_id,x.eligibility_subject_id,x.eligibility_capability) == (intent.intent_fingerprint,approval.approval_id,approval.source_ref,song_id,version_id,route,subject,capability)
        if existing is not None:
            if exact(existing): return existing
            raise OperationError("idempotency key is already bound to a different operation")
        op = _id("op")
        try:
            with self.store._tx():
                self._conn.execute("INSERT INTO operations(id,idempotency_key,intent_fingerprint,approval_id,approval_source_ref,song_id,version_id,transport_route_id,eligibility_subject_id,eligibility_capability) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (op,key,intent.intent_fingerprint,approval.approval_id,approval.source_ref,song_id,version_id,route,subject,capability))
                self._append(op,"PREPARED",approval.source_ref)
        except sqlite3.IntegrityError as exc:
            existing = self.by_idempotency_key(key)
            if existing is not None and exact(existing): return existing
            raise OperationError("cannot prepare operation with this idempotency key") from exc
        return self.get(op)

    def claim_execution(self, operation_id: str, *, intent: ActionIntent, approval: ApprovalBinding, claim_evidence_ref: str,
                        transport_decision: TransportDecision | None = None, eligibility_decision: ExecutionEligibilityDecision | None = None) -> OperationRecord:
        evidence = _text(claim_evidence_ref,"claim_evidence_ref")
        with self.store._tx():
            op = self.get(operation_id); self._approval(intent,approval)
            if (op.intent_fingerprint,op.approval_id,op.approval_source_ref) != (intent.intent_fingerprint,approval.approval_id,approval.source_ref):
                raise OperationError("operation is bound to a different intent or approval")
            self._gates(op,intent,transport_decision,eligibility_decision)
            if op.recorded_state != "PREPARED":
                raise DuplicateExecutionError(f"operation cannot be claimed from state {op.recorded_state}")
            self._append(operation_id,"EXECUTION_CLAIMED",evidence)
        return self.get(operation_id)

    def _terminal(self, operation_id: str, event: str, *, evidence_ref: str, receipt_ref: str | None = None, result_fingerprint: str | None = None) -> OperationRecord:
        evidence, receipt, result = _text(evidence_ref,"evidence_ref"), _optional(receipt_ref,"receipt_ref"), _optional(result_fingerprint,"result_fingerprint")
        if event == "SUCCEEDED" and (receipt is None or result is None):
            raise ValidationError("successful operation requires receipt_ref and result_fingerprint")
        with self.store._tx():
            op = self.get(operation_id)
            if op.recorded_state != "EXECUTING": raise OperationError(f"{event} cannot be recorded from state {op.recorded_state}")
            self._append(operation_id,event,evidence,receipt,result)
        return self.get(operation_id)

    def complete_success(self, operation_id: str, *, receipt_ref: str, evidence_ref: str, result_fingerprint: str) -> OperationRecord:
        return self._terminal(operation_id,"SUCCEEDED",evidence_ref=evidence_ref,receipt_ref=receipt_ref,result_fingerprint=result_fingerprint)

    def complete_failure(self, operation_id: str, *, evidence_ref: str, receipt_ref: str | None = None, result_fingerprint: str | None = None) -> OperationRecord:
        return self._terminal(operation_id,"FAILED",evidence_ref=evidence_ref,receipt_ref=receipt_ref,result_fingerprint=result_fingerprint)

    def mark_unknown(self, operation_id: str, *, evidence_ref: str, receipt_ref: str | None = None) -> OperationRecord:
        return self._terminal(operation_id,"UNKNOWN",evidence_ref=evidence_ref,receipt_ref=receipt_ref)

    def reconcile_unknown(self, operation_id: str, *, observed_outcome: str, evidence_ref: str, receipt_ref: str | None = None, result_fingerprint: str | None = None) -> OperationRecord:
        outcome = str(observed_outcome).strip().upper()
        if outcome not in {"SUCCEEDED","FAILED"}: raise OperationError("observed_outcome must be SUCCEEDED or FAILED")
        evidence, receipt, result = _text(evidence_ref,"evidence_ref"), _optional(receipt_ref,"receipt_ref"), _optional(result_fingerprint,"result_fingerprint")
        if outcome == "SUCCEEDED" and (receipt is None or result is None): raise OperationError("reconciled success requires receipt_ref and result_fingerprint")
        with self.store._tx():
            op = self.get(operation_id)
            if op.recorded_state != "UNKNOWN" or op.reconciled: raise OperationError("only unreconciled UNKNOWN operations may be reconciled")
            self._append(operation_id,f"RECONCILED_{outcome}",evidence,receipt,result)
        return self.get(operation_id)
