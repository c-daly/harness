"""ServerConnection lifecycle over in-memory streams and a real stdio subprocess."""

import asyncio
import sys
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import types
from mcp.shared.memory import create_client_server_memory_streams

from harness.mcp_config import McpServerSpec
from harness.mcp_host import McpServerError, McpTool, McpToolError, ServerConnection, render_result
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
