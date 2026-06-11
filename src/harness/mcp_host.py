"""MCP host: per-server connections, tool adapters, instruction injection.

The anyio contract: stdio_client/ClientSession create cancel scopes that must
enter and exit in the SAME task. Each ServerConnection therefore runs its
context managers inside one dedicated _run task parked on a stop event;
call_tool from other tasks is safe (the session multiplexes by request id).
"""

import asyncio
import contextlib
from datetime import timedelta
from typing import Any, Callable

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from harness.mcp_config import McpServerSpec, resolve_env

_MAX_RESTARTS = 3
_STOP_TIMEOUT_S = 10.0
_START_TIMEOUT_S = 30.0


class McpServerError(Exception):
    """Connection/lifecycle failure for one server."""


class McpToolError(Exception):
    """A tool result with isError=True; the dispatcher renders it as a tool error."""


def _default_transport(spec: McpServerSpec):
    if spec.transport == "stdio":
        params = StdioServerParameters(
            command=spec.command,
            args=list(spec.args),
            env=resolve_env(spec.env) or None,
            cwd=spec.cwd,
        )
        return stdio_client(params)
    headers = resolve_env(spec.headers)
    http_client = None
    if headers:
        # mirror the SDK default timeouts; a plain AsyncClient would cut long reads
        http_client = httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True
        )
    return streamable_http_client(spec.url, http_client=http_client)


async def _list_all_tools(session: ClientSession) -> list[types.Tool]:
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if cursor is None:
            return tools


class ServerConnection:
    """One MCP server. All context enters/exits live inside _run (one task)."""

    def __init__(
        self,
        spec: McpServerSpec,
        *,
        transport_factory: Callable[[McpServerSpec], Any] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.spec = spec
        self.session: ClientSession | None = None
        self.instructions: str | None = None
        self.server_info: types.Implementation | None = None
        self.tools: list[types.Tool] = []
        self._transport_factory = transport_factory or _default_transport
        self._on_event = on_event or (lambda name, data: None)
        self._ready: asyncio.Event = asyncio.Event()
        self._stop_signal: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._failure: BaseException | None = None
        self._gen = 0  # bumps on each successful start; staleness check for restarts
        self._restarts = 0
        self._restart_lock = asyncio.Lock()

    async def start(self) -> None:
        self._ready = asyncio.Event()
        self._stop_signal = asyncio.Event()
        self._failure = None
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.spec.name}")
        await self._ready.wait()
        if self._failure is not None:
            failure, self._task = self._failure, None
            raise McpServerError(
                f"mcp server {self.spec.name!r} failed to start: {failure}"
            ) from failure
        self._gen += 1

    async def _run(self) -> None:
        # asyncio.timeout is used rather than anyio.fail_after because _run is an
        # asyncio.Task so the timeout scope enters and exits in the same asyncio task.
        # anyio.fail_after would also work; asyncio.timeout avoids needing anyio here.
        try:
            async with self._transport_factory(self.spec) as opened:
                read, write = opened[0], opened[1]  # http transports yield a 3-tuple
                timeout = timedelta(seconds=self.spec.tool_timeout_s)
                async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                    async with asyncio.timeout(_START_TIMEOUT_S):
                        init = await session.initialize()
                        self.instructions = init.instructions
                        self.server_info = init.serverInfo
                        self.tools = await _list_all_tools(session)
                    self.session = session
                    self._ready.set()
                    await self._stop_signal.wait()
        except BaseException as exc:
            self._failure = exc
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.session = None
            self._ready.set()

    async def call_tool(self, tool: str, args: dict[str, Any]) -> types.CallToolResult:
        gen = self._gen
        if self.session is None:
            await self._restart_if_allowed(gen, reason="connection lost")
        try:
            return await self.session.call_tool(tool, args)  # type: ignore[union-attr]
        except McpError:
            raise  # protocol-level error (incl. per-call timeout): the server is alive
        except Exception as exc:
            await self._restart_if_allowed(gen, reason=f"transport failure: {exc!r}")
            return await self.session.call_tool(tool, args)  # type: ignore[union-attr]

    async def _restart_if_allowed(self, gen: int, *, reason: str) -> None:
        async with self._restart_lock:
            if self._gen != gen:
                return  # another caller already restarted this connection
            if self.spec.restart != "on_failure" or self._restarts >= _MAX_RESTARTS:
                raise McpServerError(f"mcp server {self.spec.name!r} unavailable ({reason})")
            self._restarts += 1
            await self._teardown()
            await self.start()
            self._on_event(
                "server_restarted",
                {"server": self.spec.name, "attempt": self._restarts, "reason": reason},
            )

    async def stop(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop_signal.set()
        try:
            await asyncio.wait_for(task, timeout=_STOP_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
