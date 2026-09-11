"""Live observations retain ownership without inventing progress or hang verdicts."""

import asyncio
from types import SimpleNamespace

import pytest

from harness.activity import ActivityTracker, activity_summary, current_activity, render_activity, waiting
from harness.agent import AgentTask, TaskLimits
from harness.cli import build_kernel
from harness.hooks import Ask, ProposedToolCall
from harness.log import read_session
from harness.provider import StreamStop, TextDelta, text_turn
from harness.session import Session
from harness.tools import ToolSpec
from tests.test_inference import make_dispatcher
from tests.test_local_scheduling import models, ready_resources, until


def test_monotonic_observation_is_not_a_hang_policy_and_late_callbacks_are_inert():
    now = [10.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    with tracker.track(session_id="root", kind="task", label="harness", phase="inference",
                       task_id="task", run_id="run", timeout=100) as root:
        with tracker.track(session_id="child", kind="model", label="local", phase="inference",
                           timeout=200) as child:
            now[0] += 90
            assert tracker.snapshot()[1].remaining_seconds == 10  # enclosing cap
            assert tracker.snapshot()[0].quiet_seconds == 90
            assert "Silence does not establish a hang" in render_activity(tracker)
            assert len(tracker.snapshot()) == 2
            child.touch("stream event", stream=True)
            root_row, child_row = tracker.snapshot()
            assert root_row.quiet_seconds == 0 and root_row.last_signal == "child activity"
            assert child_row.stream_events == 1 and child_row.parent_id == root.id
            assert child_row.task_id == "task" and child_row.run_id == "run"
            now[0] += 20
            assert "cleanup still active" in render_activity(tracker)
        assert len(tracker.snapshot()) == 1
    child.touch("late stream event", stream=True)
    assert tracker.snapshot() == () and current_activity.get() is None
    assert "Saved runs do not establish live activity" in render_activity(tracker)
    assert activity_summary(tracker) == ""


async def test_sibling_wait_survives_activity_and_other_trees_do_not_inherit_ownership():
    tracker = ActivityTracker()
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait():
        with waiting("permission"):
            entered.set()
            await release.wait()
    with tracker.track(session_id="root", kind="task", label="harness", phase="tools") as root:
        work = asyncio.create_task(wait())
        await entered.wait()
        try:
            with tracker.track(session_id="root", kind="tool", label="parallel", phase="execution") as tool:
                tool.touch("stream event", stream=True)
                rows = tracker.snapshot()
                assert [row.label for row in rows if row.kind == "wait"] == ["permission"]
                assert all(row.parent_id == root.id for row in rows[1:])
                other = ActivityTracker()
                with other.track(session_id="other", kind="task", label="harness", phase="preparing"):
                    assert other.snapshot()[0].parent_id is None
        finally:
            release.set()
            await work
        assert len(tracker.snapshot()) == 1
    assert tracker.snapshot() == ()


@pytest.mark.parametrize("exit_kind", ["completed", "cancelled", "deadline", "failed"])
async def test_native_run_stream_activity_deadline_and_cleanup(tmp_path, exit_kind):
    entered, release, cleaning, cleaned = (asyncio.Event() for _ in range(4))
    cleanup_release = asyncio.Event()
    class Provider:
        async def infer(self, request):
            try:
                yield TextDelta("secret partial output")
                entered.set()
                await release.wait()
                if exit_kind == "failed":
                    raise RuntimeError("provider failure")
                yield StreamStop("end_turn")
            finally:
                cleaning.set()
                await cleanup_release.wait()
                cleaned.set()
    kernel = build_kernel(base_dir=tmp_path, model="fake", provider=Provider())
    await kernel.loop.start()
    tracker = kernel.loop.dispatcher.scope.budget.activity
    task = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="secret user prompt",
        limits=TaskLimits(timeout_seconds=.2 if exit_kind == "deadline" else 10))))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rows = tracker.snapshot()
        assert [r.kind for r in rows] == ["task", "model"]
        assert rows[1].stream_events == 1 and rows[1].call_id
        assert rows[1].run_id == rows[0].run_id
        assert rows[1].remaining_seconds <= rows[0].remaining_seconds
        assert "secret" not in render_activity(tracker)
        if exit_kind == "cancelled":
            task.cancel()
        elif exit_kind != "deadline":
            release.set()
        await asyncio.wait_for(cleaning.wait(), 2)
        assert tracker.snapshot() and not task.done()  # cleanup remains visible
        cleanup_release.set()
        if exit_kind == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_kind == "deadline":
            with pytest.raises(TimeoutError):
                await task
        elif exit_kind == "failed":
            with pytest.raises(RuntimeError):
                await task
        else:
            assert (await task).status == "completed"
        assert cleaned.is_set() and not tracker.snapshot()
        # A new process starts empty; replay cannot resurrect live observations.
        sid = kernel.session.id
        kernel.session.close()
        resumed = build_kernel(base_dir=tmp_path, model="fake", provider=Provider(), resume_session_id=sid)
        try:
            assert resumed.loop.dispatcher.scope.budget.activity.snapshot() == ()
        finally:
            resumed.session.close()
    finally:
        release.set()
        cleanup_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        kernel.session.close()


async def test_real_permission_and_tool_wait_are_attributed_to_dispatch(tmp_path):
    entered, approval, tool_entered, release = (asyncio.Event() for _ in range(4))
    class Resolver:
        name = "test"
        async def resolve(self, request):
            entered.set()
            await approval.wait()
            return True
    class Tool:
        spec = ToolSpec(name="wait_tool", description="", parameters={})
        async def __call__(self, args):
            tool_entered.set()
            await release.wait()
            return "done"
    with Session(tmp_path, "root") as session:
        session.start()
        dispatcher = make_dispatcher(session)
        dispatcher.registry.register(Tool())
        dispatcher.hooks.register_dispatch("ask", lambda _: Ask(reason="secret reason"))
        dispatcher.resolver = Resolver()
        tracker = dispatcher.scope.budget.activity
        task = asyncio.create_task(dispatcher.dispatch_tool(ProposedToolCall("call", "wait_tool", {})))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            tool, permission = tracker.snapshot()
            assert permission.label == "permission" and permission.parent_id == tool.id
            assert permission.call_id == tool.call_id == "call"
            assert "secret reason" not in render_activity(tracker)
            approval.set()
            await asyncio.wait_for(tool_entered.wait(), 2)
            assert [r.kind for r in tracker.snapshot()] == ["tool"]
            release.set()
            assert not (await task).is_error
            assert tracker.snapshot() == ()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_local_capacity_and_readiness_waits_are_observed_and_cleared():
    resources, catalog = ready_resources(), models()
    tracker = ActivityTracker()
    entered, release, probe_entered, probe_release = (asyncio.Event() for _ in range(4))
    ready = resources._ready
    async def gated_ready(resolved, *args, **kwargs):
        if resolved.alias == "b":
            probe_entered.set()
            await probe_release.wait()
        return await ready(resolved, *args, **kwargs)
    resources._ready = gated_ready
    async def use(alias):
        with tracker.track(session_id=alias, kind="model", label=alias, phase="inference"):
            async with resources.use(catalog.resolve(alias), emit=lambda _: None):
                if alias == "a":
                    entered.set()
                    await release.wait()
    owner = asyncio.create_task(use("a"))
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter = asyncio.create_task(use("b"))
        await until(lambda: resources.scheduler.activity("local")[1] == 1)
        wait_row, = [r for r in tracker.snapshot() if r.kind == "wait"]
        assert wait_row.label == "local capacity" and wait_row.session_id == "b"
        release.set()
        await asyncio.wait_for(probe_entered.wait(), 2)
        wait_row, = [r for r in tracker.snapshot() if r.kind == "wait"]
        assert wait_row.label == "local readiness"
        waiter.cancel()
        await asyncio.gather(owner, waiter, return_exceptions=True)
        assert tracker.snapshot() == ()
    finally:
        release.set()
        probe_release.set()
        for task in (owner, waiter):
            if task is not None:
                task.cancel()
        await asyncio.gather(*[t for t in (owner, waiter) if t is not None], return_exceptions=True)
        await resources.close(emit=lambda _: None)


async def test_external_task_has_separate_runtime_owner(tmp_path):
    from tests.test_external_agent_runtime import ScriptedCodex
    provider = ScriptedCodex(hang=True)
    kernel = build_kernel(base_dir=tmp_path, model="codex/default", provider=provider)
    await kernel.loop.start()
    task = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work")))
    tracker = kernel.loop.dispatcher.scope.budget.activity
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        root, external, call = tracker.snapshot()
        assert root.label == "harness" and external.label == "codex"
        assert external.parent_id == root.id and call.parent_id == external.id
        assert external.run_id != root.run_id and call.run_id == external.run_id
        assert call.phase == "agent execution"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        kernel.session.close()
    assert provider.closed and tracker.snapshot() == ()


async def test_retry_wait_keeps_the_original_call_budget_and_clears_on_cancel(tmp_path):
    from harness.errors import Overloaded
    class Provider:
        async def complete(self, **kwargs):
            raise Overloaded("busy")
            yield  # async generator protocol
    with Session(tmp_path, "root") as session:
        session.start()
        dispatcher = make_dispatcher(session)
        dispatcher.retry_delays = (10,)
        tracker = dispatcher.scope.budget.activity
        task = asyncio.create_task(dispatcher.dispatch_model(provider=Provider(), model="fake", messages=[], tools=()))
        try:
            await until(lambda: any(row.label == "retry delay" for row in tracker.snapshot()))
            model, wait = tracker.snapshot()
            assert wait.parent_id == model.id and wait.call_id == model.call_id
            assert wait.remaining_seconds == model.remaining_seconds
            assert wait.phase == "waiting"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert tracker.snapshot() == ()


async def test_coordinator_children_share_inspection_and_settle_on_cancel(tmp_path):
    from harness.mixture import Expert, run_strategy_result
    from tests.test_coordination_admission import setup_runner
    entered = asyncio.Event()
    seen = []
    class Provider:
        async def complete(self, **kwargs):
            seen.append(True)
            if len(seen) == 2:
                entered.set()
            await asyncio.Event().wait()
            for chunk in text_turn("done"):
                yield chunk
    with Session(tmp_path, "root") as parent:
        parent.start()
        runner, budget = setup_runner(parent, Provider())
        task = asyncio.create_task(run_strategy_result("ensemble", runner, parent, "work", [Expert("a"), Expert("b")]))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            rows = budget.activity.snapshot()
            coordinator, = [r for r in rows if r.kind == "coordinator"]
            children = [r for r in rows if r.kind == "task"]
            assert len(children) == 2 and children[0].session_id != children[1].session_id
            assert all(r.parent_id == coordinator.id for r in children)
            assert all(r.remaining_seconds <= coordinator.remaining_seconds for r in children)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert budget.activity.snapshot() == () and not budget.busy


@pytest.mark.parametrize("width", [60, 120])
async def test_tui_inspection_preserves_draft_queue_and_logs_while_provider_is_silent(tmp_path, width):
    from textual.widgets import Input, Static
    from harness.tui_support import SlashCommand
    from tests.test_tui import GatedProvider, make_app
    from tests.test_tui_queue import screen_text
    from tests.test_tui_tasks import command
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider)
    async with app.run_test(size=(width, 42)) as pilot:
        await command(app, pilot, "work")
        await command(app, pilot, "next")
        composer = app.query_one("#prompt", Input)
        composer.value = "unsent draft"
        await pilot.pause(1.1)  # the timer advances during a silent provider wait
        assert "elapsed" in screen_text(app) and "activity" in screen_text(app)
        assert "/activity" in screen_text(app) and "unsent draft" in screen_text(app)
        assert "#2 next" in screen_text(app)
        before = read_session(tmp_path, app.kernel.session.id)
        app._plugin_commands["activity"] = SimpleNamespace(body="must not invoke a model")
        await app._run_command(SlashCommand("activity", ""))
        assert composer.value == "unsent draft" and len(app.controller.pending) == 1
        assert read_session(tmp_path, app.kernel.session.id) == before
        await command(app, pilot, "/activity")  # actual command route beats a plugin with this name
        assert len(app.controller.pending) == 1
        assert read_session(tmp_path, app.kernel.session.id) == before
        composer.value = "unsent draft"
        await pilot.press("escape")
        await pilot.pause(.2)
        assert not app.kernel.loop.dispatcher.scope.budget.activity.snapshot()
        assert not app.query_one("#activity-status", Static).display
        assert composer.value == "unsent draft"
        await app._run_command(SlashCommand("activity", ""))
        await pilot.pause(.1)
        assert "No active core operations" in screen_text(app)
