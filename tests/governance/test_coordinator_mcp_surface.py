from __future__ import annotations

import asyncio

import pytest
from mcp import Client

import n0te.coordinator_mcp as coordinator_mcp_module
from n0te.coordinator_gateway import (
    CoordinatorReferenceClient,
    CoordinatorReferenceClientError,
    ReferenceDiscoveryGateway,
    ReferenceDiscoveryGatewayError,
)
from n0te.coordinator_mcp import mcp
from n0te.host_observation import HostObservationBinding
from n0te.hosts import HostRuntimeIdentity
from n0te.network import NetworkPolicy, NetworkRoute
from n0te.reference_calibration import (
    ReferenceCalibrationProfile,
    ReferenceCandidate,
)
from n0te.shadow import HostShadowState, ShadowFact


EXPECTED_GATE_TOOLS = {
    "continue_execution",
    "discover_reference_candidates",
    "discover_session_reference_candidates",
    "evaluate_execution_gate",
    "inspect_trusted_context",
    "request_execution_permit",
    "execution_gate_status",
}


def test_coordinator_mcp_tool_surface_is_explicitly_allowlisted():
    async def check():
        async with Client(mcp, raise_exceptions=True) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == EXPECTED_GATE_TOOLS

    asyncio.run(check())


class _Provider:
    def __init__(self):
        self.calls = []

    def discover_references(self, *, target, comparison_dimensions, limit):
        self.calls.append((target, comparison_dimensions, limit))
        return (
            ReferenceCandidate(
                title="Near",
                source_type="CATALOG_RECORDING",
                source_locator="catalog:near",
                profile=ReferenceCalibrationProfile.create(
                    tempo_bpm=128.5,
                    dynamics=0.52,
                    semantic_tags=("electronic",),
                ),
                comparison_dimensions=("tempo", "dynamics"),
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:catalog:near",
            ),
            ReferenceCandidate(
                title="Far",
                source_type="CATALOG_RECORDING",
                source_locator="catalog:far",
                profile=ReferenceCalibrationProfile.create(
                    tempo_bpm=92.0,
                    dynamics=0.9,
                    semantic_tags=("electronic",),
                ),
                comparison_dimensions=("tempo", "dynamics"),
                source_kind="PROVIDER_VERIFIED",
                source_ref="provider:catalog:far",
            ),
        )


def test_reference_discovery_gateway_is_read_only_and_ranks_after_provider_read():
    provider = _Provider()
    gateway = ReferenceDiscoveryGateway(network_policy=NetworkPolicy("CONNECTED"))
    gateway.register(
        "catalog",
        provider=provider,
        route=NetworkRoute(
            route_id="reference-catalog",
            kind="INTERNET",
            description="Read-only reference catalog lookup",
        ),
        registration_source_ref="runtime:reference-provider:catalog",
    )

    target = ReferenceCalibrationProfile.create(
        tempo_bpm=128.0,
        dynamics=0.5,
        semantic_tags=("electronic",),
    )
    execution = gateway.discover_ranked(
        "catalog",
        target=target,
        comparison_dimensions=("tempo", "dynamics"),
        required_features=("tempo_bpm", "dynamics"),
        desired_tags=("electronic",),
        result_limit=2,
    )

    assert provider.calls == [(target, ("TEMPO", "DYNAMICS"), 12)]
    assert [row.candidate.title for row in execution.ranked] == ["Near", "Far"]
    assert execution.read_only is True
    assert execution.action_authority_granted is False
    assert execution.registration_source_ref == "runtime:reference-provider:catalog"
    assert execution.route_kind == "INTERNET"
    assert "CONNECTED_INTERNET_TRANSPORT_ELIGIBLE" in execution.transport_reason_codes


def test_offline_reference_gateway_blocks_internet_before_provider_call():
    provider = _Provider()
    gateway = ReferenceDiscoveryGateway(network_policy=NetworkPolicy("OFFLINE"))
    gateway.register(
        "catalog",
        provider=provider,
        route=NetworkRoute(
            route_id="reference-catalog",
            kind="INTERNET",
            description="Read-only reference catalog lookup",
        ),
        registration_source_ref="runtime:reference-provider:catalog",
    )

    with pytest.raises(ReferenceDiscoveryGatewayError, match="OFFLINE_BLOCKS_INTERNET"):
        gateway.discover_ranked(
            "catalog",
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
        )
    assert provider.calls == []


def test_local_reference_provider_remains_usable_while_offline():
    provider = _Provider()
    gateway = ReferenceDiscoveryGateway(network_policy=NetworkPolicy("OFFLINE"))
    gateway.register(
        "local-library",
        provider=provider,
        route=NetworkRoute(
            route_id="local-library",
            kind="LOCALHOST",
            description="Local reference library index",
        ),
        registration_source_ref="runtime:reference-provider:local-library",
    )

    execution = gateway.discover_ranked(
        "local-library",
        target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
        comparison_dimensions=("tempo",),
        result_limit=1,
    )
    assert provider.calls
    assert execution.route_kind == "LOCALHOST"
    assert "LOCALHOST_PERMITTED" in execution.transport_reason_codes
    assert execution.action_authority_granted is False


def test_reference_runtime_bootstraps_configured_http_provider(monkeypatch):
    constructed = {}

    class _ConfiguredProvider(_Provider):
        def __init__(
            self,
            *,
            endpoint,
            provider_source_ref,
            headers,
            timeout_seconds,
        ):
            super().__init__()
            self.endpoint = endpoint
            constructed.update(
                endpoint=endpoint,
                provider_source_ref=provider_source_ref,
                headers=headers,
                timeout_seconds=timeout_seconds,
                instance=self,
            )

    monkeypatch.setattr(
        coordinator_mcp_module,
        "HttpJsonReferenceProvider",
        _ConfiguredProvider,
    )
    monkeypatch.setenv("N0TE_NETWORK_MODE", "CONNECTED")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_BACKEND", "http_json")
    monkeypatch.setenv(
        "N0TE_REFERENCE_SEARCH_ENDPOINT",
        "https://references.example.test/search",
    )
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_PROVIDER_ID", "web-reference-search")
    monkeypatch.setenv(
        "N0TE_REFERENCE_SEARCH_PROVIDER_SOURCE_REF",
        "provider:web-reference-search:v1",
    )
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_BEARER_TOKEN", "secret-test-token")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_TIMEOUT_SECONDS", "4.5")
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        gateway = coordinator_mcp_module._reference_runtime()
        assert gateway.network_mode == "CONNECTED"
        assert gateway.registered_providers == ("web-reference-search",)
        assert constructed == {
            "endpoint": "https://references.example.test/search",
            "provider_source_ref": "provider:web-reference-search:v1",
            "headers": {"Authorization": "Bearer secret-test-token"},
            "timeout_seconds": 4.5,
            "instance": constructed["instance"],
        }

        execution = gateway.discover_ranked(
            "web-reference-search",
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            result_limit=1,
        )
        assert execution.route_kind == "INTERNET"
        assert execution.registration_source_ref == (
            "environment:N0TE_REFERENCE_SEARCH_ENDPOINT"
        )
        assert constructed["instance"].calls
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def test_bootstrapped_internet_provider_still_obeys_offline_mode(monkeypatch):
    instance = _Provider()

    class _ConfiguredProvider:
        def __init__(self, *, endpoint, provider_source_ref, headers, timeout_seconds):
            self.endpoint = endpoint

        def discover_references(self, *, target, comparison_dimensions, limit):
            return instance.discover_references(
                target=target,
                comparison_dimensions=comparison_dimensions,
                limit=limit,
            )

    monkeypatch.setattr(
        coordinator_mcp_module,
        "HttpJsonReferenceProvider",
        _ConfiguredProvider,
    )
    monkeypatch.setenv("N0TE_NETWORK_MODE", "OFFLINE")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_BACKEND", "http_json")
    monkeypatch.setenv(
        "N0TE_REFERENCE_SEARCH_ENDPOINT",
        "https://references.example.test/search",
    )
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_PROVIDER_ID", raising=False)
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        gateway = coordinator_mcp_module._reference_runtime()
        with pytest.raises(
            ReferenceDiscoveryGatewayError,
            match="OFFLINE_BLOCKS_INTERNET",
        ):
            gateway.discover_ranked(
                "configured-reference-search",
                target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
                comparison_dimensions=("tempo",),
            )
        assert instance.calls == []
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def test_reference_runtime_bootstraps_openai_web_without_endpoint(monkeypatch):
    constructed = {}

    class _OpenAIProvider(_Provider):
        def __init__(self, *, api_key, model, timeout_seconds):
            super().__init__()
            constructed.update(
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                instance=self,
            )

    monkeypatch.setattr(
        coordinator_mcp_module,
        "OpenAIWebReferenceProvider",
        _OpenAIProvider,
    )
    monkeypatch.setenv("N0TE_NETWORK_MODE", "CONNECTED")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_BACKEND", "openai_web")
    monkeypatch.setenv("N0TE_OPENAI_API_KEY", "top-secret-test-key")
    monkeypatch.setenv("N0TE_REFERENCE_OPENAI_MODEL", "reference-model-test")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_TIMEOUT_SECONDS", "6.5")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_PROVIDER_ID", "public-web")
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        gateway = coordinator_mcp_module._reference_runtime()
        assert gateway.network_mode == "CONNECTED"
        assert gateway.registered_providers == ("public-web",)
        assert constructed["api_key"] == "top-secret-test-key"
        assert constructed["model"] == "reference-model-test"
        assert constructed["timeout_seconds"] == 6.5

        execution = gateway.discover_ranked(
            "public-web",
            target=ReferenceCalibrationProfile.create(tempo_bpm=128.0),
            comparison_dimensions=("tempo",),
            result_limit=1,
        )
        assert execution.route_kind == "INTERNET"
        assert execution.registration_source_ref == (
            "environment:N0TE_REFERENCE_SEARCH_BACKEND:openai_web"
        )
        assert constructed["instance"].calls
        assert "top-secret-test-key" not in repr(execution)
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def test_openai_web_backend_fails_closed_without_runtime_api_key(monkeypatch):
    monkeypatch.setenv("N0TE_NETWORK_MODE", "CONNECTED")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_BACKEND", "openai_web")
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("N0TE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="requires N0TE_OPENAI_API_KEY or OPENAI_API_KEY"):
            coordinator_mcp_module._reference_runtime()
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def test_unknown_reference_backend_fails_closed(monkeypatch):
    monkeypatch.setenv("N0TE_NETWORK_MODE", "CONNECTED")
    monkeypatch.setenv("N0TE_REFERENCE_SEARCH_BACKEND", "mystery-provider")
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_ENDPOINT", raising=False)
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="unsupported reference search backend"):
            coordinator_mcp_module._reference_runtime()
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def _session_bundle(*, observation_id: str = "observation:1") -> dict:
    runtime = HostRuntimeIdentity.from_runtime_labels(
        host_family="ABLETON_LIVE",
        version="12.1",
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    )
    return {
        "binding": {
            "workspace_id": "workspace:1",
            "song_id": "song:1",
            "workspace_observation_id": "observation:1",
            "host_runtime_fingerprint": runtime.fingerprint,
            "runtime": {
                "host_family": "ABLETON_LIVE",
                "version": "12.1",
                "edition": "Standard",
                "os_name": "Darwin",
                "machine": "arm64",
                "fingerprint": runtime.fingerprint,
            },
        },
        "shadow": {
            "status": "CURRENT",
            "workspace_id": "workspace:1",
            "current_workspace_observation_id": observation_id,
            "baseline_batch_id": "batch:1",
            "latest_batch_id": "batch:1",
            "facts": [
                {
                    "object_kind": "TEMPO",
                    "object_ref": "tempo:main",
                    "field": "bpm",
                    "value": 128.0,
                    "batch_id": "batch:1",
                    "actor": "EXTERNAL",
                    "evidence_ref": "host:ableton:tempo",
                }
            ],
        },
    }


def _typed_session() -> tuple[HostObservationBinding, HostShadowState]:
    runtime = HostRuntimeIdentity.from_runtime_labels(
        host_family="ABLETON_LIVE",
        version="12.1",
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    )
    binding = HostObservationBinding(
        workspace_id="workspace:1",
        song_id="song:1",
        workspace_observation_id="observation:1",
        host_runtime_fingerprint=runtime.fingerprint,
        runtime=runtime,
    )
    shadow = HostShadowState(
        status="CURRENT",
        workspace_id="workspace:1",
        current_workspace_observation_id="observation:1",
        baseline_batch_id="batch:1",
        latest_batch_id="batch:1",
        facts=(
            ShadowFact(
                object_kind="TEMPO",
                object_ref="tempo:main",
                field="bpm",
                value=128.0,
                batch_id="batch:1",
                actor="EXTERNAL",
                evidence_ref="host:ableton:tempo",
            ),
        ),
    )
    return binding, shadow


def test_session_reference_tool_derives_calibration_from_host_observation(monkeypatch):
    provider = _Provider()
    monkeypatch.setenv("N0TE_NETWORK_MODE", "OFFLINE")
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_PROVIDER_ID", raising=False)
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        coordinator_mcp_module.register_reference_discovery_provider(
            "session-local",
            provider=provider,
            route_id="session-local-library",
            route_kind="LOCALHOST",
            route_description="test local session references",
            registration_source_ref="test:session-local",
        )
        result = coordinator_mcp_module.discover_session_reference_candidates(
            "session-local",
            _session_bundle(),
            ["tempo"],
            semantic_tags=["electronic"],
            required_features=["tempo_bpm"],
            desired_tags=["electronic"],
            result_limit=2,
        )
        assert provider.calls
        assert provider.calls[0][0].feature_map() == {"TEMPO_BPM": 128.0}
        assert result["session_calibration"]["features"] == {"TEMPO_BPM": 128.0}
        assert result["session_calibration"]["evidence"] == [
            {
                "feature": "TEMPO_BPM",
                "value": 128.0,
                "source_refs": ["host:ableton:tempo"],
            }
        ]
        assert result["primary"]["title"] == "Near"
        assert result["read_only"] is True
        assert result["action_authority_granted"] is False
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def test_session_reference_tool_rejects_stale_host_shadow_before_provider_read(monkeypatch):
    provider = _Provider()
    monkeypatch.setenv("N0TE_NETWORK_MODE", "OFFLINE")
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_ENDPOINT", raising=False)
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        coordinator_mcp_module.register_reference_discovery_provider(
            "session-local",
            provider=provider,
            route_id="session-local-library",
            route_kind="LOCALHOST",
            route_description="test local session references",
            registration_source_ref="test:session-local",
        )
        with pytest.raises(ValueError, match="stale"):
            coordinator_mcp_module.discover_session_reference_candidates(
                "session-local",
                _session_bundle(observation_id="observation:old"),
                ["tempo"],
            )
        assert provider.calls == []
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def test_session_bundle_rejects_hand_authored_calibration_fields():
    session = _session_bundle()
    session["calibration"] = {"TEMPO_BPM": 160.0}
    with pytest.raises(ValueError, match="unsupported fields"):
        coordinator_mcp_module._session_evidence(session)


def test_host_side_reference_client_refuses_non_loopback_coordinator():
    with pytest.raises(CoordinatorReferenceClientError, match="loopback"):
        CoordinatorReferenceClient("https://coordinator.example.test/mcp")
    with pytest.raises(CoordinatorReferenceClientError, match="/mcp"):
        CoordinatorReferenceClient("http://127.0.0.1:8000/reference-search")


def test_host_side_reference_client_roundtrips_typed_session_through_mcp(monkeypatch):
    provider = _Provider()
    monkeypatch.setenv("N0TE_NETWORK_MODE", "OFFLINE")
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_BACKEND", raising=False)
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("N0TE_REFERENCE_SEARCH_PROVIDER_ID", raising=False)
    coordinator_mcp_module._reference_runtime.cache_clear()
    try:
        coordinator_mcp_module.register_reference_discovery_provider(
            "session-local",
            provider=provider,
            route_id="session-local-library",
            route_kind="LOCALHOST",
            route_description="test local session references",
            registration_source_ref="test:session-local",
        )

        def in_process_client(_endpoint, **kwargs):
            return Client(mcp, **kwargs)

        host_client = CoordinatorReferenceClient(
            client_factory=in_process_client,
        )
        binding, shadow = _typed_session()
        result = asyncio.run(
            host_client.discover_session(
                "session-local",
                binding,
                shadow,
                comparison_dimensions=("tempo",),
                semantic_tags=("electronic",),
                required_features=("tempo_bpm",),
                desired_tags=("electronic",),
                result_limit=2,
            )
        )

        assert provider.calls
        assert provider.calls[0][0].feature_map() == {"TEMPO_BPM": 128.0}
        assert result["provider_id"] == "session-local"
        assert result["session_calibration"]["workspace_id"] == "workspace:1"
        assert result["session_calibration"]["song_id"] == "song:1"
        assert result["session_calibration"]["workspace_observation_id"] == "observation:1"
        assert result["primary"]["title"] == "Near"
        assert result["read_only"] is True
        assert result["action_authority_granted"] is False
    finally:
        coordinator_mcp_module._reference_runtime.cache_clear()


def test_host_side_reference_client_rejects_forged_action_authority():
    class _ForgedResult:
        structured_content = {
            "provider_id": "session-local",
            "read_only": True,
            "action_authority_granted": True,
            "primary": None,
            "ranked": [],
            "session_calibration": {
                "workspace_id": "workspace:1",
                "song_id": "song:1",
                "workspace_observation_id": "observation:1",
            },
        }

    class _ForgedClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def call_tool(self, name, arguments):
            assert name == "discover_session_reference_candidates"
            return _ForgedResult()

    def forged_factory(_endpoint, **_kwargs):
        return _ForgedClient()

    host_client = CoordinatorReferenceClient(client_factory=forged_factory)
    binding, shadow = _typed_session()
    with pytest.raises(CoordinatorReferenceClientError, match="action authority"):
        asyncio.run(
            host_client.discover_session(
                "session-local",
                binding,
                shadow,
                comparison_dimensions=("tempo",),
            )
        )
