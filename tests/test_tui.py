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
    Usage,
    UsageReport,
    tool_call_turn,
    text_turn,
)
from harness.tools import ToolSpec
from harness.tui import AppBoundAsk, HarnessApp, PermissionScreen
from harness.tui_support import TuiResolver
from harness.types import ModelId, ToolName


class EchoTool:
    spec = ToolSpec(
        name=ToolName("echo_tool"),
        description="Echo the input back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    async def __call__(self, args: dict) -> str:
        return args["text"]


def make_app(tmp_path, **kernel_kwargs) -> HarnessApp:
    """Build a HarnessApp for tests.

    Special kwargs (consumed here, not forwarded to build_kernel):
      engine: PermissionEngine -- when given, wires up AppBoundAsk + TuiResolver.
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
    build_kwargs.update(kernel_kwargs)
    kernel = build_kernel(**build_kwargs)
    return HarnessApp(kernel, ask=ask)


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
        await pilot.pause(0.1)                  # turn parked at the gate
        await pilot.press(*"two", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
        provider.release.set()                  # let the first turn finish
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
    engine = PermissionEngine([
        RuleSet(rules=[PermissionRule(action="deny", tool="model:*")], default="allow")
    ])
    app = make_app(tmp_path, permissions=engine)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "turn failed" in lines
        await pilot.press(*"again", "enter")    # loop still accepts turns
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert lines.count("turn failed") == 2


async def test_streaming_tokens_appear_in_live_tail_then_finalize(tmp_path):
    provider = FakeProvider([[
        TextDelta(text="str"), TextDelta(text="eam"),
        UsageReport(usage=Usage()), StreamStop(stop_reason="end_turn"),
    ]])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "stream" in lines                                   # finalized
        assert app.query_one("#live", Static).content == ""       # tail cleared


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
            TextDelta(text="full"), TextDelta(text=" reply"),
            UsageReport(usage=Usage()), StreamStop(stop_reason="end_turn"),
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
        assert "parfull" not in lines          # the partial didnt bleed into the final
        assert "retrying" in lines             # the reset was announced


async def test_thinking_only_stream_clears_live_tail(tmp_path):
    provider = FakeProvider([[
        ThinkingDelta(text="pondering"), TextDelta(text="done thinking"),
        UsageReport(usage=Usage()), StreamStop(stop_reason="end_turn"),
    ], [
        ThinkingDelta(text="only thoughts"),
        UsageReport(usage=Usage()), StreamStop(stop_reason="end_turn"),
    ]])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.3)
        assert str(app.query_one("#live", Static).content) == ""
        await pilot.press(*"two", "enter")     # thinking-only turn
        await pilot.pause(0.3)
        assert str(app.query_one("#live", Static).content) == ""   # not stuck


async def test_permission_modal_allow_completes_turn(tmp_path):
    engine = PermissionEngine([
        RuleSet(
            rules=[PermissionRule(action="ask", tool="echo_tool")],
            default="allow",
        )
    ])
    provider = FakeProvider([
        tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
        text_turn("done"),
    ])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)                       # modal up; dispatch parked
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
    engine = PermissionEngine([
        RuleSet(
            rules=[PermissionRule(action="ask", tool="echo_tool")],
            default="allow",
        )
    ])
    provider = FakeProvider([
        tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
        text_turn("done"),
    ])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)                       # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("n")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "denied by user" in lines


async def test_permission_modal_always_persists_grant(tmp_path):
    grants_path = tmp_path / "grants.toml"
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ],
        grants_path=grants_path,
    )
    provider = FakeProvider([
        tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
        text_turn("done"),
    ])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)                       # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("a")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "done" in lines
    # grant was persisted
    assert grants_path.exists()
    assert "echo_tool" in grants_path.read_text()


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
    engine = PermissionEngine([
        RuleSet(
            rules=[PermissionRule(action="ask", tool="echo_tool")],
            default="allow",
        )
    ])
    provider = FakeProvider([
        tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
        text_turn("done"),
    ])
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
    engine = PermissionEngine([
        RuleSet(
            rules=[PermissionRule(action="ask", tool="echo_tool")],
            default="allow",
        )
    ])
    provider = ParkingProvider(
        tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"})
    )
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
