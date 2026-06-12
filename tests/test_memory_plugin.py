"""Tests for the golden memory plugin: store, MCP server, hooks, manifest loading."""

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import types
from mcp.shared.memory import create_client_server_memory_streams

# ---------------------------------------------------------------------------
# Loader helper: import plugins/memory/*.py by file path since plugins/ is not
# an importable package.
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "memory"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store():
    return _load_module("memory_store", _PLUGIN_ROOT / "store.py")


# ---------------------------------------------------------------------------
# Step 1: Store contract tests
# ---------------------------------------------------------------------------


def test_write_and_get_roundtrip(tmp_path, store):
    rel = store.write(
        tmp_path,
        entry_type="user",
        name="test-entry",
        subject="user",
        description="A test entry",
        body="The body of the entry.",
    )
    assert rel.endswith("test-entry.md")
    text = store.get(tmp_path, "test-entry", "user")
    assert text is not None
    assert "test-entry" in text
    assert "A test entry" in text
    assert "The body of the entry." in text
    # Frontmatter fields are present (on-disk key stays `type:`)
    assert "type: user" in text
    assert "subject: user" in text


def test_write_invalid_type_raises(tmp_path, store):
    with pytest.raises(ValueError, match="not valid"):
        store.write(
            tmp_path,
            entry_type="bogus",
            name="x",
            subject="user",
            description="d",
            body="b",
        )


def test_write_bad_name_raises(tmp_path, store):
    with pytest.raises(ValueError, match="name"):
        store.write(
            tmp_path,
            entry_type="user",
            name="bad name with spaces",
            subject="user",
            description="d",
            body="b",
        )


def test_write_bad_subject_traversal_raises(tmp_path, store):
    """subject is interpolated into the path; a traversal value must be rejected."""
    with pytest.raises(ValueError, match="subject"):
        store.write(
            tmp_path,
            entry_type="user",
            name="evil",
            subject="../evil",
            description="d",
            body="b",
        )
    # Nothing escaped the store root.
    escaped = tmp_path.parent / "evil"
    assert not escaped.exists()


def test_write_collision_raises(tmp_path, store):
    store.write(
        tmp_path,
        entry_type="user",
        name="unique-name",
        subject="user",
        description="first",
        body="body",
    )
    with pytest.raises(ValueError, match="already exists"):
        store.write(
            tmp_path,
            entry_type="user",
            name="unique-name",
            subject="user",
            description="second",
            body="body2",
        )


def test_list_entries_filter_by_type(tmp_path, store):
    store.write(
        tmp_path,
        entry_type="user",
        name="u1",
        subject="user",
        description="User pref",
        body="",
    )
    store.write(
        tmp_path,
        entry_type="project",
        name="p1",
        subject="myproject",
        description="Project note",
        body="",
    )
    users = store.list_entries(tmp_path, entry_type="user")
    assert len(users) == 1
    assert users[0]["name"] == "u1"


def test_list_entries_filter_by_subject(tmp_path, store):
    store.write(
        tmp_path,
        entry_type="user",
        name="u2",
        subject="user",
        description="desc",
        body="",
    )
    store.write(
        tmp_path,
        entry_type="project",
        name="p2",
        subject="projectA",
        description="desc",
        body="",
    )
    results = store.list_entries(tmp_path, subject="projectA")
    assert len(results) == 1
    assert results[0]["name"] == "p2"


def test_brief_user_bullets_and_subjects(tmp_path, store):
    store.write(
        tmp_path,
        entry_type="user",
        name="pref1",
        subject="user",
        description="Prefers dark mode",
        body="",
    )
    store.write(
        tmp_path,
        entry_type="project",
        name="proj1",
        subject="harness",
        description="Project note",
        body="",
    )
    text = store.brief(tmp_path)
    assert "# Memory" in text
    assert "## User-level" in text
    assert "Prefers dark mode" in text
    assert "pref1" in text
    assert "## Subjects" in text
    assert "harness" in text


def test_brief_empty_store(tmp_path, store):
    text = store.brief(tmp_path)
    assert text == "# Memory\n\n_No entries._"


def test_brief_fail_open_on_unreadable_entry(tmp_path, store):
    # Write a valid entry then add a malformed file in the subject dir
    store.write(
        tmp_path,
        entry_type="user",
        name="ok-entry",
        subject="user",
        description="Something",
        body="",
    )
    # Create a corrupted file (missing frontmatter)
    bad = tmp_path / "user" / "9999-99-99-corrupted.md"
    bad.write_text("not valid frontmatter at all", encoding="utf-8")
    # brief() must never raise
    text = store.brief(tmp_path)
    assert "# Memory" in text


def test_rebuild_index_count_and_file(tmp_path, store):
    store.write(
        tmp_path,
        entry_type="user",
        name="r1",
        subject="user",
        description="d1",
        body="",
    )
    store.write(
        tmp_path,
        entry_type="reference",
        name="r2",
        subject="wiki",
        description="d2",
        body="",
    )
    count = store.rebuild_index(tmp_path)
    assert count == 2
    assert (tmp_path / "MEMORY.md").exists()


def test_get_nonexistent_returns_none(tmp_path, store):
    result = store.get(tmp_path, "no-such-entry", "user")
    assert result is None


# ---------------------------------------------------------------------------
# Step 5: Plugin-level tests
# ---------------------------------------------------------------------------


def test_load_plugins_memory_loads_clean():
    from harness.plugins import load_plugins

    plugins_dir = Path(__file__).parent.parent / "plugins"
    loaded = load_plugins([plugins_dir])
    names = [p.name for p in loaded.plugins]
    assert "memory" in names
    mem = next(p for p in loaded.plugins if p.name == "memory")
    assert mem.hooks_module is not None
    assert len(mem.lifecycle_hooks) == 1
    assert mem.lifecycle_hooks[0].point.value == "session_start"


def test_lifecycle_callable_resolves():
    from harness.plugins import load_plugins

    plugins_dir = Path(__file__).parent.parent / "plugins"
    loaded = load_plugins([plugins_dir])
    mem = next(p for p in loaded.plugins if p.name == "memory")
    assert "brief" in mem.lifecycle_callables
    fn = mem.lifecycle_callables["brief"]
    assert callable(fn)


def test_session_brief_returns_inject(tmp_path, monkeypatch):
    from harness.plugins import load_plugins

    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(tmp_path))
    # Write an entry so the brief has content
    store_mod = _load_module("memory_store_for_brief", _PLUGIN_ROOT / "store.py")
    store_mod.write(
        tmp_path,
        entry_type="user",
        name="brief-test",
        subject="user",
        description="A test preference",
        body="",
    )

    plugins_dir = Path(__file__).parent.parent / "plugins"
    loaded = load_plugins([plugins_dir])
    mem = next(p for p in loaded.plugins if p.name == "memory")
    fn = mem.lifecycle_callables["brief"]
    result = fn({})
    assert len(result) == 1
    assert hasattr(result[0], "text")
    assert "# Memory" in result[0].text


def test_session_brief_kill_switch(tmp_path, monkeypatch):
    from harness.plugins import load_plugins

    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_MEMORY_BRIEF", "0")

    plugins_dir = Path(__file__).parent.parent / "plugins"
    loaded = load_plugins([plugins_dir])
    mem = next(p for p in loaded.plugins if p.name == "memory")
    fn = mem.lifecycle_callables["brief"]
    result = fn({})
    assert result == []


# ---------------------------------------------------------------------------
# MCP server in-memory test (mirrors test_mcp_host.py memory_transport pattern)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _memory_transport(fastmcp):
    """In-memory transport for the memory MCP server."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        lowlevel = fastmcp._mcp_server
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: lowlevel.run(
                    server_read,
                    server_write,
                    lowlevel.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                yield (client_read, client_write)
            finally:
                tg.cancel_scope.cancel()


async def test_mcp_server_write_and_get(tmp_path, monkeypatch):
    """In-memory MCP server: write then get."""
    from harness.mcp_config import McpServerSpec
    from harness.mcp_host import ServerConnection

    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(tmp_path))

    server_mod = _load_module("memory_server_test", _PLUGIN_ROOT / "server.py")
    fastmcp = server_mod.mcp

    spec = McpServerSpec(name="memory-test", transport="stdio", command="unused")

    conn = ServerConnection(spec, transport_factory=lambda s: _memory_transport(fastmcp))
    await conn.start()
    try:
        # Write an entry
        result = await conn.call_tool(
            "memory_write",
            {
                "entry_type": "user",
                "name": "mcp-test",
                "subject": "user",
                "description": "MCP roundtrip test",
                "body": "test body",
            },
        )
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        # Should be a path (not an error)
        assert len(texts) == 1
        assert "mcp-test" in texts[0]
        assert not texts[0].startswith("error:")

        # Get the entry back
        result2 = await conn.call_tool(
            "memory_get",
            {"name": "mcp-test", "entry_type": "user"},
        )
        texts2 = [c.text for c in result2.content if isinstance(c, types.TextContent)]
        assert len(texts2) == 1
        assert "mcp-test" in texts2[0]
        assert "MCP roundtrip test" in texts2[0]
    finally:
        await conn.stop()


async def test_mcp_server_subject_traversal_returns_error(tmp_path, monkeypatch):
    """A traversal subject must come back as an errors-as-values string, never raise."""
    from harness.mcp_config import McpServerSpec
    from harness.mcp_host import ServerConnection

    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(tmp_path))

    server_mod = _load_module("memory_server_traversal", _PLUGIN_ROOT / "server.py")
    fastmcp = server_mod.mcp

    spec = McpServerSpec(name="memory-traversal", transport="stdio", command="unused")

    conn = ServerConnection(spec, transport_factory=lambda s: _memory_transport(fastmcp))
    await conn.start()
    try:
        result = await conn.call_tool(
            "memory_write",
            {
                "entry_type": "user",
                "name": "evil",
                "subject": "../evil",
                "description": "should fail",
                "body": "x",
            },
        )
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert len(texts) == 1
        assert texts[0].startswith("error: ")
        assert "subject" in texts[0]
        # Nothing escaped the store root.
        assert not (tmp_path.parent / "evil").exists()
    finally:
        await conn.stop()
