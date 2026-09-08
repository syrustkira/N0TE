from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from governance.execution_permit import (
    ExecutionPermitAuthority,
    ExecutionPermitError,
    SQLitePermitLedger,
)
from governance.trusted_context import (
    FileTrustedContextProvider,
    TrustedContextError,
    crosscheck_execution_envelope,
    validate_trusted_context_snapshot,
)
from n0te.authority import ActionIntent, AuthorityService
from n0te.coordinator_gateway import CoordinatorMutationGateway
from n0te.coordinator_mcp import mcp


def envelope():
    return {
        "intent": {
            "description": "Advance professional music acquisition to a verified external state",
            "outcome_class": "EXTERNAL_STATE",
        },
        "active_object": "professional-music-acquisition",
        "retained_scope_refs": [
            "artist",
            "producer",
            "songwriter",
            "engineer",
            "catalog",
            "services",
            "licensing",
            "relationships",
            "audience",
            "business",
        ],
        "dependencies": {
            "upstream": ["full-entry-service-funnel", "typed-inquiry-routing"],
            "downstream": ["qualified-inquiry", "quote", "paid-start"],
        },
        "truth_owners": {
            "WHY": "TELLMEN0TE MASTER CONTEXT",
            "WHAT": "TELLMEN0TE_PUBLIC_SCOPE_CANON",
            "HOW": "N0TE_PRODUCT_DB",
            "NOW": "TELLMEN0TE_OS",
            "PROOF": "LIVE_PROVIDER_EVIDENCE",
        },
        "claims": [
            {
                "statement": "The professional-services inquiry path is live.",
                "truth_type": "CURRENT_FACT",
                "evidence_posture": "OBSERVED",
                "source_refs": ["live:/services", "live:/contact"],
                "blocking": True,
            },
            {
                "statement": "Meaningful acquisition has not yet been run.",
                "truth_type": "CURRENT_FACT",
                "evidence_posture": "OBSERVED",
                "source_refs": ["TELLMEN0TE_OS:REVENUE_PIPELINE"],
                "blocking": False,
            },
        ],
        "required_functions": [
            "epistemology",
            "anti-flattening",
            "offer",
            "discovery",
            "distribution",
            "sales",
            "experiments",
            "friction-memory",
        ],
        "invoked_functions": [
            "epistemology",
            "anti-flattening",
            "offer",
            "discovery",
            "distribution",
            "sales",
            "experiments",
            "friction-memory",
        ],
        "authority": {
            "action_class": "REVERSIBLE",
            "authorized": True,
            "requires_human": False,
            "source_ref": "coordinator:bounded-reversible-authority",
        },
        "acceptance": {
            "outcome_class": "EXTERNAL_STATE",
            "observable_condition": "A bounded acquisition action is actually published/sent through an authorized channel and fresh readback confirms it reached the intended surface.",
            "evidence_required": ["provider-receipt", "fresh-readback"],
            "artifact_is_not_completion": True,
        },
        "next_causal_dependency": {
            "state": "KNOWN",
            "description": "Measure qualified response through inquiry to paid start.",
            "source_ref": "TELLMEN0TE_OS:REVENUE_PIPELINE",
        },
    }


def snapshot_raw():
    env = envelope()
    return {
        "snapshot_id": "ctx-001",
        "source_fingerprint": "sha256:canonical-owner-bundle",
        "observed_at": "2026-09-08T21:00:00Z",
        "expires_at": "2099-09-08T22:00:00Z",
        "retained_scope_refs": env["retained_scope_refs"],
        "truth_owners": env["truth_owners"],
        "policies": {
            "professional-music-acquisition": {
                "required_functions": env["required_functions"],
                "required_dependencies": env["dependencies"],
                "allowed_outcome_classes": ["EXTERNAL_STATE"],
            }
        },
    }


def action():
    return ActionIntent(
        action_id="publish:acq-test-001",
        job_id="music-acquisition-001",
        action_class="REVERSIBLE",
        description="Publish one bounded acquisition test",
        target_ref="channel:test-surface",
        revision_fingerprint="sha256:surface-before",
        payload_fingerprint="sha256:creative-v1",
        destination="channel:test-surface",
        purpose="professional music service acquisition",
        data_categories=("public-marketing-copy",),
    )


def authority(tmp_path, now=None):
    now_fn = (lambda: now[0]) if now is not None else __import__("time").time
    return ExecutionPermitAuthority(
        secret=b"0123456789abcdef0123456789abcdef",
        ledger=SQLitePermitLedger(tmp_path / "permits.sqlite3"),
        now_fn=now_fn,
    )


def context_file(tmp_path):
    path = tmp_path / "trusted-context.json"
    path.write_text(json.dumps({"snapshots": [snapshot_raw()]}))
    return path


def test_trusted_context_rejects_model_scope_omission():
    env = envelope()
    env["retained_scope_refs"].remove("licensing")
    with pytest.raises(TrustedContextError, match="retained scope"):
        crosscheck_execution_envelope(env, validate_trusted_context_snapshot(snapshot_raw()))


def test_trusted_context_rejects_missing_canonical_function_even_when_envelope_is_internally_valid():
    env = envelope()
    env["required_functions"].remove("sales")
    env["invoked_functions"].remove("sales")
    with pytest.raises(TrustedContextError, match="canonically required functions"):
        crosscheck_execution_envelope(env, validate_trusted_context_snapshot(snapshot_raw()))


def test_one_time_permit_is_bound_to_context_and_action_and_cannot_replay(tmp_path):
    permits = authority(tmp_path)
    env = envelope()
    snap = validate_trusted_context_snapshot(snapshot_raw())
    act = action()
    issued = permits.issue(envelope=env, action=act, snapshot=snap, ttl_seconds=300)
    consumed = permits.consume(token=issued.token, envelope=env, action=act, snapshot=snap)
    assert consumed.permit_id == issued.permit_id
    with pytest.raises(ExecutionPermitError, match="already been consumed"):
        permits.consume(token=issued.token, envelope=env, action=act, snapshot=snap)


def test_permit_rejects_action_changed_after_issuance(tmp_path):
    permits = authority(tmp_path)
    env = envelope()
    snap = validate_trusted_context_snapshot(snapshot_raw())
    act = action()
    issued = permits.issue(envelope=env, action=act, snapshot=snap)
    changed = ActionIntent(
        **{
            **act.material_fields(),
            "payload_fingerprint": "sha256:creative-v2",
        }
    )
    with pytest.raises(ExecutionPermitError, match="action_intent_fingerprint"):
        permits.consume(token=issued.token, envelope=env, action=changed, snapshot=snap)


def test_permit_rejects_envelope_changed_after_issuance(tmp_path):
    permits = authority(tmp_path)
    env = envelope()
    snap = validate_trusted_context_snapshot(snapshot_raw())
    act = action()
    issued = permits.issue(envelope=env, action=act, snapshot=snap)
    env["acceptance"]["observable_condition"] = "Different completion condition"
    with pytest.raises(ExecutionPermitError, match="envelope_fingerprint"):
        permits.consume(token=issued.token, envelope=env, action=act, snapshot=snap)


def test_permit_expires_fail_closed(tmp_path):
    clock = [1000]
    permits = authority(tmp_path, clock)
    env = envelope()
    snap = validate_trusted_context_snapshot(snapshot_raw())
    act = action()
    issued = permits.issue(envelope=env, action=act, snapshot=snap, ttl_seconds=1)
    clock[0] = 1002
    with pytest.raises(ExecutionPermitError, match="expired"):
        permits.consume(token=issued.token, envelope=env, action=act, snapshot=snap)


def test_human_required_action_needs_exact_action_approval(tmp_path):
    env = envelope()
    env["authority"].update(
        action_class="IRREVERSIBLE",
        requires_human=True,
        source_ref="artist:explicit-approval-required",
    )
    act = ActionIntent(
        **{
            **action().material_fields(),
            "action_class": "IRREVERSIBLE",
        }
    )
    permits = authority(tmp_path)
    snap = validate_trusted_context_snapshot(snapshot_raw())
    with pytest.raises(ExecutionPermitError, match="lacks exact approval"):
        permits.issue(envelope=env, action=act, snapshot=snap)
    approval = AuthorityService.bind_approval(act, "artist:approval:001")
    issued = permits.issue(envelope=env, action=act, snapshot=snap, approval=approval)
    assert issued.action_intent_fingerprint == act.intent_fingerprint


def test_mutation_gateway_never_calls_executor_without_valid_permit(tmp_path):
    path = context_file(tmp_path)
    contexts = FileTrustedContextProvider(path)
    permits = authority(tmp_path)
    calls = []
    gateway = CoordinatorMutationGateway(permits=permits, contexts=contexts)
    gateway.register(
        "publish-acquisition-test",
        action_class="REVERSIBLE",
        executor=lambda act: calls.append(act.action_id) or "published",
    )
    with pytest.raises(ExecutionPermitError):
        gateway.execute(
            "publish-acquisition-test",
            permit_token="invalid",
            context_snapshot_id="ctx-001",
            envelope=envelope(),
            action=action(),
        )
    assert calls == []

    snap = contexts.get("ctx-001")
    issued = permits.issue(envelope=envelope(), action=action(), snapshot=snap)
    result = gateway.execute(
        "publish-acquisition-test",
        permit_token=issued.token,
        context_snapshot_id="ctx-001",
        envelope=envelope(),
        action=action(),
    )
    assert result.result == "published"
    assert calls == ["publish:acq-test-001"]


def test_mcp_surface_exposes_gate_but_no_mutation_bypass():
    async def check():
        async with Client(mcp, raise_exceptions=True) as client:
            tools = await client.list_tools()
            names = {item.name for item in tools.tools}
            assert "evaluate_execution_gate" in names
            assert "request_execution_permit" in names
            assert not any(name.startswith("execute_") for name in names)
            result = await client.call_tool("evaluate_execution_gate", {"envelope": envelope()})
            assert result.is_error is False

    asyncio.run(check())
