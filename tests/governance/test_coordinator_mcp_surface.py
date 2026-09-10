from __future__ import annotations

import asyncio

import pytest
from mcp import Client

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
