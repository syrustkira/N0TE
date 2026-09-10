from __future__ import annotations

import asyncio

import pytest
from mcp import Client

import n0te.coordinator_mcp as coordinator_mcp_module
from n0te.coordinator_gateway import (
    ReferenceDiscoveryGateway,
    ReferenceDiscoveryGatewayError,
)
from n0te.coordinator_mcp import mcp
from n0te.network import NetworkPolicy, NetworkRoute
from n0te.reference_calibration import (
    ReferenceCalibrationProfile,
    ReferenceCandidate,
)


EXPECTED_GATE_TOOLS = {
    "continue_execution",
    "discover_reference_candidates",
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
