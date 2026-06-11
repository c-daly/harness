"""MCP host: per-server connections, tool adapters, instruction injection.

The anyio contract: stdio_client/ClientSession create cancel scopes that must
enter and exit in the SAME task. Each ServerConnection therefore runs its
context managers inside one dedicated _run task parked on a stop event;
call_tool from other tasks is safe (the session multiplexes by request id).
"""

import asyncio
import contextlib
import json
from datetime import timedelta
from typing import Any, Callable

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from harness.events import CustomEvent
from harness.hooks import HookBus, Inject, LifecyclePoint
from harness.mcp_config import McpServerSpec, resolve_env
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import ToolName

_MAX_RESTARTS = 3  # consecutive failed respawns before a connection is declared dead
_STOP_TIMEOUT_S = 10.0
_START_TIMEOUT_S = 30.0
_MAX_REASON_LEN = 400


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
        if self._task is not None:
            raise McpServerError(f"mcp server {self.spec.name!r} is already running")
        self._ready = asyncio.Event()
        self._stop_signal = asyncio.Event()
        self._failure = None
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.spec.name}")
        try:
            await self._ready.wait()
        except asyncio.CancelledError:
            task, self._task = self._task, None
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        if self._failure is not None:
            failure, self._task = self._failure, None
            if not isinstance(failure, Exception):
                raise failure  # propagate cancellation/interrupt as itself
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
            if not isinstance(exc, Exception):
                raise  # CancelledError/KeyboardInterrupt/SystemExit must propagate
        finally:
            self.session = None
            self._ready.set()

    async def call_tool(self, tool: str, args: dict[str, Any]) -> types.CallToolResult:
        gen = self._gen
        if self.session is None:
            await self._restart_if_allowed(gen, reason="connection lost")
        session = self._require_session()
        try:
            return await session.call_tool(tool, args)
        except McpError:
            raise  # protocol-level error (incl. per-call timeout): the server is alive
        except Exception as exc:
            # A dead stdio child surfaces here as anyio.ClosedResourceError on the NEXT
            # call after the death: the die call itself raises McpError (caught above),
            # leaving a stale but non-None self.session. The subsequent call on that
            # stale session raises ClosedResourceError (not McpError), taking this path.
            reason = f"transport failure: {exc!r}"[:_MAX_REASON_LEN]
            await self._restart_if_allowed(gen, reason=reason)
            return await self._require_session().call_tool(tool, args)

    def _require_session(self) -> ClientSession:
        session = self.session
        if session is None:
            raise McpServerError(
                f"mcp server {self.spec.name!r} unavailable (session lost after restart)"
            )
        return session

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
            self._restarts = 0  # success closes the failure episode: the budget is
            # 3 CONSECUTIVE failed respawns, not 3 per lifetime (a flaky server that
            # recovers each time must not go permanently dead hours later)

    async def stop(self) -> None:
        await self._teardown()

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done() and self.session is not None

    async def _teardown(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop_signal.set()
        try:
            await asyncio.wait_for(task, timeout=_STOP_TIMEOUT_S)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise


def render_result(result: types.CallToolResult) -> str:
    """Tool protocol returns str: join text blocks, summarize the rest, fall
    back to structuredContent JSON when there is no text at all."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content omitted]")
    if not parts and result.structuredContent is not None:
        parts.append(json.dumps(result.structuredContent))
    return "\n".join(parts)


class McpTool:
    """Adapter: one MCP server tool as a harness Tool (spec + async __call__)."""

    def __init__(self, conn: ServerConnection, tool: types.Tool) -> None:
        self.spec = ToolSpec(
            name=ToolName(f"mcp__{conn.spec.name}__{tool.name}"),
            description=tool.description or "",
            parameters=tool.inputSchema,
        )
        self._conn = conn
        self._remote_name = tool.name

    async def __call__(self, args: dict[str, Any]) -> str:
        result = await self._conn.call_tool(self._remote_name, args)
        text = render_result(result)
        if result.isError:
            raise McpToolError(text or "tool returned an error")
        return text


class McpHost:
    """Owns all connections. start() is async; the caller starts the host
    inside the asyncio context, before loop.start().
    Lifecycle events buffer until flush_events() so nothing precedes
    SessionStarted in a fresh log; runtime events (restarts) append directly
    once flushed.

    Single-use: one McpHost per HookBus/registry per process (start() guards
    re-entry; a second host on the same bus would duplicate the instructions
    hook). A dead connection keeps its last instructions: subagent sessions
    started after a server died still inject them (harmless; tools fail fast)."""

    def __init__(
        self,
        specs,
        *,
        registry: ToolRegistry,
        hooks: HookBus,
        session: Session,
        transport_factory: Callable[[McpServerSpec], Any] | None = None,
    ) -> None:
        self.connections: dict[str, ServerConnection] = {}
        self._specs = tuple(specs)
        self._registry = registry
        self._hooks = hooks
        self._session = session
        self._transport_factory = transport_factory
        self._pending: list[CustomEvent] | None = []  # None once flushed

    def _emit(self, name: str, data: dict) -> None:
        if self._session.closed:
            return  # teardown race: dropping informational events beats masking real errors
        event = CustomEvent(namespace="mcp", name=name, data=data)
        if self._pending is not None:
            self._pending.append(event)
        else:
            self._session.append(event)

    def flush_events(self) -> None:
        pending, self._pending = self._pending or [], None
        if self._session.closed:
            return
        for event in pending:
            self._session.append(event)

    async def start(self) -> list[str]:
        """Connect everything; per-server failures become warnings, never crashes."""
        if self.connections:
            raise RuntimeError("McpHost.start() already called; call stop() first")
        warnings: list[str] = []
        conns = [
            ServerConnection(
                spec, transport_factory=self._transport_factory, on_event=self._emit
            )
            for spec in self._specs
        ]
        results = await asyncio.gather(*(c.start() for c in conns), return_exceptions=True)
        taken = {str(s.name) for s in self._registry.specs()}
        for conn, result in zip(conns, results):
            if isinstance(result, BaseException):
                self._emit(
                    "server_failed",
                    {"server": conn.spec.name, "error": str(result)[:500]},
                )
                warnings.append(f"mcp server {conn.spec.name!r} failed to start: {result}")
                continue
            self.connections[conn.spec.name] = conn
            registered = 0
            for tool in conn.tools:
                adapter = McpTool(conn, tool)
                name = str(adapter.spec.name)
                if name in taken:
                    self._emit("tool_collision", {"tool": name, "server": conn.spec.name})
                    warnings.append(
                        f"mcp tool {name} would collide with an existing tool; skipped"
                    )
                    continue
                taken.add(name)
                self._registry.register(adapter)
                registered += 1
            self._emit(
                "server_started",
                {
                    "server": conn.spec.name,
                    "tools": registered,
                    "source": conn.spec.source,
                    "transport": conn.spec.transport,
                },
            )
        if any(c.instructions for c in self.connections.values()):
            self._hooks.register_lifecycle(
                "mcp-instructions", LifecyclePoint.SESSION_START, self._instructions_hook
            )
        return warnings

    def _instructions_hook(self, ctx) -> list[Inject]:
        return [
            Inject(text=f"## MCP server: {name}\n\n{conn.instructions}")
            for name, conn in sorted(self.connections.items())
            if conn.instructions
        ]

    async def stop(self) -> None:
        await asyncio.gather(
            *(c.stop() for c in self.connections.values()), return_exceptions=True
        )
        for name in self.connections:
            self._emit("server_stopped", {"server": name})
        self.connections.clear()
