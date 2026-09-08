from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from mcp.server import MCPServer

from governance.execution_envelope import evaluate_execution_envelope
from governance.execution_permit import ExecutionPermitAuthority, SQLitePermitLedger
from governance.trusted_context import FileTrustedContextProvider

from .authority import ActionIntent, ApprovalBinding

mcp = MCPServer(
    "N0TE Coordinator Gate",
    instructions=(
        "Use the execution gate before any stateful coordinator action. "
        "A permit is bound to trusted canonical context and one exact ActionIntent. "
        "This server intentionally exposes no ungated mutation tool."
    ),
)


def _action(raw: dict) -> ActionIntent:
    if not isinstance(raw, dict):
        raise ValueError("action must be an object")
    return ActionIntent(
        action_id=raw.get("action_id"),
        job_id=raw.get("job_id"),
        action_class=raw.get("action_class"),
        description=raw.get("description"),
        target_ref=raw.get("target_ref"),
        revision_fingerprint=raw.get("revision_fingerprint"),
        payload_fingerprint=raw.get("payload_fingerprint"),
        destination=raw.get("destination"),
        purpose=raw.get("purpose"),
        data_categories=tuple(raw.get("data_categories") or ()),
    )


def _approval(raw: dict | None) -> ApprovalBinding | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("approval must be an object")
    return ApprovalBinding(
        approval_id=raw.get("approval_id"),
        intent_fingerprint=raw.get("intent_fingerprint"),
        source_ref=raw.get("source_ref"),
    )


def _runtime():
    secret = os.environ.get("N0TE_EXECUTION_GATE_SECRET")
    context_path = os.environ.get("N0TE_TRUSTED_CONTEXT_PATH")
    ledger_path = os.environ.get("N0TE_EXECUTION_PERMIT_DB")
    if not secret:
        raise RuntimeError("N0TE_EXECUTION_GATE_SECRET is not configured")
    if not context_path:
        raise RuntimeError("N0TE_TRUSTED_CONTEXT_PATH is not configured")
    if not ledger_path:
        raise RuntimeError("N0TE_EXECUTION_PERMIT_DB is not configured")
    contexts = FileTrustedContextProvider(Path(context_path))
    permits = ExecutionPermitAuthority(
        secret=secret.encode("utf-8"),
        ledger=SQLitePermitLedger(Path(ledger_path)),
    )
    return contexts, permits


@mcp.tool()
def evaluate_execution_gate(envelope: dict) -> dict:
    """Validate the proposed working model without issuing authority."""
    return asdict(evaluate_execution_envelope(envelope))


@mcp.tool()
def inspect_trusted_context(snapshot_id: str) -> dict:
    """Read the server-owned canonical snapshot used to cross-check an action."""
    contexts, _ = _runtime()
    snapshot = contexts.get(snapshot_id)
    return {
        "snapshot_id": snapshot.snapshot_id,
        "source_fingerprint": snapshot.source_fingerprint,
        "observed_at": snapshot.observed_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "retained_scope_refs": sorted(snapshot.retained_scope_refs),
        "truth_owners": dict(snapshot.truth_owners),
        "active_objects": sorted(snapshot.policies),
        "snapshot_fingerprint": snapshot.fingerprint,
    }


@mcp.tool()
def request_execution_permit(
    context_snapshot_id: str,
    envelope: dict,
    action: dict,
    approval: dict | None = None,
    ttl_seconds: int = 300,
) -> dict:
    """Issue a short-lived one-time permit for one exact stateful ActionIntent.

    The caller cannot establish canonical scope by filling in the envelope alone.
    The envelope is cross-checked against a server-owned trusted context snapshot.
    Human-required actions also need an exact ApprovalBinding for the same action.
    """
    contexts, permits = _runtime()
    snapshot = contexts.get(context_snapshot_id)
    issued = permits.issue(
        envelope=envelope,
        action=_action(action),
        snapshot=snapshot,
        approval=_approval(approval),
        ttl_seconds=ttl_seconds,
    )
    return asdict(issued)


@mcp.tool()
def execution_gate_status() -> dict:
    """Report whether the permit service is configured, without exposing secrets."""
    return {
        "secret_configured": bool(os.environ.get("N0TE_EXECUTION_GATE_SECRET")),
        "trusted_context_configured": bool(os.environ.get("N0TE_TRUSTED_CONTEXT_PATH")),
        "permit_ledger_configured": bool(os.environ.get("N0TE_EXECUTION_PERMIT_DB")),
        "ungated_mutation_tools_exposed_by_this_server": 0,
        "stateful_execution_rule": "REGISTERED_MUTATIONS_MUST_CONSUME_ONE_TIME_PERMIT",
    }


if __name__ == "__main__":
    transport = os.environ.get("N0TE_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        host = os.environ.get("N0TE_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("N0TE_MCP_PORT", "8000"))
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
