from __future__ import annotations

import os
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from mcp.server import MCPServer

from governance.compiled_context_provider import CanonicalCompiledContextProvider
from governance.execution_envelope import evaluate_execution_envelope
from governance.execution_permit import ExecutionPermitAuthority, SQLitePermitLedger
from governance.trusted_context import FileTrustedContextProvider

from .authority import ActionIntent, ApprovalBinding

mcp = MCPServer(
    "N0TE Coordinator Gate",
    instructions=(
        "CONTINUE means compile the current canonical execution state, resume the current cursor, "
        "invoke the machine-required functions, and use the same compiled context for any stateful permit. "
        "Do not reconstruct the project from model salience and do not create new doctrine for an already-owned rule. "
        "A permit is bound to canonical context and one exact ActionIntent. "
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _configured_path(env_name: str, default_relative: str) -> Path:
    value = os.environ.get(env_name)
    return Path(value) if value else _repo_root() / default_relative


def _build_context_provider():
    manifest = _configured_path("N0TE_EXECUTION_MANIFEST_PATH", "governance/execution_manifest.json")
    cursor = _configured_path("N0TE_EXECUTION_CURSOR_PATH", "governance/current_execution_cursor.json")
    facts = _configured_path("N0TE_EXECUTION_FACTS_PATH", "governance/current_execution_facts.json")
    incidents = _configured_path("N0TE_EXECUTION_INCIDENTS_PATH", "governance/current_execution_incidents.json")

    if manifest.is_file() and cursor.is_file() and facts.is_file():
        return CanonicalCompiledContextProvider(
            manifest_path=manifest,
            cursor_path=cursor,
            facts_path=facts,
            incidents_path=incidents if incidents.is_file() else None,
        )

    context_path = os.environ.get("N0TE_TRUSTED_CONTEXT_PATH")
    if not context_path:
        raise RuntimeError(
            "compiled execution sources are unavailable and N0TE_TRUSTED_CONTEXT_PATH is not configured"
        )
    return FileTrustedContextProvider(Path(context_path))


@lru_cache(maxsize=1)
def _runtime():
    secret = os.environ.get("N0TE_EXECUTION_GATE_SECRET")
    ledger_path = os.environ.get("N0TE_EXECUTION_PERMIT_DB")
    if not secret:
        raise RuntimeError("N0TE_EXECUTION_GATE_SECRET is not configured")
    if not ledger_path:
        raise RuntimeError("N0TE_EXECUTION_PERMIT_DB is not configured")
    contexts = _build_context_provider()
    permits = ExecutionPermitAuthority(
        secret=secret.encode("utf-8"),
        ledger=SQLitePermitLedger(Path(ledger_path)),
    )
    return contexts, permits


def _current_snapshot(contexts):
    current = getattr(contexts, "current_snapshot", None)
    if not callable(current):
        raise RuntimeError("compiled CONTINUE context is not configured")
    return current()


@mcp.tool()
def continue_execution() -> dict:
    """Compile and return the one resumable execution packet for CONTINUE.

    This is the normal coordinator entrypoint. The packet retains the whole accepted
    building internally while exposing the current job cursor, mandatory functions,
    decision constraints, current material facts and next causal dependency.
    """
    contexts, _ = _runtime()
    projection_fn = getattr(contexts, "current_projection", None)
    if not callable(projection_fn):
        raise RuntimeError("compiled CONTINUE context is not configured")
    snapshot = _current_snapshot(contexts)
    projection = projection_fn()
    return {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "source_fingerprint": snapshot.source_fingerprint,
        "projection": projection,
    }


@mcp.tool()
def evaluate_execution_gate(envelope: dict) -> dict:
    """Validate the proposed working model without issuing authority."""
    return asdict(evaluate_execution_envelope(envelope))


@mcp.tool()
def inspect_trusted_context(snapshot_id: str) -> dict:
    """Read the canonical snapshot used to cross-check an action."""
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
    envelope: dict,
    action: dict,
    context_snapshot_id: str | None = None,
    approval: dict | None = None,
    ttl_seconds: int = 300,
) -> dict:
    """Issue a short-lived one-time permit for one exact stateful ActionIntent.

    When compiled context is active, callers may omit context_snapshot_id and the
    server binds the permit to the current compiled snapshot. Supplying an old ID
    fails closed after canonical state changes. Human-required actions still need an
    exact ApprovalBinding for the same action; ordinary standing-authority work does
    not acquire a new human checkpoint here.
    """
    contexts, permits = _runtime()
    if context_snapshot_id is None:
        snapshot = _current_snapshot(contexts)
    else:
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
    """Report gate configuration and whether compiled CONTINUE is active."""
    contexts, _ = _runtime()
    compiled = callable(getattr(contexts, "current_projection", None))
    return {
        "secret_configured": bool(os.environ.get("N0TE_EXECUTION_GATE_SECRET")),
        "permit_ledger_configured": bool(os.environ.get("N0TE_EXECUTION_PERMIT_DB")),
        "compiled_continue_active": compiled,
        "context_provider": type(contexts).__name__,
        "ungated_mutation_tools_exposed_by_this_server": 0,
        "stateful_execution_rule": "COMPILE_CURRENT_STATE_THEN_CONSUME_ONE_TIME_PERMIT",
    }


if __name__ == "__main__":
    transport = os.environ.get("N0TE_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        host = os.environ.get("N0TE_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("N0TE_MCP_PORT", "8000"))
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
