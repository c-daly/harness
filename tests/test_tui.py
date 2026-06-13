"""Textual app: headless pilot tests. Each test builds a kernel on tmp_path."""

import asyncio

from textual.widgets import Input, RichLog, Static

from harness.cli import build_kernel
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import (
    EchoProvider,
    FakeProvider,
    StreamStop,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageReport,
    tool_call_turn,
    text_turn,
)
from harness.tools import ToolSpec
from harness.tui import AppBoundAsk, HarnessApp, PermissionScreen
from harness.tui_support import TuiResolver
from harness.types import CallId, ModelId, ToolName
from tests.conftest import fixture_stdio_spec

from harness.plugins import load_plugins


class EchoTool:
    spec = ToolSpec(
        name=ToolName("echo_tool"),
        description="Echo the input back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    async def __call__(self, args: dict) -> str:
        return args["text"]


class PathEchoTool:
    # A write_file-named stub: its primary arg is file_path, so grant_pattern yields a
    # non-empty workspace-scoped match that persists (unlike an empty-match allow-all).
    spec = ToolSpec(
        name=ToolName("write_file"),
        description="Write file stub",
        parameters={
            "type": "object",
            "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        },
    )

    async def __call__(self, args: dict) -> str:
        return args["file_path"]


def make_app(tmp_path, catalog_path=None, plugins=None, **kernel_kwargs) -> HarnessApp:
    """Build a HarnessApp for tests.

    Special kwargs (consumed here, not forwarded to build_kernel):
      engine: PermissionEngine -- when given, wires up AppBoundAsk + TuiResolver.
      catalog_path: Path -- when given, forwarded to HarnessApp for /model.
      plugins: LoadedPlugins -- when given, forwarded to build_kernel.
    """
    engine = kernel_kwargs.pop("engine", None)
    ask: AppBoundAsk | None = None
    resolver = None
    if engine is not None:
        ask = AppBoundAsk()
        resolver = TuiResolver(ask=ask, engine=engine)
    build_kwargs: dict = dict(
        provider=kernel_kwargs.pop("provider", EchoProvider()),
        base_dir=tmp_path,
        model=kernel_kwargs.pop("model", ModelId("echo")),
    )
    if resolver is not None:
        build_kwargs["resolver"] = resolver
    if engine is not None and "permissions" not in kernel_kwargs:
        build_kwargs["permissions"] = engine
    if plugins is not None:
        build_kwargs["plugins"] = plugins
    build_kwargs.update(kernel_kwargs)
    kernel = build_kernel(**build_kwargs)
    return HarnessApp(kernel, catalog_path=catalog_path, ask=ask)


async def test_submit_renders_user_line_and_reply(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hi there", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "hi there" in lines
        assert "echo: hi there" in lines
        assert app.query_one(Input).value == ""


async def test_session_started_exactly_once_and_teardown(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.2)
    # run_test exits the app; on_unmount fires _finish() -> loop.end() -> session_ended
    # session.close() is NOT called by the app (that is run_tui job), call it here
    kernel = app.kernel
    kernel.session.close()
    envelopes = read_session(tmp_path, kernel.session.id)
    types = [e.event.type for e in envelopes]
    assert types[0] == "session_started"
    assert types.count("session_started") == 1
    assert "session_ended" in types


class GatedProvider:
    """Deterministic in-flight turn: parks until the test releases the gate."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(self, *, model, messages, tools):
        await self.release.wait()
        yield TextDelta(text="gated done")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_second_submit_while_turn_running_is_rejected(tmp_path):
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.1)  # turn parked at the gate
        await pilot.press(*"two", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
        provider.release.set()  # let the first turn finish
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "gated done" in lines
    # exactly ONE user_message in the log
    app.kernel.session.close()
    envelopes = read_session(tmp_path, app.kernel.session.id)
    assert [e.event.type for e in envelopes].count("user_message") == 1


async def test_turn_failure_renders_and_loop_survives(tmp_path):
    # engine denying model:* (tests/test_permissions.py construction); every
    # turn raises ModelDispatchBlocked, so the loop must survive repeat failures.
    engine = PermissionEngine(
        [RuleSet(rules=[PermissionRule(action="deny", tool="model:*")], default="allow")]
    )
    app = make_app(tmp_path, permissions=engine)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "turn failed" in lines
        await pilot.press(*"again", "enter")  # loop still accepts turns
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert lines.count("turn failed") == 2


async def test_streaming_tokens_appear_in_live_tail_then_finalize(tmp_path):
    provider = FakeProvider(
        [
            [
                TextDelta(text="str"),
                TextDelta(text="eam"),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="end_turn"),
            ]
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "stream" in lines  # finalized
        assert app.query_one("#live", Static).content == ""  # tail cleared


class _FlakyStreamProvider:
    """Attempt 1 yields partial then raises Overloaded; attempt 2 yields the full reply."""

    def __init__(self):
        self.attempts = 0

    async def complete(self, *, model, messages, tools=()):
        from harness.errors import Overloaded

        self.attempts += 1
        if self.attempts < 2:
            yield TextDelta(text="par")
            raise Overloaded("busy")
        for chunk in [
            TextDelta(text="full"),
            TextDelta(text=" reply"),
            UsageReport(usage=Usage()),
            StreamStop(stop_reason="end_turn"),
        ]:
            yield chunk


async def test_retry_resets_live_tail_no_duplication(tmp_path):
    provider = _FlakyStreamProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    app.kernel.loop.dispatcher.retry_delays = (0.0,)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.4)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "full reply" in lines
        assert "parfull" not in lines  # the partial didnt bleed into the final
        assert "retrying" in lines  # the reset was announced


async def test_thinking_only_stream_clears_live_tail(tmp_path):
    provider = FakeProvider(
        [
            [
                ThinkingDelta(text="pondering"),
                TextDelta(text="done thinking"),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="end_turn"),
            ],
            [
                ThinkingDelta(text="only thoughts"),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="end_turn"),
            ],
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.3)
        assert str(app.query_one("#live", Static).content) == ""
        await pilot.press(*"two", "enter")  # thinking-only turn
        await pilot.pause(0.3)
        assert str(app.query_one("#live", Static).content) == ""  # not stuck


async def test_permission_modal_allow_completes_turn(tmp_path):
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = FakeProvider(
        [
            tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up; dispatch parked
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("y")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "done" in lines
    app.kernel.session.close()
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert "permission_requested" in events
    assert "permission_resolved" in events


async def test_permission_modal_deny_blocks_tool(tmp_path):
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = FakeProvider(
        [
            tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("n")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "denied by user" in lines


async def test_permission_modal_always_persists_grant(tmp_path):
    # Uses write_file so grant_pattern yields a non-empty workspace-scoped path match:
    # only constrained grants persist (C1 -- an empty-match allow-all rule is never
    # written to grants.toml from a single keystroke).
    grants_path = tmp_path / "grants.toml"
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="write_file")],
                default="allow",
            )
        ],
        grants_path=grants_path,
    )
    provider = FakeProvider(
        [
            tool_call_turn(
                "calling", ToolName("write_file"), {"file_path": "/w/proj/a.txt", "content": "hi"}
            ),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(PathEchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("a")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "done" in lines
    # grant was persisted (constrained to the workspace path glob)
    assert grants_path.exists()
    assert "write_file" in grants_path.read_text()


async def test_escape_interrupts_turn_and_loop_survives(tmp_path):
    provider = GatedProvider()  # parks until released -- never released here
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
        # loop survives: swap provider and run another turn
        app.kernel.loop.provider = EchoProvider()
        await pilot.click("#prompt")
        await pilot.press(*"again", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "echo: again" in lines
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("user_interrupt") == 1


async def test_escape_with_no_turn_running_is_a_noop(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)
        events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
        assert "user_interrupt" not in events


async def test_escape_on_permission_modal_denies_it(tmp_path):
    # The app-level priority Esc binding preempts the modal own escape binding;
    # action_interrupt must DELEGATE to the modal -- this test pins that behaviour.
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = FakeProvider(
        [
            tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, PermissionScreen)  # modal gone
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "denied by user" in lines
        events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
        assert "user_interrupt" not in events  # the TURN was not interrupted


class ParkingProvider:
    """First complete() call yields the given chunks; subsequent calls park on an
    Event (like GatedProvider) -- used to drive a turn into a second, blocked model
    dispatch so Esc can interrupt a genuinely-running turn."""

    def __init__(self, first_chunks):
        self.first_chunks = first_chunks
        self.calls = 0
        self.release = asyncio.Event()

    async def complete(self, *, model, messages, tools=()):
        self.calls += 1
        if self.calls == 1:
            for chunk in self.first_chunks:
                yield chunk
            return
        await self.release.wait()  # park: never released in these tests
        yield TextDelta(text="unreached")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_escape_after_modal_close_interrupts_running_turn(tmp_path):
    # Esc #1 denies the modal (turn keeps running); the turn proceeds to model call
    # #2, which parks; Esc #2 interrupts the genuinely-running turn. Exactly one
    # user_interrupt is recorded -- the modal-deny Esc must NOT count as an interrupt.
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = ParkingProvider(tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}))
    app = make_app(tmp_path, provider=provider, model=ModelId("parking"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up after tool call in turn #1
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("escape")  # Esc #1: deny the modal
        await pilot.pause(0.3)
        assert not isinstance(app.screen, PermissionScreen)  # modal gone
        await pilot.pause(0.3)  # turn proceeds to model call #2 -> parks
        await pilot.press("escape")  # Esc #2: interrupt the running turn
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "denied by user" in lines
        assert "interrupted" in lines
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("user_interrupt") == 1


class PartialThenParkProvider:
    """Yields some streamed text, then parks on an Event -- so an interrupt has a
    non-empty stream buffer to preserve."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(self, *, model, messages, tools=()):
        yield TextDelta(text="partial output")
        await self.release.wait()  # park: never released here
        yield TextDelta(text=" tail")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_escape_preserves_partial_streamed_output(tmp_path):
    provider = PartialThenParkProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("partial"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)  # partial streamed, turn parked
        await pilot.press("escape")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "~ partial output" in lines  # the partial was preserved with the ~ prefix
        assert "interrupted" in lines
        assert app.query_one("#live", Static).content == ""  # live tail cleared
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("user_interrupt") == 1


MODELS_TOML_TWO_ALIASES = (
    "[models.alias-a]\n"
    "route = 'local/model-a'\n"
    "input_cost_per_token = 0.0\n"
    "output_cost_per_token = 0.0\n"
    "\n"
    "[models.alias-b]\n"
    "route = 'local/model-b'\n"
    "input_cost_per_token = 0.0\n"
    "output_cost_per_token = 0.0\n"
)


async def test_slash_help_and_tools_and_unknown(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/help", "enter")
        await pilot.press(*"/tools", "enter")
        await pilot.press(*"/nope", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "/model" in lines  # help lists commands
        assert "dispatch_agent" in lines  # tools lists the builtin
        assert "unknown command" in lines


async def test_slash_model_switches_via_catalog(tmp_path):
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_TWO_ALIASES)
    app = make_app(tmp_path, catalog_path=catalog_file)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model", "enter")  # no arg: list aliases
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "alias-a" in lines and "alias-b" in lines
        await pilot.press(*"/model alias-b", "enter")
        await pilot.pause(0.3)  # switch runs in a worker now (lazy litellm import)
        # the ALIAS now flows through dispatch; the CatalogProvider resolves the
        # route per call. The route still shows in the confirmation line.
        from harness.provider_litellm import CatalogProvider

        assert str(app.kernel.loop.model) == "alias-b"
        assert app.kernel.loop.model_pinned is True
        assert isinstance(app.kernel.loop.provider, CatalogProvider)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "local/model-b" in lines  # route surfaced in 'model → alias-b (route)'
        await pilot.press(*"/model nope", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "unknown alias" in lines


async def test_slash_model_without_catalog_says_so(tmp_path):
    app = make_app(tmp_path)  # no catalog_path
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "no catalog" in lines


async def test_slash_quit_exits_cleanly(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/quit", "enter")
        await pilot.pause(0.2)
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert "session_ended" in events
    assert [e for e in events].count("session_ended") == 1


async def test_slash_quit_during_running_turn_is_clean(tmp_path):
    provider = GatedProvider()  # never released
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"stuck", "enter")
        await pilot.pause(0.2)  # turn parked
        await pilot.press(*"/quit", "enter")
        await pilot.pause(0.3)
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("session_ended") == 1  # no crash, clean single end


async def test_stats_line_updates_after_a_turn(tmp_path):
    provider = FakeProvider(
        [
            [
                TextDelta(text="hi"),
                UsageReport(usage=Usage(input_tokens=7, output_tokens=3)),
                StreamStop(stop_reason="end_turn"),
            ]
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        app.refresh_stats()  # poke instead of waiting 1s
        await pilot.pause(0.1)
        stats = str(app.query_one("#stats", Static).content)
        assert "in 7" in stats and "out 3" in stats
        assert "tools 0" in stats


async def test_tui_pipes_mcp_child_stderr_to_file(tmp_path):
    kernel = build_kernel(
        provider=EchoProvider(),
        base_dir=tmp_path,
        model=ModelId("echo"),
        mcp=[fixture_stdio_spec()],
    )
    app = HarnessApp(kernel)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)  # mcp start + session driver
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.3)
    try:
        errlog_path = tmp_path / "sessions" / str(kernel.session.id) / "mcp-stderr.log"
        assert errlog_path.exists()
        assert "ListToolsRequest" in errlog_path.read_text()
    finally:
        # close what run_tui would close in its finally (run_test does not run run_tui)
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await kernel.mcp.stop()
        kernel.session.close()


async def test_hostile_tool_name_renders_neutralized(tmp_path):
    """Transcript rendering strips control bytes and never interprets markup
    from tool-controlled strings (the \u2699 line renders str(tool) via _plain)."""
    hostile = "ev\x1b[31mil\x07[bold red]X[/]\rZZ"

    class HostileTool:
        spec = ToolSpec(name=ToolName(hostile), description="", parameters={})

        async def __call__(self, args):
            return "ok"

    provider = FakeProvider(
        [
            tool_call_turn("", ToolName(hostile), {}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    app.kernel.registry.register(HostileTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        rendered = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "\x1b" not in rendered and "\x07" not in rendered and "\r" not in rendered
        assert "done" in rendered


async def test_malformed_stream_fails_turn_and_loop_survives(tmp_path):
    """A provider yielding garbage tool-call JSON raises MalformedStreamError
    (non-retryable): the TUI renders the failure, repairs, and the next turn works."""
    provider = FakeProvider(
        [
            [
                ToolCallDelta(
                    index=0, call_id=CallId("x1"), tool=ToolName("echo_tool"), args_json="{not json"
                ),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="tool_use"),
            ]
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "turn failed" in lines
        app.kernel.loop.provider = EchoProvider()
        await pilot.click("#prompt")
        await pilot.press(*"again", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "echo: again" in lines


# ---------------------------------------------------------------------------
# Task 7: TUI plugin commands
# ---------------------------------------------------------------------------

MINIMAL_PLUGIN_MANIFEST = """
[plugin]
name = "echo-plugin"
version = "0.1.0"
description = "Plugin for TUI command tests"
"""

_ECHO_COMMAND_CONTENT = (
    "---\nname: echo-args\ndescription: Echo the arguments\n---\nPlease echo: $ARGUMENTS"
)


def _make_echo_plugin(tmp_path):
    """Create a plugin with commands/echo-args.md in tmp_path; return LoadedPlugins."""
    plugin_dir = tmp_path / "echo-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(MINIMAL_PLUGIN_MANIFEST)
    commands_dir = plugin_dir / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo-args.md").write_text(_ECHO_COMMAND_CONTENT)
    return load_plugins([tmp_path])


async def test_plugin_command_submits_its_body_as_a_turn(tmp_path):
    """Invoking /echo-args expands $ARGUMENTS and submits the body as a normal turn."""
    loaded = _make_echo_plugin(tmp_path / "plugins")
    app = make_app(tmp_path, plugins=loaded)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/echo-args hello world", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "Please echo: hello world" in lines  # the expanded body was submitted
        assert "echo: Please echo: hello world" in lines  # EchoProvider replied


async def test_plugin_command_listed_in_help_and_unknown_still_unknown(tmp_path):
    """Plugin commands appear in /help output; unknown commands remain unknown."""
    loaded = _make_echo_plugin(tmp_path / "plugins")
    app = make_app(tmp_path, plugins=loaded)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/help", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "/echo-args" in lines
        await pilot.press(*"/nope", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "unknown command" in lines


async def test_plugin_command_respects_turn_guard(tmp_path):
    """Invoking a plugin command mid-turn renders the \"already running\" message."""
    loaded = _make_echo_plugin(tmp_path / "plugins")
    provider = GatedProvider()  # parks until released -- never released here
    app = make_app(tmp_path, plugins=loaded, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")  # start a turn; parks at gate
        await pilot.pause(0.1)
        await pilot.press(*"/echo-args hi", "enter")  # plugin command mid-turn
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
