"""Textual app: headless pilot tests. Each test builds a kernel on tmp_path."""

import asyncio

from textual.widgets import Input, RichLog

from harness.cli import build_kernel
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import (
    EchoProvider,
    StreamStop,
    TextDelta,
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


class SlowProvider:
    """Echo with a 0.5s think time -- long enough to probe concurrency."""

    async def complete(self, *, model, messages, tools):
        await asyncio.sleep(0.5)
        yield TextDelta(text="slow done")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_second_submit_while_turn_running_is_rejected(tmp_path):
    app = make_app(tmp_path, provider=SlowProvider(), model=ModelId("slow"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.1)                  # turn parked in the provider
        await pilot.press(*"two", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
        await pilot.pause(0.8)                  # first turn finishes
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "slow done" in lines
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
