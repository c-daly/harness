"""An operator can extend a live root timer without widening other authority."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.cli import build_kernel
from harness.events import Envelope, TaskBudgetExtended, parse_envelope_line
from harness.execution import current_scope
from harness.execution_controls import render_execution
from harness.fold import fold
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.run_budgets import extend_execution, extension_block
from harness.tools import ToolSpec


class HoldTool:
    spec = ToolSpec(name="hold", description="Wait for the test to release work.", parameters={})

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, args):
        self.entered.set()
        await self.release.wait()
        return "ready"


@asynccontextmanager
async def parked(base, *, timeout=10, task=None):
    provider = FakeProvider([tool_call_turn("hold-call", "hold", {}), text_turn("done"), text_turn("next")])
    kernel = build_kernel(base_dir=base, model="fake", provider=provider,
                          execution_overrides={"task_timeout_seconds": timeout})
    tool = HoldTool()
    kernel.registry.register(tool)
    await kernel.loop.start()
    if task is None:
        kernel.tasks.create("Finish held work")
        task = kernel.tasks.prepare("work")
    work = asyncio.create_task(kernel.loop.run_task(task))
    try:
        await asyncio.wait_for(tool.entered.wait(), 3)
        yield kernel, tool, work
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        kernel.session.close()


def live(kernel):
    row, = kernel.loop.dispatcher.scope.budget.runs.snapshot(kernel.session.id)
    return row


def grants(kernel):
    return [e for e in read_session(kernel.session.base, kernel.session.id)
            if isinstance(e.event, TaskBudgetExtended)]


async def test_extension_keeps_tool_work_alive_updates_observation_and_exports_audit(tmp_path):
    from harness.portable import task_package
    async with parked(tmp_path, timeout=1) as (kernel, tool, work):
        before = live(kernel)
        activity = kernel.loop.dispatcher.scope.budget.activity
        signal = activity.snapshot()[0].last_signal
        scope = kernel.loop.dispatcher.scope
        counts = (scope.budget.model_calls, scope.budget.tool_calls, scope.budget.children)
        first = extend_execution(kernel, before.run_id[:8], .5)
        second = extend_execution(kernel, before.run_id, .5)
        assert first.timeout_seconds == 1.5 and second.timeout_seconds == 2
        assert second.remaining_seconds > before.remaining_seconds + .8
        assert activity.snapshot()[0].last_signal == signal
        assert activity.snapshot()[0].remaining_seconds > before.remaining_seconds + .8
        assert counts == (scope.budget.model_calls, scope.budget.tool_calls, scope.budget.children)
        assert scope.budget.limits.task_timeout_seconds == 1
        await asyncio.sleep(1.05)  # cross the original deadline with actual tool work still owned
        assert not work.done()
        tool.release.set()
        result = await work
        assert result.status == "completed" and result.acceptance == "unverified"
        assert [(e.event.previous_timeout_seconds, e.event.timeout_seconds) for e in grants(kernel)] == [(1, 1.5), (1.5, 2)]
        assert all(e.event.is_intent for e in grants(kernel))
        package, _ = task_package(tmp_path, kernel.session.id)
        exported = package["runs"][0]["budget_extensions"]
        assert [e["timeout_seconds"] for e in exported] == [1.5, 2]
        assert [e["source_seq"] for e in exported] == [e.seq for e in grants(kernel)]
        assert not scope.budget.runs.snapshot(kernel.session.id)
        with pytest.raises(ValueError, match="live run"):
            extend_execution(kernel, before.run_id, 10)


@pytest.mark.parametrize("seconds", [0, -1, True, "3", float("nan"), float("inf"), -float("inf"), 10**400])
async def test_invalid_grants_do_not_change_timer_or_log(tmp_path, seconds):
    async with parked(tmp_path) as (kernel, _, _):
        row = live(kernel)
        before = read_session(tmp_path, kernel.session.id)
        with pytest.raises(ValueError, match="positive and finite"):
            extend_execution(kernel, row.run_id, seconds)
        assert live(kernel).timeout_seconds == row.timeout_seconds
        assert read_session(tmp_path, kernel.session.id) == before


@pytest.mark.parametrize("mode", ["explicit", "replayed"])
async def test_explicit_and_replayed_caps_are_not_extendible(tmp_path, mode):
    task = AgentTask(prompt="work", limits=TaskLimits(timeout_seconds=10))
    if mode == "replayed":
        task = AgentTask.model_validate(AgentTask(prompt="work").model_dump())
    async with parked(tmp_path, task=task) as (kernel, _, _):
        row = live(kernel)
        assert "explicit or replayed" in row.extension_blocked
        with pytest.raises(ValueError, match="explicit or replayed"):
            extend_execution(kernel, row.run_id, 10)
        assert not grants(kernel)


async def test_foreign_ambiguous_and_stale_ids_and_delegated_actors_are_rejected(tmp_path):
    async with parked(tmp_path / "one") as (kernel, _, _):
        async with parked(tmp_path / "two") as (other, _, _):
            row = live(kernel)
            with pytest.raises(ValueError, match="live run"):
                extend_execution(other, row.run_id, 1)
            for identity in (row.run_id[:7], "not-a-run-id"):
                with pytest.raises(ValueError, match="run ID"):
                    extend_execution(kernel, identity, 1)
            with pytest.raises(ValueError, match="live run"):
                extend_execution(kernel, "f" * 32, 1)
            scope = kernel.loop.dispatcher.scope
            token = current_scope.set(replace(scope, depth=1))
            try:
                with pytest.raises(ValueError, match="root operator"):
                    extend_execution(kernel, row.run_id, 1)
            finally:
                current_scope.reset(token)
            assert not grants(kernel) and not grants(other)


async def test_failed_durable_grant_does_not_move_deadline(tmp_path, monkeypatch):
    async with parked(tmp_path, timeout=.3) as (kernel, _, work):
        row = live(kernel)
        append = kernel.session.append
        def fail(event):
            if isinstance(event, TaskBudgetExtended):
                raise OSError("cannot persist operator grant")
            return append(event)
        monkeypatch.setattr(kernel.session, "append", fail)
        with pytest.raises(OSError, match="persist"):
            extend_execution(kernel, row.run_id, 10)
        assert live(kernel).timeout_seconds == .3
        with pytest.raises(TimeoutError, match="0.3s"):
            await work
        assert not grants(kernel)


async def test_extended_timer_still_expires_with_updated_duration(tmp_path):
    async with parked(tmp_path, timeout=.3) as (kernel, _, work):
        extend_execution(kernel, live(kernel).run_id, .3)
        with pytest.raises(TimeoutError, match="0.6s"):
            await work
        result, = fold(read_session(tmp_path, kernel.session.id)).agent_runs.values()
        assert result.status == "incomplete" and result.reason == "deadline"
        assert not kernel.loop.dispatcher.scope.budget.runs.snapshot(kernel.session.id)


async def test_cancelled_run_cannot_be_revived_while_cleanup_is_pending(tmp_path):
    from harness.provider import TextDelta
    cleaning, release, entered = (asyncio.Event() for _ in range(3))
    class Provider:
        async def infer(self, request):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield TextDelta("unreachable")
            finally:
                cleaning.set()
                await release.wait()
    kernel = build_kernel(base_dir=tmp_path, provider=Provider(), model="fake")
    await kernel.loop.start()
    work = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work")))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        row = live(kernel)
        work.cancel()
        with pytest.raises(ValueError, match="cancelling"):
            extend_execution(kernel, row.run_id, 10)
        await asyncio.wait_for(cleaning.wait(), 3)
        with pytest.raises(ValueError, match="cancelling"):
            extend_execution(kernel, row.run_id, 10)
        assert not grants(kernel)
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
        kernel.session.close()
    assert not kernel.loop.dispatcher.scope.budget.runs.snapshot(kernel.session.id)


async def test_extension_does_not_widen_native_request_deadline(tmp_path):
    from harness.provider import TextDelta
    entered = asyncio.Event()
    class Provider:
        async def infer(self, request):
            entered.set()
            await asyncio.Event().wait()
            yield TextDelta("unreachable")
    kernel = build_kernel(base_dir=tmp_path, provider=Provider(), model="fake",
        execution_overrides={"task_timeout_seconds": 1, "inference_timeout_seconds": .2})
    await kernel.loop.start()
    work = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work")))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        extend_execution(kernel, live(kernel).run_id, 10)
        root, call = kernel.loop.dispatcher.scope.budget.activity.snapshot()
        assert root.remaining_seconds > 10 and call.remaining_seconds < .3
        with pytest.raises(TimeoutError):
            await work
        result, = fold(read_session(tmp_path, kernel.session.id)).agent_runs.values()
        assert result.reason == "timeout"
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        kernel.session.close()


async def test_grants_do_not_become_resume_defaults_or_live_timers(tmp_path):
    async with parked(tmp_path) as (kernel, tool, work):
        extend_execution(kernel, live(kernel).run_id, 300)
        tool.release.set()
        await work
        sid = kernel.session.id
    resumed = build_kernel(base_dir=tmp_path, provider=FakeProvider([]), model="fake", resume_session_id=sid)
    try:
        scope = resumed.loop.dispatcher.scope
        assert scope.budget.limits.task_timeout_seconds == 10
        assert not scope.budget.runs.snapshot(sid)
        assert len(grants(resumed)) == 1
        assert "No live task timers" in render_execution(scope)
    finally:
        resumed.session.close()


def test_handoff_and_child_provenance_refuse_extensions_before_task_defaults_materialize():
    scope = SimpleNamespace(depth=0)
    task = AgentTask(prompt="work")
    assert extension_block(task, scope) is None
    assert "delegated" in extension_block(task, SimpleNamespace(depth=1))
    assert "handoff" in extension_block(task.model_copy(update={"handoff_id": "a" * 32}), scope)
    from harness.handoff import current_handoff
    token = current_handoff.set(object())
    try:
        assert "handoff" in extension_block(task, scope)
    finally:
        current_handoff.reset(token)


@pytest.mark.parametrize("total", [0, 1, True, "2", float("nan"), float("inf")])
def test_grant_event_rejects_invalid_or_non_increasing_values(total):
    with pytest.raises(ValueError):
        TaskBudgetExtended(run_id="a" * 32, task_id="task", previous_timeout_seconds=1, timeout_seconds=total)


def test_grant_event_round_trip_preserves_audit():
    event = TaskBudgetExtended(run_id="a" * 32, task_id="task", previous_timeout_seconds=1, timeout_seconds=2)
    envelope = Envelope(session_id="source", seq=2, ts=0, event=event)
    assert parse_envelope_line(envelope.model_dump_json()).event == event


async def test_elapsed_budget_cannot_be_revived_before_timeout_callback_runs(tmp_path):
    import time
    async with parked(tmp_path, timeout=.15) as (kernel, _, work):
        row = live(kernel)
        time.sleep(.2)  # deliberately overdue on the same loop, before cancellation is delivered
        with pytest.raises(ValueError, match="already elapsed"):
            extend_execution(kernel, row.run_id, 10)
        assert not grants(kernel)
        with pytest.raises(TimeoutError):
            await work


@pytest.mark.parametrize("colliding", [False, True])
async def test_external_runtime_budget_stays_fixed_and_ambiguous_prefix_is_refused(tmp_path, monkeypatch, colliding):
    from itertools import count
    from tests.test_external_agent_runtime import ScriptedCodex
    if colliding:
        serial = count()
        monkeypatch.setattr("harness.agent.uuid4", lambda: SimpleNamespace(hex=f"deadbeef{next(serial):024x}"))
    provider = ScriptedCodex(hang=True)
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model="codex/default")
    await kernel.loop.start()
    work = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work")))
    try:
        await asyncio.wait_for(provider.entered.wait(), 3)
        scope = kernel.loop.dispatcher.scope
        root, external = scope.budget.runs.snapshot(kernel.session.id)
        assert root.extension_blocked is None
        assert "external runtime" in external.extension_blocked
        if colliding:
            with pytest.raises(ValueError, match="one live run"):
                extend_execution(kernel, root.run_id[:8], 10)
        with pytest.raises(ValueError, match="external runtime"):
            extend_execution(kernel, external.run_id, 10)
        extend_execution(kernel, root.run_id, 100)
        root_activity, external_activity, model_activity = scope.budget.activity.snapshot()
        assert root_activity.remaining_seconds > 690
        assert external_activity.remaining_seconds < 600 and model_activity.remaining_seconds < 600
        assert not work.done()
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        kernel.session.close()
    assert provider.closed and not scope.budget.runs.snapshot(kernel.session.id)


async def test_tui_extension_is_core_usable_while_busy_and_preserves_draft_queue(tmp_path):
    from textual.widgets import Input
    from harness.tui_support import SlashCommand
    from tests.test_tui import make_app
    from tests.test_tui_tasks import command
    from tests.test_tui_queue import screen_text
    provider = FakeProvider([tool_call_turn("hold-call", "hold", {}), text_turn("done")])
    app = make_app(tmp_path, provider=provider)
    tool = HoldTool()
    app.kernel.registry.register(tool)
    async with app.run_test(size=(140, 48)) as pilot:
        await command(app, pilot, "work")
        await asyncio.wait_for(tool.entered.wait(), 3)
        await command(app, pilot, "next")
        row = live(app.kernel)
        composer = app.query_one("#prompt", Input)
        composer.value = "unsent draft"
        app._plugin_commands["execution"] = SimpleNamespace(body="must not invoke a model")
        await app._run_command(SlashCommand("execution", f"extend {row.run_id[:8]} 100"))
        await pilot.pause(.1)
        assert "budget extended to 700s total" in screen_text(app)
        assert "keep their own caps" in " ".join(screen_text(app).split())
        assert composer.value == "unsent draft" and len(app.controller.pending) == 1
        assert len(provider.calls) == 1
        assert app.kernel.loop.dispatcher.scope.budget.limits.task_timeout_seconds == 600
        await command(app, pilot, f"/execution extend {row.run_id[:8]} 100")
        assert live(app.kernel).timeout_seconds == 800 and len(app.controller.pending) == 1
        assert len(provider.calls) == 1 and len(grants(app.kernel)) == 2
        await pilot.press("escape")
        await pilot.pause(.2)
        before = read_session(tmp_path, app.kernel.session.id)
        await app._run_command(SlashCommand("execution", f"extend {row.run_id[:8]} 100"))
        assert read_session(tmp_path, app.kernel.session.id) == before
        assert not app.kernel.loop.dispatcher.scope.budget.runs.snapshot(app.kernel.session.id)
