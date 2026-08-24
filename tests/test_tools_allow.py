"""Per-server tool filtering (tools_allow globs) and checklist defaults
(default_enabled) in mcp.toml."""

from contextlib import asynccontextmanager

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from harness.hooks import HookBus
from harness.mcp_config import McpConfigError, McpServerSpec, emit_mcp_toml, load_mcp_file
from harness.mcp_host import McpHost
from harness.session import Session
from harness.tools import ToolRegistry
from harness.types import new_session_id


def _load(tmp_path, body: str):
    p = tmp_path / "mcp.toml"
    p.write_text(body)
    return load_mcp_file(p, source="user")


def test_tools_allow_parses_and_defaults_empty(tmp_path):
    specs = _load(
        tmp_path,
        """[servers.router]
command = "/bin/mcp-router"
tools_allow = ["workflow__*", "experiment__*"]

[servers.plain]
command = "/bin/other"
""",
    )
    by_name = {s.name: s for s in specs}
    assert by_name["router"].tools_allow == ("workflow__*", "experiment__*")
    assert by_name["plain"].tools_allow == ()


def test_default_enabled_parses_and_defaults_true(tmp_path):
    specs = _load(
        tmp_path,
        """[servers.a]
command = "/bin/a"
default_enabled = false

[servers.b]
command = "/bin/b"
""",
    )
    by_name = {s.name: s for s in specs}
    assert by_name["a"].default_enabled is False
    assert by_name["b"].default_enabled is True


def test_tools_allow_rejects_non_string_entries(tmp_path):
    with pytest.raises(McpConfigError, match="tools_allow"):
        _load(
            tmp_path,
            """[servers.x]
command = "/bin/x"
tools_allow = [1]
""",
        )


def test_default_enabled_rejects_non_bool(tmp_path):
    with pytest.raises(McpConfigError, match="default_enabled"):
        _load(
            tmp_path,
            """[servers.x]
command = "/bin/x"
default_enabled = "yes"
""",
        )


# --- host-filter test: fnmatch tools_allow globs filter tool exposure ---

router_fixture = FastMCP("router", instructions="Router fixture: scripted multi-family tools.")


@router_fixture.tool()
def workflow__start() -> str:
    """Start a workflow."""
    return "started"


@router_fixture.tool()
def native__bash(command: str) -> str:
    """Run a shell command."""
    return command


@router_fixture.tool()
def experiment__log(message: str) -> str:
    """Log an experiment observation."""
    return message


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


def router_factory():
    return lambda spec: memory_transport(router_fixture)


def make_session(tmp_path) -> Session:
    return Session(tmp_path, new_session_id())


async def test_host_filters_tools_by_tools_allow_globs(tmp_path):
    spec = McpServerSpec(
        name="router",
        transport="stdio",
        command="unused",
        tools_allow=("workflow__*", "experiment__*"),
    )
    registry = ToolRegistry()
    hooks = HookBus()
    session = make_session(tmp_path)
    host = McpHost(
        [spec], registry=registry, hooks=hooks, session=session,
        transport_factory=router_factory(),
    )
    await host.start()
    try:
        names = {str(s.name) for s in registry.specs()}
        assert "mcp__router__workflow__start" in names
        assert "mcp__router__experiment__log" in names
        assert "mcp__router__native__bash" not in names
    finally:
        await host.stop()
        session.close()


# --- emit_mcp_toml round-trip: tools_allow and default_enabled survive a
# read-all -> mutate-one -> rewrite-all cycle (cli.py mcp add/remove/import) ---


def test_emit_mcp_toml_round_trips_tools_allow_and_default_enabled(tmp_path):
    original = McpServerSpec(
        name="router",
        transport="stdio",
        command="/bin/mcp-router",
        tools_allow=("workflow__*", "experiment__*"),
        default_enabled=False,
    )
    text = emit_mcp_toml((original,))
    p = tmp_path / "mcp.toml"
    p.write_text(text)
    reparsed = load_mcp_file(p, source="user")
    assert len(reparsed) == 1
    assert reparsed[0].tools_allow == ("workflow__*", "experiment__*")
    assert reparsed[0].default_enabled is False


def test_emit_mcp_toml_omits_tools_allow_and_default_enabled_when_default(tmp_path):
    plain = McpServerSpec(name="plain", transport="stdio", command="/bin/plain")
    text = emit_mcp_toml((plain,))
    assert "tools_allow" not in text
    assert "default_enabled" not in text
    p = tmp_path / "mcp.toml"
    p.write_text(text)
    reparsed = load_mcp_file(p, source="user")
    assert reparsed[0].tools_allow == ()
    assert reparsed[0].default_enabled is True
