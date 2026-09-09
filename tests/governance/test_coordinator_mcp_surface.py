from __future__ import annotations

import asyncio

from mcp import Client

from n0te.coordinator_mcp import mcp


EXPECTED_GATE_TOOLS = {
    "continue_execution",
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
