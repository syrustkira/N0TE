from __future__ import annotations

import os
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from mcp.server import MCPServer

from governance.compiled_context_provider import CanonicalCompiledContextProvider
from governance.execution_envelope import evaluate_execution_envelope
from governance.execution_permit import ExecutionPermitAuthority, SQLitePermitLedger
from governance.trusted_context import FileTrustedContextProvider

from .audio_engineering import EngineeringEvidenceBinding, EngineeringSnapshot
from .authority import ActionIntent, ApprovalBinding
from .coordinator_gateway import ReferenceDiscoveryGateway
from .host_observation import HostObservationBinding
from .hosts import HostRuntimeIdentity
from .network import NetworkPolicy, NetworkRoute
from .openai_web_reference_provider import (
    DEFAULT_OPENAI_WEB_REFERENCE_MODEL,
    OpenAIWebReferenceProvider,
)
from .reference_calibration import ReferenceCalibrationProfile, ReferenceDiscoveryProvider
from .reference_providers import HttpJsonReferenceProvider
from .session_reference_calibration import derive_session_calibration
from .shadow import SHADOW_ACTORS, SHADOW_OBJECT_KINDS, HostShadowState, ShadowFact

mcp = MCPServer(
    "N0TE Coordinator Gate",
    instructions=(
        "CONTINUE means compile the current canonical execution state, resume the current cursor, "
        "invoke the machine-required functions, and use the same compiled context for any stateful permit. "
        "Do not reconstruct the project from model salience and do not create new doctrine for an already-owned rule. "
        "A permit is bound to canonical context and one exact ActionIntent. "
        "Reference discovery is read-only and may use only providers registered by the trusted runtime. "
        "Provider discovery may be model-backed, but calibration/ranking and action authority remain local to N0TE. "
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


def _strict_object(
    raw: object,
    field: str,
    *,
    allowed: set[str],
    required: set[str],
) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{field} must be an object")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{field} contains unsupported fields: {unknown}")
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"{field} is missing required fields: {missing}")
    return raw


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _reference_profile(raw: dict) -> ReferenceCalibrationProfile:
    if not isinstance(raw, dict):
        raise ValueError("target must be an object")
    features = raw.get("features")
    if not isinstance(features, dict):
        raise ValueError("target.features must be an object")
    tags = raw.get("semantic_tags") or ()
    if isinstance(tags, (str, bytes)):
        raise ValueError("target.semantic_tags must be a sequence")
    return ReferenceCalibrationProfile(
        features=tuple(features.items()),
        semantic_tags=tuple(tags),
    )


def _runtime_identity(raw: object) -> HostRuntimeIdentity:
    payload = _strict_object(
        raw,
        "session.binding.runtime",
        allowed={
            "host_family",
            "version",
            "edition",
            "os_name",
            "machine",
            "translation_mode",
            "display_name",
            "generic_host_label",
            "fingerprint",
        },
        required={"host_family", "version", "edition", "os_name", "machine"},
    )
    runtime = HostRuntimeIdentity.from_runtime_labels(
        host_family=_required_text(payload["host_family"], "runtime.host_family"),
        version=_required_text(payload["version"], "runtime.version"),
        edition=_required_text(payload["edition"], "runtime.edition"),
        os_name=_required_text(payload["os_name"], "runtime.os_name"),
        machine=_required_text(payload["machine"], "runtime.machine"),
        translation_mode=_required_text(
            payload.get("translation_mode", "NATIVE"), "runtime.translation_mode"
        ),
        display_name=_optional_text(payload.get("display_name"), "runtime.display_name"),
        generic_host_label=_optional_text(
            payload.get("generic_host_label"), "runtime.generic_host_label"
        ),
    )
    supplied_fingerprint = payload.get("fingerprint")
    if supplied_fingerprint is not None and _required_text(
        supplied_fingerprint, "runtime.fingerprint"
    ) != runtime.fingerprint:
        raise ValueError("session runtime fingerprint does not match runtime identity")
    return runtime


def _session_binding(raw: object) -> HostObservationBinding:
    payload = _strict_object(
        raw,
        "session.binding",
        allowed={
            "workspace_id",
            "song_id",
            "workspace_observation_id",
            "host_runtime_fingerprint",
            "runtime",
        },
        required={
            "workspace_id",
            "song_id",
            "workspace_observation_id",
            "host_runtime_fingerprint",
            "runtime",
        },
    )
    runtime = _runtime_identity(payload["runtime"])
    return HostObservationBinding(
        workspace_id=_required_text(payload["workspace_id"], "binding.workspace_id"),
        song_id=_required_text(payload["song_id"], "binding.song_id"),
        workspace_observation_id=_required_text(
            payload["workspace_observation_id"], "binding.workspace_observation_id"
        ),
        host_runtime_fingerprint=_required_text(
            payload["host_runtime_fingerprint"], "binding.host_runtime_fingerprint"
        ),
        runtime=runtime,
    )


def _shadow_fact(raw: object, index: int) -> ShadowFact:
    payload = _strict_object(
        raw,
        f"session.shadow.facts[{index}]",
        allowed={
            "object_kind",
            "object_ref",
            "field",
            "value",
            "batch_id",
            "actor",
            "evidence_ref",
        },
        required={
            "object_kind",
            "object_ref",
            "field",
            "value",
            "batch_id",
            "actor",
            "evidence_ref",
        },
    )
    object_kind = _required_text(payload["object_kind"], "shadow.fact.object_kind").upper()
    actor = _required_text(payload["actor"], "shadow.fact.actor").upper()
    if object_kind not in SHADOW_OBJECT_KINDS:
        raise ValueError(f"unsupported shadow fact object_kind: {object_kind}")
    if actor not in SHADOW_ACTORS:
        raise ValueError(f"unsupported shadow fact actor: {actor}")
    return ShadowFact(
        object_kind=object_kind,
        object_ref=_required_text(payload["object_ref"], "shadow.fact.object_ref"),
        field=_required_text(payload["field"], "shadow.fact.field"),
        value=payload["value"],
        batch_id=_required_text(payload["batch_id"], "shadow.fact.batch_id"),
        actor=actor,
        evidence_ref=_required_text(payload["evidence_ref"], "shadow.fact.evidence_ref"),
    )


def _session_shadow(raw: object) -> HostShadowState:
    payload = _strict_object(
        raw,
        "session.shadow",
        allowed={
            "status",
            "workspace_id",
            "current_workspace_observation_id",
            "baseline_batch_id",
            "latest_batch_id",
            "facts",
        },
        required={
            "status",
            "workspace_id",
            "current_workspace_observation_id",
            "baseline_batch_id",
            "latest_batch_id",
            "facts",
        },
    )
    facts = payload["facts"]
    if not isinstance(facts, list):
        raise ValueError("session.shadow.facts must be a list")
    return HostShadowState(
        status=_required_text(payload["status"], "shadow.status").upper(),
        workspace_id=_required_text(payload["workspace_id"], "shadow.workspace_id"),
        current_workspace_observation_id=_required_text(
            payload["current_workspace_observation_id"],
            "shadow.current_workspace_observation_id",
        ),
        baseline_batch_id=_optional_text(
            payload["baseline_batch_id"], "shadow.baseline_batch_id"
        ),
        latest_batch_id=_optional_text(
            payload["latest_batch_id"], "shadow.latest_batch_id"
        ),
        facts=tuple(_shadow_fact(item, index) for index, item in enumerate(facts)),
    )


def _engineering_snapshot(raw: object | None) -> EngineeringSnapshot | None:
    if raw is None:
        return None
    payload = _strict_object(
        raw,
        "session.engineering_snapshot",
        allowed={
            "binding",
            "analyzer_version",
            "sample_rate_hz",
            "channels",
            "bits_per_sample",
            "frame_count",
            "duration_seconds",
            "sample_peak_dbfs",
            "rms_dbfs",
            "crest_factor_db",
            "dc_offset_percent",
            "stereo_correlation",
            "integrated_lufs",
            "loudness_state",
            "loudness_standard",
            "loudness_backend",
        },
        required={
            "binding",
            "analyzer_version",
            "sample_rate_hz",
            "channels",
            "bits_per_sample",
            "frame_count",
            "duration_seconds",
            "sample_peak_dbfs",
            "rms_dbfs",
            "crest_factor_db",
            "dc_offset_percent",
            "stereo_correlation",
            "integrated_lufs",
            "loudness_state",
            "loudness_standard",
            "loudness_backend",
        },
    )
    binding_raw = _strict_object(
        payload["binding"],
        "session.engineering_snapshot.binding",
        allowed={"song_id", "version_id", "asset_id", "sha256", "source_size_bytes"},
        required={"song_id", "version_id", "asset_id", "sha256", "source_size_bytes"},
    )
    evidence = EngineeringEvidenceBinding(
        song_id=_required_text(binding_raw["song_id"], "engineering.binding.song_id"),
        version_id=_required_text(
            binding_raw["version_id"], "engineering.binding.version_id"
        ),
        asset_id=_required_text(binding_raw["asset_id"], "engineering.binding.asset_id"),
        sha256=_required_text(binding_raw["sha256"], "engineering.binding.sha256"),
        source_size_bytes=binding_raw["source_size_bytes"],
    )
    return EngineeringSnapshot(
        binding=evidence,
        analyzer_version=_required_text(
            payload["analyzer_version"], "engineering.analyzer_version"
        ),
        sample_rate_hz=payload["sample_rate_hz"],
        channels=payload["channels"],
        bits_per_sample=payload["bits_per_sample"],
        frame_count=payload["frame_count"],
        duration_seconds=payload["duration_seconds"],
        sample_peak_dbfs=payload["sample_peak_dbfs"],
        rms_dbfs=payload["rms_dbfs"],
        crest_factor_db=payload["crest_factor_db"],
        dc_offset_percent=payload["dc_offset_percent"],
        stereo_correlation=payload["stereo_correlation"],
        integrated_lufs=payload["integrated_lufs"],
        loudness_state=_required_text(payload["loudness_state"], "engineering.loudness_state"),
        loudness_standard=_required_text(
            payload["loudness_standard"], "engineering.loudness_standard"
        ),
        loudness_backend=_required_text(
            payload["loudness_backend"], "engineering.loudness_backend"
        ),
    )


def _session_evidence(raw: object):
    payload = _strict_object(
        raw,
        "session",
        allowed={"binding", "shadow", "engineering_snapshot"},
        required={"binding", "shadow"},
    )
    return (
        _session_binding(payload["binding"]),
        _session_shadow(payload["shadow"]),
        _engineering_snapshot(payload.get("engineering_snapshot")),
    )


def _ranked_reference_payload(item) -> dict:
    candidate = item.candidate
    return {
        "title": candidate.title,
        "source_type": candidate.source_type,
        "source_locator": candidate.source_locator,
        "source_kind": candidate.source_kind,
        "source_ref": candidate.source_ref,
        "confidence": candidate.confidence,
        "comparison_dimensions": list(candidate.comparison_dimensions),
        "profile": {
            "features": candidate.profile.feature_map(),
            "semantic_tags": list(candidate.profile.semantic_tags),
        },
        "similarity": item.similarity,
        "matched_features": list(item.matched_features),
        "feature_distances": dict(item.feature_distances),
        "matched_tags": list(item.matched_tags),
    }


def _reference_execution_payload(execution) -> dict:
    return {
        "provider_id": execution.provider_id,
        "route_id": execution.route_id,
        "route_kind": execution.route_kind,
        "registration_source_ref": execution.registration_source_ref,
        "transport_reason_codes": list(execution.transport_reason_codes),
        "read_only": execution.read_only,
        "action_authority_granted": execution.action_authority_granted,
        "primary": (
            _ranked_reference_payload(execution.ranked[0])
            if execution.ranked
            else None
        ),
        "ranked": [_ranked_reference_payload(item) for item in execution.ranked],
    }


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


def _bootstrap_configured_reference_provider(
    gateway: ReferenceDiscoveryGateway,
) -> None:
    raw_backend = os.environ.get("N0TE_REFERENCE_SEARCH_BACKEND")
    endpoint = os.environ.get("N0TE_REFERENCE_SEARCH_ENDPOINT")
    if raw_backend is None:
        if not endpoint:
            return
        backend = "http_json"
    else:
        backend = raw_backend.strip().casefold().replace("-", "_")
        if not backend:
            raise RuntimeError("N0TE_REFERENCE_SEARCH_BACKEND must not be empty")

    default_provider_id = (
        "openai-web-reference-search"
        if backend == "openai_web"
        else "configured-reference-search"
    )
    provider_id = os.environ.get(
        "N0TE_REFERENCE_SEARCH_PROVIDER_ID",
        default_provider_id,
    )
    provider_source_ref = os.environ.get(
        "N0TE_REFERENCE_SEARCH_PROVIDER_SOURCE_REF",
        f"provider:{provider_id}",
    )

    if backend == "http_json":
        if not endpoint:
            raise RuntimeError(
                "http_json reference backend requires N0TE_REFERENCE_SEARCH_ENDPOINT"
            )
        token = os.environ.get("N0TE_REFERENCE_SEARCH_BEARER_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        timeout_seconds = float(
            os.environ.get("N0TE_REFERENCE_SEARCH_TIMEOUT_SECONDS", "8")
        )
        provider = HttpJsonReferenceProvider(
            endpoint=endpoint,
            provider_source_ref=provider_source_ref,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        parsed = urlparse(provider.endpoint)
        host = (parsed.hostname or "").casefold()
        route_kind = (
            "LOCALHOST"
            if host in {"localhost", "127.0.0.1", "::1"}
            else "INTERNET"
        )
        registration_source_ref = "environment:N0TE_REFERENCE_SEARCH_ENDPOINT"
        route_description = "Configured read-only reference discovery endpoint"
    elif backend == "openai_web":
        api_key = os.environ.get("N0TE_OPENAI_API_KEY") or os.environ.get(
            "OPENAI_API_KEY"
        )
        if not api_key:
            raise RuntimeError(
                "openai_web reference backend requires N0TE_OPENAI_API_KEY or OPENAI_API_KEY"
            )
        model = os.environ.get(
            "N0TE_REFERENCE_OPENAI_MODEL",
            DEFAULT_OPENAI_WEB_REFERENCE_MODEL,
        )
        timeout_seconds = float(
            os.environ.get("N0TE_REFERENCE_SEARCH_TIMEOUT_SECONDS", "20")
        )
        provider = OpenAIWebReferenceProvider(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        route_kind = "INTERNET"
        registration_source_ref = (
            "environment:N0TE_REFERENCE_SEARCH_BACKEND:openai_web"
        )
        route_description = "OpenAI web-search reference discovery"
    else:
        raise RuntimeError(f"unsupported reference search backend: {backend}")

    gateway.register(
        provider_id,
        provider=provider,
        route=NetworkRoute(
            route_id=f"reference-search:{provider_id}",
            kind=route_kind,
            description=route_description,
        ),
        registration_source_ref=registration_source_ref,
    )


@lru_cache(maxsize=1)
def _reference_runtime() -> ReferenceDiscoveryGateway:
    mode = os.environ.get("N0TE_NETWORK_MODE", "OFFLINE")
    gateway = ReferenceDiscoveryGateway(network_policy=NetworkPolicy(mode))
    _bootstrap_configured_reference_provider(gateway)
    return gateway


def register_reference_discovery_provider(
    provider_id: str,
    *,
    provider: ReferenceDiscoveryProvider,
    route_id: str,
    route_kind: str,
    route_description: str,
    registration_source_ref: str,
    lan_approval_ref: str | None = None,
) -> None:
    """Trusted bootstrap hook. This is intentionally not an MCP tool."""
    _reference_runtime().register(
        provider_id,
        provider=provider,
        route=NetworkRoute(
            route_id=route_id,
            kind=route_kind,
            description=route_description,
            lan_approval_ref=lan_approval_ref,
        ),
        registration_source_ref=registration_source_ref,
    )


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
def discover_reference_candidates(
    provider_id: str,
    target: dict,
    comparison_dimensions: list[str],
    required_features: list[str] | None = None,
    desired_tags: list[str] | None = None,
    feature_weights: dict[str, float] | None = None,
    discovery_limit: int = 12,
    result_limit: int = 3,
) -> dict:
    """Read from one trusted registered reference provider and rank locally.

    This tool performs no Song write, DAW mutation, provider mutation, publication,
    or purchase. The chosen provider may itself use model-backed read-only discovery,
    but provider output never receives action authority and final calibration/ranking
    remains deterministic inside N0TE. Network policy may deny the route first.
    """
    execution = _reference_runtime().discover_ranked(
        provider_id,
        target=_reference_profile(target),
        comparison_dimensions=tuple(comparison_dimensions),
        required_features=tuple(required_features or ()),
        desired_tags=tuple(desired_tags or ()),
        feature_weights=feature_weights,
        discovery_limit=discovery_limit,
        result_limit=result_limit,
    )
    return _reference_execution_payload(execution)


@mcp.tool()
def discover_session_reference_candidates(
    provider_id: str,
    session: dict,
    comparison_dimensions: list[str],
    semantic_tags: list[str] | None = None,
    required_features: list[str] | None = None,
    desired_tags: list[str] | None = None,
    feature_weights: dict[str, float] | None = None,
    discovery_limit: int = 12,
    result_limit: int = 3,
) -> dict:
    """Current host observation evidence -> calibration -> provider discovery -> local rank.

    The caller supplies canonical observation evidence rather than hand-authored
    calibration coordinates. Stale workspace bindings, cross-Song engineering data,
    conflicting tempo facts, unsupported evidence and missing provenance fail closed.
    """
    binding, shadow, engineering_snapshot = _session_evidence(session)
    derivation = derive_session_calibration(
        binding,
        shadow,
        engineering_snapshot=engineering_snapshot,
        semantic_tags=tuple(semantic_tags or ()),
    )
    execution = _reference_runtime().discover_ranked(
        provider_id,
        target=derivation.profile,
        comparison_dimensions=tuple(comparison_dimensions),
        required_features=tuple(required_features or ()),
        desired_tags=tuple(desired_tags or ()),
        feature_weights=feature_weights,
        discovery_limit=discovery_limit,
        result_limit=result_limit,
    )
    payload = _reference_execution_payload(execution)
    payload["session_calibration"] = {
        "workspace_id": binding.workspace_id,
        "song_id": binding.song_id,
        "workspace_observation_id": binding.workspace_observation_id,
        "host_family": binding.runtime.family,
        "features": derivation.profile.feature_map(),
        "semantic_tags": list(derivation.profile.semantic_tags),
        "evidence": [
            {
                "feature": item.feature,
                "value": item.value,
                "source_refs": list(item.source_refs),
            }
            for item in derivation.evidence
        ],
    }
    return payload


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
    reference_gateway = _reference_runtime()
    return {
        "secret_configured": bool(os.environ.get("N0TE_EXECUTION_GATE_SECRET")),
        "permit_ledger_configured": bool(os.environ.get("N0TE_EXECUTION_PERMIT_DB")),
        "compiled_continue_active": compiled,
        "context_provider": type(contexts).__name__,
        "ungated_mutation_tools_exposed_by_this_server": 0,
        "reference_discovery_network_mode": reference_gateway.network_mode,
        "registered_reference_providers": list(reference_gateway.registered_providers),
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
