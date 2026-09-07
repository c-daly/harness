"""Task continuity must be visible in the composed terminal, including recovery."""

import json

from textual.widgets import Input

from harness.provider import FakeProvider, text_turn
from harness.log import read_session
from harness.tui_support import SlashCommand
from tests.test_tui import GatedProvider, make_app
from tests.test_tui_queue import screen_text
from tests.test_task_evidence import digest


async def command(app, pilot, text):
    # Submit through the actual composer; avoid key-by-key latency for JSON fixtures.
    app.query_one("#prompt", Input).value = text
    await pilot.click("#prompt")
    await pilot.press("enter")
    await pilot.pause(0.1)


async def test_task_requirements_check_review_and_continuation_are_visible(tmp_path):
    app = make_app(tmp_path, provider=FakeProvider([text_turn("done"), text_turn("changed")]))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        await command(app, pilot, "/task new Produce expected result")
        await command(app, pilot, "/task require Inspect the result")
        await command(app, pilot, "/task require-json " + json.dumps({
            "id": "output", "description": "Exact output", "check": {
                "kind": "output", "sha256": digest("done")}}))
        await command(app, pilot, "Do it")
        assert "2/2 unresolved" in screen_text(app)
        await command(app, pilot, "/task check")
        assert "1/2 unresolved" in screen_text(app)
        assert "recorded bytes match" in screen_text(app)
        await command(app, pilot, "/task confirm r1 I inspected the result")
        assert "0/2 unresolved" in screen_text(app)
        await command(app, pilot, "/task accept Reviewed and accepted")
        assert "accepted by user" in screen_text(app)
        await command(app, pilot, "Change it")
        banner = str(app.query_one("#task-status").render())
        assert "2/2 unresolved" in banner and "accepted by user" not in banner
        runs = [e.event for e in read_session(tmp_path, app.kernel.session.id)
                if e.event.type == "agent_run_started"]
        assert len(runs) == 2 and runs[0].task_id == runs[1].task_id


async def test_busy_task_commands_preserve_selection_queue_and_draft(tmp_path):
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await command(app, pilot, "/task new Keep working")
        await command(app, pilot, "/task require Review work")
        task_id = app.kernel.tasks.selected().definition.id
        await command(app, pilot, "Work")
        await command(app, pilot, "Follow up")
        app.query_one("#prompt", Input).value = "unsent draft"
        await app._run_command(SlashCommand("task", "off"))
        assert app.kernel.tasks.selected().definition.id == task_id
        assert len(app.controller.pending) == 1
        assert app.query_one("#prompt", Input).value == "unsent draft"
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert "1/1 unresolved" in screen_text(app)
        assert "execution: cancelled" in screen_text(app)
        assert "unsent draft" in screen_text(app)
        assert "Queue paused" in screen_text(app)


async def test_resumed_task_and_requirements_reappear_and_clear_changes_session(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await command(app, pilot, "/task new Persistent objective")
        await command(app, pilot, "/task require Verify outcome")
        session_id = app.kernel.session.id
        await app._rebuild_kernel(resume_session_id=session_id)
        await pilot.pause(0.1)
        assert "Persistent objective" in screen_text(app) and "1/1 unresolved" in screen_text(app)
        await command(app, pilot, "/task off")
        assert "1 task(s) awaiting acceptance" in screen_text(app)
        await command(app, pilot, "/clear")
        assert not app.query_one("#task-status").display
        assert app.kernel.session.id != session_id


async def test_plain_task_inspection_never_renders_user_markup_or_calls_provider(tmp_path):
    from harness.frontmatter import CommandDef
    app = make_app(tmp_path)
    # The core command must remain reachable if a plugin uses the same name.
    app._plugin_commands["task"] = CommandDef(name="task", description="collision", body="plugin prompt")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await command(app, pilot, "/task new [red]Literal objective[/red]")
        await command(app, pilot, "/task require Keep [bold]text[/bold]")
        await command(app, pilot, "/task")
        screen = screen_text(app)
        assert "[red]Literal objective[/red]" in screen
        assert "Keep [bold]text[/bold]" in screen
        assert not app.kernel.loop.history
