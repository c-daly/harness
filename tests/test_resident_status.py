"""One explicit view of task, configured context, and local readiness."""

import asyncio

from textual.widgets import Input

from harness.events import ContextSourceObserved, ResourceObserved
from harness.log import read_session
from harness.resident import render_status
from harness.resources import ResourceObservation
from harness.status_cli import main
from harness.tui_support import SlashCommand
from tests.test_resident_context import Lookup, make_kernel, policy
from tests.test_tui import make_app
from tests.test_tui_queue import screen_text
from tests.test_tui_tasks import command


async def test_status_cli_reads_saved_state_without_changing_log_or_using_model(tmp_path, capsys):
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=Lookup())
    kernel.tasks.create("Continue project")
    kernel.tasks.add_requirement({"id": "review", "description": "Review work"})
    await kernel.loop.run_task(kernel.tasks.prepare("work"))
    kernel.session.append(ResourceObserved(observation=ResourceObservation(
        alias="local", config_digest="fixture", status="ready", stale=False)))
    before = read_session(tmp_path, kernel.session.id)
    try:
        main([str(kernel.session.id), "--base-dir", str(tmp_path)])
        text = capsys.readouterr().out
        assert "Continue project" in text and "1/1 unresolved" in text
        assert "normal-memory: ready for recorded attempt" in text
        assert "local: ready (stale)" in text and "not a live readiness check" in text
        assert len(kernel.provider.calls) == 1
        assert read_session(tmp_path, kernel.session.id) == before
    finally:
        kernel.session.close()


async def test_new_attempt_never_displays_previous_source_as_ready(tmp_path):
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=Lookup())
    kernel.tasks.create("Continue project")
    first = await kernel.loop.run_task(kernel.tasks.prepare("work"))
    from harness.events import AgentRunStarted
    try:
        kernel.session.append(AgentRunStarted(task_id=first.task_id, run_id="new", runtime="harness"))
        text = render_status(read_session(tmp_path, kernel.session.id))
        assert "not fetched for this attempt" in text
        assert "ready for recorded attempt" not in text
        kernel.session.append(ContextSourceObserved(task_id=first.task_id, run_id="new", source_id="normal-memory",
            policy_digest=policy().digest, status="fetching"))
        assert "completion unconfirmed" in render_status(read_session(tmp_path, kernel.session.id))
    finally:
        kernel.session.close()


async def test_optional_failure_and_status_are_visible_without_source_payload(tmp_path):
    app = make_app(tmp_path, context_policy=policy())
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        await command(app, pilot, "/task new Keep continuity")
        await command(app, pilot, "/task require Review work")
        await command(app, pilot, "work")
        await command(app, pilot, "/status")
        screen = screen_text(app)
        assert "Context normal-memory: unavailable" in screen
        assert "Keep continuity" in screen and "1/1 unresolved" in screen
        assert "No local runtime profiles configured" in screen


async def test_context_cancellation_preserves_queue_draft_and_status(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()
    class Waiting(Lookup):
        async def __call__(self, args):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
    app = make_app(tmp_path, context_policy=policy(timeout_seconds=30))
    app.kernel.registry.register(Waiting())
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.1)
        await command(app, pilot, "/task new Keep continuity")
        await command(app, pilot, "/task require Review work")
        await command(app, pilot, "work")
        await asyncio.wait_for(entered.wait(), 2)
        await command(app, pilot, "follow up")
        app.query_one("#prompt", Input).value = "unsent draft"
        await app._run_command(SlashCommand("status", ""))
        await pilot.pause(0.1)
        assert "Context normal-memory: fetching" in screen_text(app)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert closed.is_set()
        await app._run_command(SlashCommand("status", ""))
        await pilot.pause(0.1)
        screen = screen_text(app)
        assert "Context normal-memory: cancelled" in screen and "execution: cancelled" in screen
        assert "Queue paused" in screen and "follow up" in screen and "unsent draft" in screen
        assert app.query_one("#prompt", Input).value == "unsent draft"
        assert not any(e.event.type == "model_call_proposed" for e in read_session(tmp_path, app.kernel.session.id))


async def test_source_deadline_removes_its_expired_permission_modal(tmp_path):
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    from harness.tui import PermissionScreen
    engine = PermissionEngine([RuleSet(rules=[PermissionRule("ask", "memory_lookup")], default="allow")])
    app = make_app(tmp_path, context_policy=policy(timeout_seconds=0.3), engine=engine)
    lookup = Lookup()
    app.kernel.registry.register(lookup)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        composer = app.query_one("#prompt", Input)
        composer.post_message(Input.Submitted(composer, "work"))
        await pilot.pause(0.1)
        assert isinstance(app.screen, PermissionScreen)
        await pilot.pause(0.4)
        assert not isinstance(app.screen, PermissionScreen)
        assert not lookup.calls and app.controller.active is None
        assert "Context normal-memory: timeout" in screen_text(app)


async def test_expiring_background_permission_preserves_the_live_prompt_above_it(tmp_path):
    from harness.hooks import ProposedToolCall
    from harness.interaction import PermissionRequest
    from harness.tui import AppBoundAsk, PermissionScreen
    from harness.types import CallId, ToolName
    app = make_app(tmp_path)
    ask = AppBoundAsk()
    ask.app = app
    def request(name):
        return PermissionRequest(CallId(name), ProposedToolCall(CallId(name), ToolName("lookup"), {}), name)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        old = app.run_worker(ask(request("old")), exit_on_error=False)
        await pilot.pause(0.1)
        newer = app.run_worker(ask(request("newer")), exit_on_error=False)
        await pilot.pause(0.1)
        old.cancel()
        await pilot.pause(0.1)
        assert isinstance(app.screen, PermissionScreen) and app.screen.request.call_id == "newer"
        await pilot.press("y")
        await pilot.pause(0.2)
        assert newer.result == "allow"
        assert not any(isinstance(screen, PermissionScreen) for screen in app.screen_stack)
