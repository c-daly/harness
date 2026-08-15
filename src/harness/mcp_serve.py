"""Serve the harness tool surface over MCP (streamable HTTP, in-process).

The inverse of mcp_host: external MCP clients (a headless `claude -p` turn)
see exactly the ToolSpecs they were given, and every tools/call routes through
the injected dispatch callable — the dispatcher seam — so permissions, hooks,
and the event log apply identically to native and MCP-arriving tool calls.
HTTP (not stdio) because stdio MCP servers are spawned as the CLIENT's child
process, which would not share this process's registry and dispatcher.
"""

import asyncio
import contextlib
from typing import Any, Awaitable, Callable, Sequence

import mcp.types as mcp_types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from harness.dispatcher import ToolOutcome
from harness.hooks import ProposedToolCall
from harness.tools import ToolSpec
from harness.types import ToolName, new_call_id

Dispatch = Callable[[ProposedToolCall], Awaitable[ToolOutcome]]


class McpToolServer:
    def __init__(self, *, specs: Sequence[ToolSpec], dispatch: Dispatch) -> None:
        self._specs = tuple(specs)
        self._by_name = {str(s.name): s for s in self._specs}
        self._dispatch = dispatch
        self._uvicorn: uvicorn.Server | None = None
        self._serve_task: asyncio.Task | None = None
        self._port: int | None = None

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("McpToolServer not started")
        return f"http://127.0.0.1:{self._port}/mcp"

    def _build_app(self) -> Starlette:
        server: Server = Server("harness")

        @server.list_tools()
        async def _list_tools() -> list[mcp_types.Tool]:
            return [
                mcp_types.Tool(
                    name=str(s.name), description=s.description, inputSchema=s.parameters
                )
                for s in self._specs
            ]

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict[str, Any] | None):
            if name not in self._by_name:
                raise ValueError(f"unknown tool {name!r}")
            outcome = await self._dispatch(
                ProposedToolCall(
                    call_id=new_call_id(), tool=ToolName(name), args=arguments or {}
                )
            )
            if outcome.is_error:
                raise ValueError(outcome.text)
            return [mcp_types.TextContent(type="text", text=outcome.text)]

        manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        return Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)

    async def start(self) -> None:
        config = uvicorn.Config(
            self._build_app(), host="127.0.0.1", port=0, log_level="error", lifespan="on"
        )
        self._uvicorn = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(self._uvicorn.serve())
        while not self._uvicorn.started:
            if self._serve_task.done():
                self._serve_task.result()  # raise the startup failure
            await asyncio.sleep(0.01)
        self._port = self._uvicorn.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
        self._uvicorn = None
        self._serve_task = None
        self._port = None
