from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from stylepilot.adapters.lightroom.errors import (
    LightroomConnectionError,
    LightroomProtocolError,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_FORK_SERVER_ENTRYPOINT = (
    _PROJECT_ROOT / "vendor" / "lightroom-mcp" / "server" / "dist" / "index.js"
)


class McpToolClient(Protocol):
    """Small MCP surface consumed by the Lightroom adapter."""

    @property
    def server_name(self) -> str | None: ...

    @property
    def server_version(self) -> str | None: ...

    @property
    def protocol_version(self) -> str | None: ...

    async def list_tools(self) -> frozenset[str]: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """How StylePilot starts its checked-out Lightroom MCP fork."""

    command: str = "node"
    args: tuple[str, ...] = (str(_FORK_SERVER_ENTRYPOINT),)
    cwd: Path | None = _PROJECT_ROOT
    read_timeout_seconds: float = 180.0


class StdioMcpToolClient:
    """Persistent stdio connection implemented with the official MCP SDK."""

    def __init__(self, config: McpServerConfig | None = None) -> None:
        self._config = config or McpServerConfig()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._server_name: str | None = None
        self._server_version: str | None = None
        self._protocol_version: str | None = None

    @property
    def server_name(self) -> str | None:
        return self._server_name

    @property
    def server_version(self) -> str | None:
        return self._server_version

    @property
    def protocol_version(self) -> str | None:
        return self._protocol_version

    async def __aenter__(self) -> StdioMcpToolClient:
        if self._stack is not None:
            msg = "The MCP client is already connected."
            raise RuntimeError(msg)

        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=self._config.command,
                        args=list(self._config.args),
                        cwd=self._config.cwd,
                    )
                )
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._config.read_timeout_seconds,
                )
            )
            initialized = await session.initialize()
        except BaseException:
            await stack.aclose()
            raise

        self._stack = stack
        self._session = session
        self._server_name = initialized.server_info.name
        self._server_version = initialized.server_info.version
        self._protocol_version = initialized.protocol_version
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def list_tools(self) -> frozenset[str]:
        response = await self._require_session().list_tools()
        return frozenset(tool.name for tool in response.tools)

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = await self._require_session().call_tool(
            name,
            arguments=arguments,
            read_timeout_seconds=self._config.read_timeout_seconds,
        )
        if not isinstance(result, CallToolResult):
            msg = f"MCP tool {name!r} returned an unsupported result type."
            raise LightroomProtocolError(msg)

        text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
        if result.is_error:
            raise LightroomConnectionError(text or f"MCP tool {name!r} failed.")

        if result.structured_content is not None:
            return dict(result.structured_content)
        if not text:
            msg = f"MCP tool {name!r} returned no JSON content."
            raise LightroomProtocolError(msg)

        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as error:
            msg = f"MCP tool {name!r} returned invalid JSON: {error.msg}."
            raise LightroomProtocolError(msg) from error
        if not isinstance(payload, dict):
            msg = f"MCP tool {name!r} must return a JSON object."
            raise LightroomProtocolError(msg)
        return payload

    def _require_session(self) -> ClientSession:
        if self._session is None:
            msg = "The MCP client must be used inside 'async with'."
            raise RuntimeError(msg)
        return self._session
