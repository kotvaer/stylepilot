from __future__ import annotations

import asyncio
from typing import cast

import pytest
from mcp import ClientSession
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from stylepilot.adapters.lightroom import (
    LightroomConnectionError,
    LightroomProtocolError,
    StdioMcpToolClient,
)


class FakeSession:
    def __init__(self, result: object) -> None:
        self.result = result

    async def call_tool(self, *_args: object, **_kwargs: object) -> object:
        return self.result

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(
            tools=[Tool(name="get_selected_photos", input_schema={"type": "object"})]
        )


def connected_client(result: object) -> StdioMcpToolClient:
    client = StdioMcpToolClient()
    client._session = cast(ClientSession, FakeSession(result))
    return client


def tool_result(text: str, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=text)],
        is_error=is_error,
    )


def test_parses_upstream_text_json_result() -> None:
    client = connected_client(tool_result('{"count": 0, "photos": []}'))

    payload = asyncio.run(client.call_tool("get_selected_photos", {}))

    assert payload == {"count": 0, "photos": []}


def test_prefers_structured_content() -> None:
    result = CallToolResult(
        content=[TextContent(text="ignored")],
        structured_content={"success": True},
    )
    client = connected_client(result)

    assert asyncio.run(client.call_tool("test", {})) == {"success": True}


def test_surfaces_mcp_tool_error() -> None:
    client = connected_client(tool_result("plugin offline", is_error=True))

    with pytest.raises(LightroomConnectionError, match="plugin offline"):
        asyncio.run(client.call_tool("get_selected_photos", {}))


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (tool_result("not-json"), "invalid JSON"),
        (tool_result("[]"), "JSON object"),
        (CallToolResult(content=[]), "no JSON content"),
        (object(), "unsupported result type"),
    ],
)
def test_rejects_invalid_tool_results(result: object, message: str) -> None:
    client = connected_client(result)

    with pytest.raises(LightroomProtocolError, match=message):
        asyncio.run(client.call_tool("test", {}))


def test_lists_tools_and_requires_active_context() -> None:
    connected = connected_client(tool_result("{}"))
    assert asyncio.run(connected.list_tools()) == frozenset({"get_selected_photos"})

    disconnected = StdioMcpToolClient()
    assert disconnected.server_name is None
    assert disconnected.server_version is None
    assert disconnected.protocol_version is None
    with pytest.raises(RuntimeError, match="inside 'async with'"):
        asyncio.run(disconnected.list_tools())
