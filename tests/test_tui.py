"""Textual app: headless pilot tests. Each test builds a kernel on tmp_path."""

from textual.widgets import Input, RichLog

from harness.cli import build_kernel
from harness.log import read_session
from harness.provider import EchoProvider
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
