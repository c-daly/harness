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
)
from harness.tui import HarnessApp
from harness.types import ModelId


def make_app(tmp_path, **kernel_kwargs) -> HarnessApp:
    kernel = build_kernel(
        provider=kernel_kwargs.pop("provider", EchoProvider()),
        base_dir=tmp_path,
        model=kernel_kwargs.pop("model", ModelId("echo")),
        **kernel_kwargs,
    )
    return HarnessApp(kernel)


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
