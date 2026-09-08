from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from n0te.authority import ActionIntent, ApprovalBinding, AuthorityService

from .execution_envelope import require_execution_permit
from .trusted_context import TrustedContextSnapshot, crosscheck_execution_envelope


class ExecutionPermitError(ValueError):
    """A stateful action does not possess a valid one-time execution permit."""


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise ExecutionPermitError("malformed execution permit encoding") from exc


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class IssuedExecutionPermit:
    token: str
    permit_id: str
    issued_at: int
    expires_at: int
    envelope_fingerprint: str
    context_snapshot_fingerprint: str
    action_intent_fingerprint: str
    action_class: str


@dataclass(frozen=True)
class ConsumedExecutionPermit:
    permit_id: str
    consumed_at: int
    envelope_fingerprint: str
    action_intent_fingerprint: str


class SQLitePermitLedger:
    """Persistent one-time-use ledger. Failure to read/write the ledger fails closed."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ExecutionPermitError("permit ledger path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS consumed_execution_permits (
                        permit_id TEXT PRIMARY KEY,
                        consumed_at INTEGER NOT NULL,
                        envelope_fingerprint TEXT NOT NULL,
                        action_intent_fingerprint TEXT NOT NULL
                    )"""
                )
        except sqlite3.DatabaseError as exc:
            raise ExecutionPermitError("permit ledger is unavailable") from exc

    def consume(self, *, permit_id: str, consumed_at: int, envelope_fingerprint: str, action_intent_fingerprint: str):
        try:
            with sqlite3.connect(self.path, isolation_level="IMMEDIATE") as conn:
                conn.execute("BEGIN IMMEDIATE")
                exists = conn.execute(
                    "SELECT 1 FROM consumed_execution_permits WHERE permit_id=?",
                    (permit_id,),
                ).fetchone()
                if exists is not None:
                    raise ExecutionPermitError("execution permit has already been consumed")
                conn.execute(
                    "INSERT INTO consumed_execution_permits(permit_id,consumed_at,envelope_fingerprint,action_intent_fingerprint) VALUES(?,?,?,?)",
                    (permit_id, consumed_at, envelope_fingerprint, action_intent_fingerprint),
                )
                conn.commit()
        except ExecutionPermitError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionPermitError("permit ledger failed closed") from exc


class ExecutionPermitAuthority:
    VERSION = 1

    def __init__(self, *, secret: bytes, ledger: SQLitePermitLedger, now_fn=time.time):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ExecutionPermitError("execution permit secret must be at least 32 bytes")
        if not isinstance(ledger, SQLitePermitLedger):
            raise TypeError("ledger must be SQLitePermitLedger")
        self._secret = secret
        self._ledger = ledger
        self._now = now_fn

    def _sign(self, payload_bytes: bytes) -> bytes:
        return hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()

    def _decode(self, token: str):
        if not isinstance(token, str) or token.count(".") != 1:
            raise ExecutionPermitError("malformed execution permit")
        payload_part, signature_part = token.split(".", 1)
        payload_bytes = _b64decode(payload_part)
        signature = _b64decode(signature_part)
        if not hmac.compare_digest(signature, self._sign(payload_bytes)):
            raise ExecutionPermitError("execution permit signature is invalid")
        try:
            payload = json.loads(payload_bytes)
        except json.JSONDecodeError as exc:
            raise ExecutionPermitError("execution permit payload is invalid") from exc
        if payload.get("version") != self.VERSION:
            raise ExecutionPermitError("unsupported execution permit version")
        return payload

    @staticmethod
    def _validate_action_binding(envelope, action: ActionIntent):
        if not isinstance(action, ActionIntent):
            raise TypeError("action must be ActionIntent")
        envelope_class = str(envelope["authority"]["action_class"]).strip().upper()
        if envelope_class != action.action_class:
            raise ExecutionPermitError(
                f"action class mismatch envelope={envelope_class} action={action.action_class}"
            )

    @staticmethod
    def _validate_required_human_approval(envelope, action: ActionIntent, approval: ApprovalBinding | None):
        if envelope["authority"]["requires_human"] is not True:
            return
        if approval is None:
            raise ExecutionPermitError("human-required action lacks exact approval binding")
        validation = AuthorityService.validate(action, approval)
        if validation.status != "VALID":
            raise ExecutionPermitError("human approval is stale for the current action intent")

    def issue(
        self,
        *,
        envelope,
        action: ActionIntent,
        snapshot: TrustedContextSnapshot,
        approval: ApprovalBinding | None = None,
        ttl_seconds: int = 300,
    ) -> IssuedExecutionPermit:
        if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 900:
            raise ExecutionPermitError("ttl_seconds must be between 1 and 900")
        gate = require_execution_permit(envelope)
        crosscheck_execution_envelope(envelope, snapshot)
        self._validate_action_binding(envelope, action)
        if action.action_class == "READ_ONLY":
            raise ExecutionPermitError("read-only actions do not require stateful execution permits")
        self._validate_required_human_approval(envelope, action, approval)

        issued_at = int(self._now())
        payload = {
            "version": self.VERSION,
            "permit_id": f"permit_{uuid.uuid4().hex}",
            "issued_at": issued_at,
            "expires_at": issued_at + ttl_seconds,
            "envelope_fingerprint": gate.envelope_fingerprint,
            "context_snapshot_fingerprint": snapshot.fingerprint,
            "action_intent_fingerprint": action.intent_fingerprint,
            "action_class": action.action_class,
            "authority_source_ref": str(envelope["authority"]["source_ref"]).strip(),
        }
        payload_bytes = _canonical(payload)
        token = f"{_b64encode(payload_bytes)}.{_b64encode(self._sign(payload_bytes))}"
        return IssuedExecutionPermit(token=token, **{key: payload[key] for key in (
            "permit_id",
            "issued_at",
            "expires_at",
            "envelope_fingerprint",
            "context_snapshot_fingerprint",
            "action_intent_fingerprint",
            "action_class",
        )})

    def consume(
        self,
        *,
        token: str,
        envelope,
        action: ActionIntent,
        snapshot: TrustedContextSnapshot,
    ) -> ConsumedExecutionPermit:
        gate = require_execution_permit(envelope)
        crosscheck_execution_envelope(envelope, snapshot)
        self._validate_action_binding(envelope, action)
        payload = self._decode(token)
        now = int(self._now())
        if now > int(payload["expires_at"]):
            raise ExecutionPermitError("execution permit has expired")
        expected = {
            "envelope_fingerprint": gate.envelope_fingerprint,
            "context_snapshot_fingerprint": snapshot.fingerprint,
            "action_intent_fingerprint": action.intent_fingerprint,
            "action_class": action.action_class,
        }
        for field, current in expected.items():
            if payload.get(field) != current:
                raise ExecutionPermitError(f"execution permit no longer matches {field}")

        permit_id = str(payload.get("permit_id", "")).strip()
        if not permit_id:
            raise ExecutionPermitError("execution permit has no permit_id")
        self._ledger.consume(
            permit_id=permit_id,
            consumed_at=now,
            envelope_fingerprint=gate.envelope_fingerprint,
            action_intent_fingerprint=action.intent_fingerprint,
        )
        return ConsumedExecutionPermit(
            permit_id=permit_id,
            consumed_at=now,
            envelope_fingerprint=gate.envelope_fingerprint,
            action_intent_fingerprint=action.intent_fingerprint,
        )
