"""Export is a core control, with visible completion and no late write on Esc."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from textual.widgets import Input

from harness.log import read_session
from harness.provider import FakeProvider
from harness.tui_support import SlashCommand
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text
from tests.test_tui_tasks import command


async def test_export_command_is_visible_and_cannot_be_shadowed(tmp_path):
    app = make_app(tmp_path, provider=FakeProvider([]))
    output = tmp_path / "portable work.zip"
    async with app.run_test(size=(140, 45)) as pilot:
        await command(app, pilot, "/task new Continue elsewhere")
        await command(app, pilot, "/task require Inspect final work")
        before = read_session(tmp_path, app.kernel.session.id)
        app._plugin_commands["export"] = SimpleNamespace(body="run agent instructions")
        await command(app, pilot, f'/export "{output}"')
        await asyncio.wait_for(app._semantic_worker.wait(), 3)
        await pilot.pause(.1)
        assert output.exists() and "Exported CONTINUE.md" in screen_text(app)
        assert "1/1 unresolved" in screen_text(app)
        assert read_session(tmp_path, app.kernel.session.id) == before
        assert not app.kernel.provider.calls
        await command(app, pilot, "/help")
        assert "/export FILE.zip" in screen_text(app)
        composer = app.query_one("#prompt", Input)
        composer.value = "keep this draft"
        await app._run_command(SlashCommand("export", f'"{output}"'))
        await asyncio.wait_for(app._semantic_worker.wait(), 3)
        await pilot.pause(.1)
        assert "destination already exists; choose a new file path" in screen_text(app)
        assert composer.value == "keep this draft"


@pytest.mark.parametrize("interrupt", ["escape", "new_work"])
async def test_cancelled_preparation_cannot_publish_later(tmp_path, monkeypatch, interrupt):
    from harness.portable import prepare_export
    from harness.provider import text_turn
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def paused(*args, **kwargs):
        data = prepare_export(*args, **kwargs)
        entered.set()
        try:
            if not release.wait(10):
                raise TimeoutError("test did not release preparation")
            return data
        finally:
            finished.set()

    monkeypatch.setattr("harness.portable.prepare_export", paused)
    app = make_app(tmp_path, provider=FakeProvider([text_turn("continued")]))
    output = tmp_path / "cancelled.zip"
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            await command(app, pilot, "/task new Preserve work")
            await command(app, pilot, f"/export {output}")
            assert await asyncio.to_thread(entered.wait, 3)
            if interrupt == "escape":
                app.query_one("#prompt", Input).value = "unsent draft"
                await pilot.press("escape")
            else:
                await command(app, pilot, "Continue the task")
            await pilot.pause(.1)
            assert "Export cancelled before publication" in screen_text(app)
            release.set()
            assert await asyncio.to_thread(finished.wait, 3)
            await pilot.pause(.1)
            assert not output.exists()
            if interrupt == "escape":
                assert app.query_one("#prompt", Input).value == "unsent draft"
                assert not app.kernel.provider.calls
            else:
                assert len(app.kernel.provider.calls) == 1
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 3)
