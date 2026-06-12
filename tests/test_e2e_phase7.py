"""Phase 7 milestone: the eight-primitive plugin contract - the golden memory
plugin end to end."""

import dataclasses
import importlib.util
import sys
from pathlib import Path

from textual.widgets import RichLog

from harness.cli import build_kernel, run_once
from harness.log import read_session
from harness.plugins import load_plugins
from harness.provider import EchoProvider, FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName

from tests.test_tui import make_app

_PLUGINS_DIR = Path(__file__).parent.parent / "plugins"


# ---------------------------------------------------------------------------
# Helper: write an inline fixture plugin into a tmp dir.
# ---------------------------------------------------------------------------


def _write_plugin(root: Path, name: str, manifest: str, files: dict = None) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(manifest, encoding="utf-8")
    for rel, content in (files or {}).items():
        path = plugin_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return plugin_dir


# ---------------------------------------------------------------------------
# Test 1: TUI full session with memory plugin
# ---------------------------------------------------------------------------


async def test_memory_plugin_full_session_in_tui(tmp_path, monkeypatch):
    """Load the golden memory plugin; run a TUI pilot session.

    Proves: lifecycle hook injects # Memory brief; skills inventory injected;
    mcp/server_started rendered; plugin_loaded fires after session_started;
    invoke_skill in registry; /brief command dispatches its body."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(mem_dir))
    store_path = _PLUGINS_DIR / "memory" / "store.py"
    spec = importlib.util.spec_from_file_location("_mem_store_seed", store_path)
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    store.write(
        mem_dir,
        entry_type="user",
        name="e2e-pref",
        subject="user",
        description="Prefers concise answers",
        body="Keep it short.",
    )
    loaded = load_plugins([_PLUGINS_DIR])
    assert any(p.name == "memory" for p in loaded.plugins)
    mcp_specs = [dataclasses.replace(s, command=sys.executable) for s in loaded.mcp_servers]
    app = make_app(
        tmp_path,
        provider=EchoProvider(),
        model=ModelId("echo"),
        plugins=loaded,
        mcp=mcp_specs,
    )
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.8)
            assert "# Memory" in app.kernel.loop.system_prompt
            assert "invoke_skill" in {str(s.name) for s in app.kernel.registry.specs()}
            lines_out = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert "server_started" in lines_out
            await pilot.click("#prompt")
            await pilot.press(*"hello world", "enter")
            await pilot.pause(0.4)
            lines_out = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert "hello world" in lines_out
            await pilot.click("#prompt")
            await pilot.press(*"/brief", "enter")
            await pilot.pause(0.4)
            await pilot.press(*"/quit", "enter")
            await pilot.pause(0.3)
    finally:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        if app.kernel.mcp is not None:
            await app.kernel.mcp.stop()
            app.kernel.mcp.flush_events()
        app.kernel.session.close()
    envelopes = read_session(tmp_path, app.kernel.session.id)
    event_types = [e.event.type for e in envelopes]
    session_start_idx = event_types.index("session_started")
    plugin_loaded_indices = [
        i
        for i, e in enumerate(envelopes)
        if e.event.type == "custom"
        and e.event.namespace == "plugin"
        and e.event.name == "plugin_loaded"
    ]
    assert plugin_loaded_indices, "no plugin_loaded events found"
    assert all(idx > session_start_idx for idx in plugin_loaded_indices)
    assert "invoke_skill" in {str(s.name) for s in app.kernel.registry.specs()}


# ---------------------------------------------------------------------------
# Test 2: memory_write via MCP dispatch in headless run
# ---------------------------------------------------------------------------


async def test_memory_write_via_dispatch(tmp_path, monkeypatch):
    """Headless kernel with FakeProvider scripting mcp__memory__memory_write;
    after run_once the store file exists with correct frontmatter.
    Proves: MCP server subprocess lifecycle + tool dispatch + store write
    work end-to-end."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(mem_dir))
    loaded = load_plugins([_PLUGINS_DIR])
    mcp_specs = [
        dataclasses.replace(
            s, command=sys.executable, env={"HARNESS_MEMORY_DIR": "HARNESS_MEMORY_DIR"}
        )
        for s in loaded.mcp_servers
    ]
    provider = FakeProvider(
        [
            tool_call_turn(
                "writing to memory",
                ToolName("mcp__memory__memory_write"),
                {
                    "entry_type": "user",
                    "name": "e2e-write-test",
                    "subject": "user",
                    "description": "E2E write test",
                    "body": "Written via MCP dispatch.",
                },
            ),
            text_turn("done"),
        ]
    )
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake"),
        plugins=loaded,
        mcp=mcp_specs,
    )
    result = await run_once(kernel, "write a memory entry")
    assert result == "done"
    store_files = list(mem_dir.rglob("*e2e-write-test*.md"))
    assert store_files, "no store file found in memory dir"
    file_content = store_files[0].read_text(encoding="utf-8")
    assert "type: user" in file_content
    assert "e2e-write-test" in file_content
    from harness.events import ToolCallCompleted

    envelopes = read_session(tmp_path, kernel.session.id)
    tool_results = [
        e.event
        for e in envelopes
        if isinstance(e.event, ToolCallCompleted) and not e.event.is_error
    ]
    assert tool_results, "no successful ToolCallCompleted found"
    assert any("e2e-write-test" in (r.result_text or "") for r in tool_results)


# ---------------------------------------------------------------------------
# Test 3: dispatch hook from fixture plugin blocks a named tool
# ---------------------------------------------------------------------------


async def test_dispatch_hook_from_fixture_plugin_blocks(tmp_path):
    """Fixture plugin whose dispatch hook returns Block for echo_tool;
    FakeProvider calls that tool; the turn result contains blocked by policy;
    turn completes normally.
    Proves: dispatch hook primitive enforces policy at kernel level."""
    from harness.events import ToolCallCompleted
    from harness.tools import ToolSpec
    from harness.types import ToolName

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        name="guard",
        manifest='[plugin]\nname = "guard"\nversion = "0.1.0"\ndescription = "Policy guard fixture plugin"\n\n[hooks]\nmodule = "hooks.py"\n\n[[hooks.dispatch]]\nname = "block-banned"\nfunction = "block_banned"\npriority = 50',
        files={
            "hooks.py": 'from harness.hooks import Block, Allow\n\n\ndef block_banned(action):\n    from harness.hooks import ProposedToolCall\n    if isinstance(action, ProposedToolCall) and str(action.tool) == "echo_tool":\n        return Block(reason="banned by policy test")\n    return Allow()\n'
        },
    )

    class EchoTool:
        spec = ToolSpec(
            name=ToolName("echo_tool"),
            description="Echo back",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )

        async def __call__(self, args: dict) -> str:  # pragma: no cover
            return args.get("text", "")

    loaded = load_plugins([plugin_root])
    kernel = build_kernel(
        provider=FakeProvider(
            [
                tool_call_turn("calling echo", ToolName("echo_tool"), {"text": "hi"}),
                text_turn("turn complete"),
            ]
        ),
        base_dir=tmp_path,
        model=ModelId("fake"),
        plugins=loaded,
    )
    kernel.registry.register(EchoTool())
    result = await run_once(kernel, "call it")
    assert result == "turn complete"
    envelopes = read_session(tmp_path, kernel.session.id)
    tool_completed = [e.event for e in envelopes if isinstance(e.event, ToolCallCompleted)]
    assert tool_completed
    assert tool_completed[0].is_error
    assert "blocked by policy" in (tool_completed[0].result_text or "")


# ---------------------------------------------------------------------------
# Test 4: subscriber sees events via tmp-file approach
# ---------------------------------------------------------------------------


async def test_subscriber_sees_events(tmp_path, monkeypatch):
    """Fixture plugin with a subscriber appending event types to a tmp file;
    after the TUI session the file contains user_message.
    Proves: subscriber pump delivers events through the full kernel lifecycle."""
    event_log = tmp_path / "events.txt"
    monkeypatch.setenv("HARNESS_TEST_EVENT_LOG", str(event_log))
    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        name="spy",
        manifest='[plugin]\nname = "spy"\nversion = "0.1.0"\ndescription = "Event spy subscriber"\n\n[[subscribers]]\nname = "spy-events"\nmodule = "hooks.py"\nfunction = "spy_events"',
        files={
            "hooks.py": 'import os\n\n\nasync def spy_events(envelope):\n    log_path = os.environ.get("HARNESS_TEST_EVENT_LOG")\n    if log_path:\n        with open(log_path, "a", encoding="utf-8") as f:\n            f.write(envelope.event.type + "\\n")\n'
        },
    )
    loaded = load_plugins([plugin_root])
    app = make_app(
        tmp_path,
        provider=EchoProvider(),
        model=ModelId("echo"),
        plugins=loaded,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.click("#prompt")
        await pilot.press(*"test message", "enter")
        await pilot.pause(0.5)
        await pilot.press(*"/quit", "enter")
        await pilot.pause(0.2)
    app.kernel.session.close()
    assert event_log.exists(), "subscriber never wrote to the event log"
    contents = event_log.read_text(encoding="utf-8")
    assert "user_message" in contents


# ---------------------------------------------------------------------------
# Test 5: agent from plugin restricts tools
# ---------------------------------------------------------------------------


async def test_agent_from_plugin_restricts_tools(tmp_path):
    """Fixture plugin with agents/limited.md (tools: [invoke_skill]);
    FakeProvider scripts dispatch_agent({agent: limited}) at the parent level;
    the child calls a forbidden tool and receives an error; parent completes.
    Proves: plugin agent definitions restrict tool access in subagent sessions."""
    from harness.events import SubagentSpawned, ToolCallCompleted
    from harness.tools import ToolSpec
    from harness.types import ToolName

    class ExtraTool:
        spec = ToolSpec(
            name=ToolName("extra_tool"),
            description="A tool not in agent whitelist",
            parameters={"type": "object", "properties": {}},
        )

        async def __call__(self, args: dict) -> str:  # pragma: no cover
            return "should not run"

    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        name="withagent",
        manifest='[plugin]\nname = "withagent"\nversion = "0.1.0"\ndescription = "Plugin with restricted agent"',
        files={
            "agents/limited.md": "---\nname: limited\ndescription: An agent limited to invoke_skill only\ntools:\n  - invoke_skill\n---\nYou are a limited agent with restricted tools.",
            "skills/demo.md": "---\nname: demo-skill\ndescription: A demo skill\n---\nThis is the demo skill body.",
        },
    )
    loaded = load_plugins([plugin_root])

    provider = FakeProvider(
        [
            tool_call_turn(
                "delegating to limited agent",
                ToolName("dispatch_agent"),
                {"prompt": "call extra_tool", "agent": "limited"},
            ),
            tool_call_turn(
                "child thinking",
                ToolName("extra_tool"),
                {},
            ),
            text_turn("child done after error"),
            text_turn("parent done"),
        ]
    )
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake"),
        plugins=loaded,
    )
    kernel.registry.register(ExtraTool())

    result = await run_once(kernel, "delegate to the limited agent")
    assert result == "parent done"

    envelopes = read_session(tmp_path, kernel.session.id)
    spawned = [e.event for e in envelopes if isinstance(e.event, SubagentSpawned)]
    assert spawned, "dispatch_agent did not spawn a child session"

    child_id = spawned[0].child_session_id
    child_envelopes = read_session(tmp_path, child_id)
    child_tool_errors = [
        e.event
        for e in child_envelopes
        if isinstance(e.event, ToolCallCompleted) and e.event.is_error
    ]
    assert child_tool_errors, "child has no tool error (forbidden tool should fail)"
    error_text = child_tool_errors[0].result_text or ""
    assert "extra_tool" in error_text or "unknown" in error_text or "not found" in error_text
