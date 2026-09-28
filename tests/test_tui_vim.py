"""Opt-in Vim prompt editing uses the real Textual key path."""

from textual.widgets import Input, Static

from harness.tui import HistoryInput
from tests.test_tui import make_app


async def test_vim_prompt_modes_motion_edit_and_submit(tmp_path):
    app = make_app(tmp_path, vim=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        prompt = app.query_one("#prompt", HistoryInput)
        assert prompt.vim_mode == "normal"
        assert app.query_one("#input-mode", Static).display
        await pilot.press(*"hello")  # normal mode must not insert bare letters
        assert prompt.value == ""
        await pilot.press("i", *"hello", "escape")
        assert prompt.value == "hello"
        assert prompt.vim_mode == "normal"
        assert prompt.cursor_position == 4
        await pilot.press("0", "x", "A", "!")
        assert prompt.value == "ello!"
        assert prompt.vim_mode == "insert"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert prompt.value == ""
        assert [m.text() for m in app.kernel.loop.history if m.role == "user"] == ["ello!"]
        await pilot.press("escape", "k")
        assert prompt.value == "ello!"
        await pilot.press("d", "d")
        assert prompt.value == ""


async def test_vim_can_toggle_without_restarting_session(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        prompt = app.query_one("#prompt", Input)
        prompt.value = "/vim on"
        await pilot.press("enter")
        assert app.query_one("#prompt", HistoryInput).vim_mode == "normal"
        await pilot.press("i", *"draft", "escape")
        assert prompt.value == "draft"
        prompt.value = "/vim off"
        await pilot.press("enter")
        assert not app.query_one("#input-mode", Static).display
        await pilot.press(*"ordinary")
        assert prompt.value == "ordinary"
