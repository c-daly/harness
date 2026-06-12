"""Phase 6 milestone: the TUI as subscriber plus decision provider -- streaming, enforcement,
interrupt, stats, MCP -- headless via Pilot."""

from textual.widgets import RichLog, Static

from harness.cli import build_kernel, run_once
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import (
    EchoProvider,
    FakeProvider,
    StreamStop,
    TextDelta,
    Usage,
    UsageReport,
    text_turn,
    tool_call_turn,
)
from harness.tui import HarnessApp, PermissionScreen
from harness.types import ModelId, ToolName
from tests.conftest import fixture_stdio_spec

# Import reusable helpers from tests.test_tui -- tests/ is a package.
from tests.test_tui import GatedProvider, make_app


# ---------------------------------------------------------------------------
# Test 1: TUI session over real MCP server
# ---------------------------------------------------------------------------

async def test_tui_session_over_real_mcp_server(tmp_path):
    """build_kernel with a real stdio MCP server: the TUI submits a prompt, the
    MCP tool fires, the transcript shows the tool line (\u2699/\u2713 with \"42\") and \"done\";
    the log tail is session_ended then mcp/server_stopped; exactly one session_started."""
    provider = FakeProvider([
        tool_call_turn("calling add", ToolName("mcp__fixture__add"), {"a": 19, "b": 23}),
        text_turn("done"),
    ])
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
    )
    app = HarnessApp(kernel)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.5)  # mcp start + session driver
            await pilot.click("#prompt")
            await pilot.press(*"add the numbers", "enter")
            await pilot.pause(0.8)  # tool call + second turn
            lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert "42" in lines
            assert "done" in lines
            # MCP lifecycle events rendered
            assert "mcp" in lines
            assert "server_started" in lines
    finally:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await kernel.mcp.stop()
        kernel.mcp.flush_events()
        kernel.session.close()

    envelopes = read_session(tmp_path, kernel.session.id)
    types = [e.event.type for e in envelopes]

    # Exactly one session_started
    assert types.count("session_started") == 1

    # Tail order: session_ended then mcp/server_stopped
    tail = envelopes[-2:]
    tail_types = [e.event.type for e in tail]
    assert tail_types == ["session_ended", "custom"]
    last = tail[-1].event
    assert last.namespace == "mcp" and last.name == "server_stopped"


# ---------------------------------------------------------------------------
# Test 2: TUI permission modal over MCP tool
# ---------------------------------------------------------------------------

async def test_tui_permission_modal_over_mcp_tool(tmp_path):
    """ask-rule on mcp__fixture__add: modal appears, \"y\" allows, turn completes;
    permission_requested + permission_resolved land in the log."""
    grants_path = tmp_path / "grants.toml"
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="mcp__fixture__add")],
                default="allow",
            )
        ],
        grants_path=grants_path,
    )
    provider = FakeProvider([
        tool_call_turn("calling add", ToolName("mcp__fixture__add"), {"a": 19, "b": 23}),
        text_turn("done"),
    ])
    # make_app handles engine -> AppBoundAsk + TuiResolver wiring and kernel_kwargs forwarding.
    app = make_app(
        tmp_path,
        provider=provider,
        model=ModelId("fake:echo"),
        engine=engine,
        mcp=[fixture_stdio_spec()],
    )
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.5)  # mcp start
            await pilot.click("#prompt")
            await pilot.press(*"run it", "enter")
            await pilot.pause(0.5)  # modal up; dispatch parked
            assert isinstance(app.screen, PermissionScreen)
            await pilot.press("y")
            await pilot.pause(0.8)  # second turn + tool completion
            lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert "done" in lines
    finally:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await app.kernel.mcp.stop()
        app.kernel.mcp.flush_events()
        app.kernel.session.close()

    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert "permission_requested" in events
    assert "permission_resolved" in events


# ---------------------------------------------------------------------------
# Test 3: Esc then recovery full lifecycle cycle
# ---------------------------------------------------------------------------

async def test_tui_esc_then_recovery_full_cycle(tmp_path):
    """GatedProvider parks a turn; Esc interrupts it and \"interrupted\" renders;
    swap to EchoProvider, another submit succeeds; exactly one user_interrupt;
    /quit exits cleanly with exactly one session_ended."""
    provider = GatedProvider()  # parks until released -- never released
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"slow", "enter")
        await pilot.pause(0.2)  # turn parked in the provider
        await pilot.press("escape")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "interrupted" in lines.lower()
        # Swap provider and verify loop survives
        app.kernel.loop.provider = EchoProvider()
        await pilot.click("#prompt")
        await pilot.press(*"again", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "echo: again" in lines
        # /quit for clean teardown
        await pilot.press(*"/quit", "enter")
        await pilot.pause(0.2)

    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("user_interrupt") == 1
    assert events.count("session_ended") == 1


# ---------------------------------------------------------------------------
# Test 4: Stats line after a usage turn
# ---------------------------------------------------------------------------

async def test_tui_stats_after_usage_turn(tmp_path):
    """FakeProvider with Usage(input_tokens=11, output_tokens=5);
    after refresh_stats, \"in 11\" and \"out 5\" appear in #stats."""
    provider = FakeProvider([[
        TextDelta(text="hi"),
        UsageReport(usage=Usage(input_tokens=11, output_tokens=5)),
        StreamStop(stop_reason="end_turn"),
    ]])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        app.refresh_stats()  # poke instead of waiting 1s
        await pilot.pause(0.1)
        stats = str(app.query_one("#stats", Static).content)
        assert "in 11" in stats
        assert "out 5" in stats


# ---------------------------------------------------------------------------
# Test 5: Resume renders prior exchange
# ---------------------------------------------------------------------------

async def test_tui_resume_renders_prior_exchange(tmp_path):
    """Run a headless session first; then build a TUI kernel with resume_session_id
    and run_test: the RichLog contains the prior exchange; exactly one session_started
    in the resumed log; session_resumed is present; submit \"next\" -> \"echo: next\" works."""
    # Phase 1: headless run
    first_provider = FakeProvider([text_turn("first answer")])
    first_kernel = build_kernel(
        provider=first_provider,
        base_dir=tmp_path,
        model=ModelId("fake:echo"),
    )
    reply = await run_once(first_kernel, "first question")
    assert reply == "first answer"
    first_session_id = first_kernel.session.id

    # Phase 2: TUI resume
    app = make_app(
        tmp_path,
        provider=EchoProvider(),
        model=ModelId("echo"),
        resume_session_id=first_session_id,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.2)  # mount + render history
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        # Prior exchange rendered from history
        assert "first question" in lines
        assert "first answer" in lines
        # Submit a new turn
        await pilot.click("#prompt")
        await pilot.press(*"next", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "echo: next" in lines

    # Check resumed session log
    envelopes = read_session(tmp_path, app.kernel.session.id)
    types = [e.event.type for e in envelopes]
    # Exactly one session_started (from the original run)
    assert types.count("session_started") == 1
    # session_resumed marks the TUI run boundary
    assert "session_resumed" in types
