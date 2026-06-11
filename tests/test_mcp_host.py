"""ServerConnection lifecycle over in-memory streams and a real stdio subprocess."""

import asyncio
import sys
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_client_server_memory_streams

from harness.hooks import HookBus, Inject, LifecyclePoint
from harness.mcp_config import McpServerSpec
from harness.mcp_host import (
    McpHost,
    McpServerError,
    McpTool,
    McpToolError,
    ServerConnection,
    _default_transport,
    render_result,
)
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import ToolName, new_session_id
from tests.conftest import FIXTURE_SERVER_PATH, load_fixture_server


@asynccontextmanager
async def memory_transport(fastmcp):
    """Stream-level in-memory transport: runs the server on background streams.
    Mirrors what create_connected_server_and_client_session does internally,
    but yields raw (read, write) so ServerConnection owns the ClientSession."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        lowlevel = fastmcp._mcp_server
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: lowlevel.run(
                    server_read, server_write,
                    lowlevel.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                yield (client_read, client_write)
            finally:
                tg.cancel_scope.cancel()


def memory_spec(**overrides) -> McpServerSpec:
    defaults = dict(name="fixture", transport="stdio", command="unused")
    defaults.update(overrides)
    return McpServerSpec(**defaults)


def memory_factory():
    fastmcp = load_fixture_server()
    return lambda spec: memory_transport(fastmcp)


@asynccontextmanager
async def hanging_transport(spec):
    await asyncio.sleep(3600)
    yield (None, None)  # pragma: no cover — never reached


async def test_connection_start_captures_instructions_and_tools():
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    await conn.start()
    try:
        assert conn.instructions == "Fixture server: use `add` for arithmetic."
        assert conn.server_info is not None
        names = {t.name for t in conn.tools}
        assert {"add", "fail", "big", "env_probe", "die"} <= names
    finally:
        await conn.stop()


async def test_connection_call_tool_roundtrip():
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    await conn.start()
    try:
        result = await conn.call_tool("add", {"a": 20, "b": 22})
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert texts == ["42"]
    finally:
        await conn.stop()


async def test_connection_stop_is_clean_and_repeatable():
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    await conn.start()
    await conn.stop()
    await conn.stop()  # idempotent
    assert conn.session is None


async def test_double_start_is_loud():
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    await conn.start()
    try:
        with pytest.raises(McpServerError) as exc:
            await conn.start()
        assert "already running" in str(exc.value)
    finally:
        await conn.stop()


async def test_connection_start_failure_raises_mcp_server_error():
    spec = McpServerSpec(
        name="broken", transport="stdio",
        command=sys.executable, args=("-c", "import sys; sys.exit(3)"),
    )
    conn = ServerConnection(spec)
    with pytest.raises(McpServerError) as exc:
        await conn.start()
    assert "broken" in str(exc.value)


async def test_stdio_subprocess_roundtrip_and_env_references(monkeypatch):
    monkeypatch.setenv("FIXTURE_SECRET", "s3cret")
    spec = McpServerSpec(
        name="fixture", transport="stdio",
        command=sys.executable, args=(str(FIXTURE_SERVER_PATH),),
        env={"PROBE_TARGET": "FIXTURE_SECRET"},
    )
    conn = ServerConnection(spec)
    await conn.start()
    try:
        result = await conn.call_tool("env_probe", {"name": "PROBE_TARGET"})
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert texts == ["s3cret"]
        # SDK safe-list: parent env vars NOT named in spec.env are absent in the child
        monkeypatch.setenv("NOT_FORWARDED", "leak")
        result = await conn.call_tool("env_probe", {"name": "NOT_FORWARDED"})
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert texts == ["<unset>"]
    finally:
        await conn.stop()


async def test_cancelled_start_does_not_orphan_run_task():
    conn = ServerConnection(memory_spec(), transport_factory=hanging_transport)
    starter = asyncio.create_task(conn.start())
    await asyncio.sleep(0.05)
    starter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starter
    assert conn._task is None
    assert not conn.is_alive


async def test_session_lost_after_restart_is_structured(monkeypatch):
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    await conn.start()
    try:
        async def noop(gen, *, reason):
            return None

        monkeypatch.setattr(conn, "_restart_if_allowed", noop)
        conn.session = None
        with pytest.raises(McpServerError) as exc:
            await conn.call_tool("add", {"a": 1, "b": 1})
        assert "unavailable" in str(exc.value)
    finally:
        await conn.stop()


async def test_is_alive_reflects_lifecycle():
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    assert not conn.is_alive
    await conn.start()
    assert conn.is_alive
    await conn.stop()
    assert not conn.is_alive


async def test_mcp_tool_adapter_namespaces_and_calls():
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    await conn.start()
    try:
        tool = next(t for t in conn.tools if t.name == "add")
        adapter = McpTool(conn, tool)
        assert str(adapter.spec.name) == "mcp__fixture__add"
        assert adapter.spec.description == "Add two integers."
        assert adapter.spec.parameters.get("type") == "object"
        assert await adapter({"a": 1, "b": 2}) == "3"
    finally:
        await conn.stop()


async def test_mcp_tool_iserror_raises_for_dispatcher():
    conn = ServerConnection(memory_spec(), transport_factory=memory_factory())
    await conn.start()
    try:
        tool = next(t for t in conn.tools if t.name == "fail")
        adapter = McpTool(conn, tool)
        with pytest.raises(McpToolError) as exc:
            await adapter({"message": "boom"})
        assert "boom" in str(exc.value)
    finally:
        await conn.stop()


def test_render_result_text_blocks_join():
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="one"),
            types.TextContent(type="text", text="two"),
        ]
    )
    assert render_result(result) == "one\ntwo"


def test_render_result_non_text_blocks_are_summarized():
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="caption"),
            types.ImageContent(type="image", data="aGk=", mimeType="image/png"),
        ]
    )
    assert render_result(result) == "caption\n[image content omitted]"


def test_render_result_structured_fallback_when_no_text():
    result = types.CallToolResult(content=[], structuredContent={"answer": 42})
    assert render_result(result) == "{\"answer\": 42}"


def test_render_result_empty_everything_is_empty_string():
    assert render_result(types.CallToolResult(content=[])) == ""


def make_session(tmp_path) -> Session:
    return Session(tmp_path, new_session_id())


def make_host(tmp_path, specs=None, **kwargs):
    registry = ToolRegistry()
    hooks = HookBus()
    session = make_session(tmp_path)
    host = McpHost(
        specs if specs is not None else [memory_spec()],
        registry=registry, hooks=hooks, session=session,
        transport_factory=memory_factory(), **kwargs,
    )
    return host, registry, hooks, session


async def test_host_start_registers_namespaced_tools(tmp_path):
    host, registry, hooks, session = make_host(tmp_path)
    warnings = await host.start()
    try:
        assert warnings == []
        names = {str(s.name) for s in registry.specs()}
        assert "mcp__fixture__add" in names
        assert "mcp__fixture__die" in names
        result = await registry.get(ToolName("mcp__fixture__add"))({"a": 2, "b": 2})
        assert result == "4"
    finally:
        await host.stop()
        session.close()


async def test_host_instructions_hook_injects_at_session_start(tmp_path):
    host, registry, hooks, session = make_host(tmp_path)
    await host.start()
    try:
        contributions, hook_warnings = await hooks.run_lifecycle(
            LifecyclePoint.SESSION_START, {"session_id": session.id}
        )
        assert hook_warnings == []
        injects = [c for c in contributions if isinstance(c, Inject)]
        assert len(injects) == 1
        assert "## MCP server: fixture" in injects[0].text
        assert "Fixture server: use `add` for arithmetic." in injects[0].text
    finally:
        await host.stop()
        session.close()


async def test_host_lifecycle_events_buffer_until_flush(tmp_path):
    host, registry, hooks, session = make_host(tmp_path)
    queue = session.bus.subscribe()
    await host.start()
    try:
        assert queue.qsize() == 0  # nothing logged before flush
        session.start()
        host.flush_events()
        kinds = []
        while queue.qsize():
            envelope = queue.get_nowait()
            kinds.append(getattr(envelope.event, "name", type(envelope.event).__name__))
        assert "server_started" in kinds
    finally:
        await host.stop()
        session.close()


async def test_host_collision_skips_and_warns(tmp_path):
    class Squatter:
        spec = ToolSpec(name=ToolName("mcp__fixture__add"), description="", parameters={})

        async def __call__(self, args):
            return "squatted"

    host, registry, hooks, session = make_host(tmp_path)
    registry.register(Squatter())
    warnings = await host.start()
    try:
        assert any("mcp__fixture__add" in w and "collide" in w for w in warnings)
        # the squatter survives; the MCP tool was skipped, not overwritten
        assert await registry.get(ToolName("mcp__fixture__add"))({}) == "squatted"
    finally:
        await host.stop()
        session.close()


async def test_host_partial_failure_keeps_other_servers(tmp_path):
    fastmcp = load_fixture_server()
    bad = McpServerSpec(
        name="broken", transport="stdio",
        command=sys.executable, args=("-c", "import sys; sys.exit(3)"),
    )

    def factory(spec):
        if spec.name == "broken":
            return _default_transport(spec)
        return memory_transport(fastmcp)

    registry = ToolRegistry()
    hooks = HookBus()
    session = make_session(tmp_path)
    host = McpHost(
        [memory_spec(), bad], registry=registry, hooks=hooks, session=session,
        transport_factory=factory,
    )
    warnings = await host.start()
    try:
        assert any("broken" in w for w in warnings)
        assert "mcp__fixture__add" in {str(s.name) for s in registry.specs()}
        assert "broken" not in host.connections
    finally:
        await host.stop()
        session.close()


async def test_host_double_start_is_loud(tmp_path):
    host, registry, hooks, session = make_host(tmp_path)
    await host.start()
    try:
        with pytest.raises(RuntimeError) as exc:
            await host.start()
        assert "already" in str(exc.value)
    finally:
        await host.stop()
        session.close()


async def test_host_stop_after_session_close_does_not_raise(tmp_path):
    host, registry, hooks, session = make_host(tmp_path)
    await host.start()
    session.start()
    host.flush_events()
    session.close()
    await host.stop()  # must not raise: server_stopped events drop silently
    assert host.connections == {}


async def test_restart_on_failure_respawns_stdio_server():
    events: list[tuple[str, dict]] = []
    spec = McpServerSpec(
        name="fixture", transport="stdio",
        command=sys.executable, args=(str(FIXTURE_SERVER_PATH),),
        restart="on_failure", tool_timeout_s=15.0,
    )
    conn = ServerConnection(spec, on_event=lambda name, data: events.append((name, data)))
    await conn.start()
    try:
        with pytest.raises(McpError):
            await conn.call_tool("die", {})  # kills the child mid-call
        result = await conn.call_tool("add", {"a": 1, "b": 1})  # triggers respawn
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert texts == ["2"]
        assert any(name == "server_restarted" for name, _ in events)
    finally:
        await conn.stop()


async def test_restart_never_policy_stays_down():
    spec = McpServerSpec(
        name="fixture", transport="stdio",
        command=sys.executable, args=(str(FIXTURE_SERVER_PATH),),
        restart="never", tool_timeout_s=15.0,
    )
    conn = ServerConnection(spec)
    await conn.start()
    try:
        with pytest.raises(McpError):
            await conn.call_tool("die", {})
        with pytest.raises(McpServerError) as exc:
            await conn.call_tool("add", {"a": 1, "b": 1})
        assert "unavailable" in str(exc.value)
    finally:
        await conn.stop()


async def test_restart_budget_resets_on_success():
    events: list[tuple[str, dict]] = []
    spec = McpServerSpec(
        name="fixture", transport="stdio",
        command=sys.executable, args=(str(FIXTURE_SERVER_PATH),),
        restart="on_failure", tool_timeout_s=15.0,
    )
    conn = ServerConnection(spec, on_event=lambda name, data: events.append((name, data)))
    await conn.start()
    try:
        for expected_attempt in (1, 1, 1):  # each episode resets: attempt is always 1
            with pytest.raises(McpError):
                await conn.call_tool("die", {})
            result = await conn.call_tool("add", {"a": 1, "b": 1})
            texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
            assert texts == ["2"]
            assert events[-1][0] == "server_restarted"
            assert events[-1][1]["attempt"] == expected_attempt
        assert conn._restarts == 0
    finally:
        await conn.stop()
