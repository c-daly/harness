"""Operator grants move one coordinator's timer without widening its members."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from harness.agent import current_agent_run
from harness.cli import build_kernel
from harness.coordination_budgets import extend_coordination
from harness.coordination_cli import render_coordination
from harness.events import CoordinationBudgetExtended, CoordinationFinished
from harness.execution import current_scope
from harness.execution_controls import render_execution
from harness.handoff import current_handoff
from harness.log import read_session
from harness.mixture import Expert, run_strategy_result
from harness.portable import task_package
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.run_budgets import extend_execution
from harness.run_controls import cancel_run
from harness.tui_support import SlashCommand
from tests.test_coordination_results import saved_report
from tests.test_run_controls import HeldProvider


class TeamProvider(HeldProvider):
    def __init__(self, *, cleanup=False, nested=False):
        super().__init__("a", "b", cleanup=cleanup)
        self.nested = nested
        self.turns = {}

    async def infer(self, request):
        alias = request.model
        self.turns[alias] = self.turns.get(alias, 0) + 1
        if alias in {"root", "branch"}:
            if self.turns[alias] == 1:
                models = (["branch", "b"] if self.nested else ["a", "b"]) if alias == "root" else ["a"]
                script = tool_call_turn(f"{alias}-team", "ensemble", {"prompt": "work", "models": models})
            else:
                script = text_turn("finished")
            for chunk in script:
                yield chunk
        else:
            async for chunk in super().infer(request):
                yield chunk


def budget(kernel):
    return kernel.loop.dispatcher.scope.budget


def rows(kernel):
    return budget(kernel).coordinations.snapshot()


def grants(kernel):
    return [e for e in read_session(kernel.session.base, kernel.session.id)
            if isinstance(e.event, CoordinationBudgetExtended)]


@asynccontextmanager
async def parked(base, *, seconds=10, provider=None, overrides=None):
    provider = provider or TeamProvider()
    kernel = build_kernel(base_dir=base, model="root", provider=provider,
        execution_overrides={"coordination_timeout_seconds": seconds,
                             "task_timeout_seconds": 10, "inference_timeout_seconds": 10, **(overrides or {})})
    await kernel.loop.start()
    kernel.tasks.create("Keep the team's useful work")
    kernel.tasks.add_requirement({"id": "review", "description": "Inspect work", "check": {"kind": "review"}})
    work = asyncio.create_task(kernel.loop.run_task(kernel.tasks.prepare("work")))
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in provider.entered.values())), 3)
        yield SimpleNamespace(kernel=kernel, provider=provider, work=work)
    finally:
        provider.unblock()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        kernel.session.close()


async def test_grants_keep_real_members_alive_and_export_recorded_time(tmp_path):
    async with parked(tmp_path, seconds=1) as case:
        kernel, work = case.kernel, case.work
        before, = rows(kernel)
        counts = (budget(kernel).model_calls, budget(kernel).tool_calls, budget(kernel).children)
        activity = next(r for r in budget(kernel).activity.snapshot() if r.kind == "coordinator")
        limits = budget(kernel).limits
        one = extend_coordination(kernel, before.id[:8], .5)
        two = extend_coordination(kernel, before.id, .5)
        assert (one.timeout_seconds, two.timeout_seconds) == (1.5, 2)
        assert two.remaining_seconds > before.remaining_seconds + .8
        after = next(r for r in budget(kernel).activity.snapshot() if r.id == activity.id)
        assert after.last_signal == activity.last_signal and after.stream_events == activity.stream_events
        assert after.remaining_seconds > activity.remaining_seconds + .8
        assert counts == (budget(kernel).model_calls, budget(kernel).tool_calls, budget(kernel).children)
        assert budget(kernel).limits == limits
        assert f"/execution extend-coordinator {before.id} 300" in render_execution(kernel.loop.dispatcher.scope)
        await asyncio.sleep(1.05)
        assert not work.done() and budget(kernel).active_children == 2
        case.provider.unblock()
        result = await asyncio.wait_for(work, 3)
        assert result.status == "completed" and result.acceptance == "unverified"
        assert result.remaining_criteria
        report = saved_report(kernel.session)
        assert report.timeout_seconds == 2 and report.admitted
        assert all(member.result.status == "completed" for member in report.members)
        assert not rows(kernel) and not budget(kernel).busy
        recorded = grants(kernel)
        assert [(e.event.previous_timeout_seconds, e.event.timeout_seconds) for e in recorded] == [(1, 1.5), (1.5, 2)]
        assert all(e.event.is_intent for e in recorded)
        exported, _ = task_package(tmp_path, kernel.session.id)
        assert exported["coordination"][0]["timeout_seconds"] == 2
        assert exported["coordination_budget_extensions"] == [
            {"source_seq": e.seq, **e.event.model_dump(exclude={"type"})} for e in recorded]
        assert "Operator grant" in render_coordination(tmp_path, kernel.session.id)
        assert "deadline 2s" in render_coordination(tmp_path, kernel.session.id)
        with pytest.raises(ValueError, match="one live coordinator"):
            extend_coordination(kernel, before.id, 5)


@pytest.mark.parametrize("seconds", [0, -1, True, "3", float("nan"), float("inf"), -float("inf"), 10**400])
async def test_invalid_grants_leave_timer_and_log_unchanged(tmp_path, seconds):
    async with parked(tmp_path) as case:
        row, = rows(case.kernel)
        before = read_session(tmp_path, case.kernel.session.id)
        with pytest.raises(ValueError, match="positive and finite"):
            extend_coordination(case.kernel, row.id, seconds)
        assert rows(case.kernel)[0].timeout_seconds == row.timeout_seconds
        assert read_session(tmp_path, case.kernel.session.id) == before


@pytest.mark.parametrize("seconds", [1e-320, 1e308])
async def test_unrepresentable_deadline_increase_is_refused(tmp_path, seconds):
    async with parked(tmp_path) as case:
        row, = rows(case.kernel)
        if seconds > 1:
            active = budget(case.kernel).coordinations._coordinations[row.id]
            active.timeout_seconds = 1e308
        with pytest.raises(ValueError, match="larger finite"):
            extend_coordination(case.kernel, row.id, seconds)
        assert not grants(case.kernel)


@pytest.mark.parametrize("written", [False, True])
async def test_failed_grant_does_not_move_deadline_or_replay_after_restart(tmp_path, monkeypatch, written):
    async with parked(tmp_path, seconds=.3) as case:
        kernel = case.kernel
        row, = rows(kernel)
        append = kernel.session.append

        def fail(event):
            if isinstance(event, CoordinationBudgetExtended):
                if written:
                    append(event)
                raise OSError("failed grant persistence")
            return append(event)

        with monkeypatch.context() as patch:
            patch.setattr(kernel.session, "append", fail)
            with pytest.raises(OSError, match="persistence"):
                extend_coordination(kernel, row.id, 10)
        assert rows(kernel)[0].timeout_seconds == .3
        await asyncio.wait_for(case.work, 3)
        assert saved_report(kernel.session).result.reason == "coordination deadline"
        assert saved_report(kernel.session).timeout_seconds == .3
        sid = kernel.session.id
    resumed = build_kernel(base_dir=tmp_path, model="root", provider=FakeProvider([]), resume_session_id=sid)
    try:
        assert not rows(resumed)
        assert budget(resumed).limits.coordination_timeout_seconds == .3
        assert len(grants(resumed)) == int(written)
        with pytest.raises(ValueError, match="one live coordinator"):
            extend_coordination(resumed, row.id, 10)
    finally:
        resumed.session.close()


async def test_rewritten_grant_is_not_applied(tmp_path, monkeypatch):
    async with parked(tmp_path) as case:
        row, = rows(case.kernel)
        append = case.kernel.session.append

        def rewrite(event):
            if isinstance(event, CoordinationBudgetExtended):
                event = event.model_copy(update={"timeout_seconds": 99.0})
            return append(event)

        monkeypatch.setattr(case.kernel.session, "append", rewrite)
        with pytest.raises(ValueError, match="rewritten"):
            extend_coordination(case.kernel, row.id, 5)
        assert rows(case.kernel)[0].timeout_seconds == row.timeout_seconds


async def test_root_ownership_ids_and_model_contexts_are_enforced(tmp_path):
    async with parked(tmp_path / "one") as case, parked(tmp_path / "two") as other:
        kernel = case.kernel
        row, = rows(kernel)
        with pytest.raises(ValueError, match="one live coordinator"):
            extend_coordination(other.kernel, row.id, 5)
        with pytest.raises(ValueError, match="owning root"):
            budget(kernel).coordinations.extend(other.kernel.session, row.id, 5)
        for invalid in (row.id[:7], "not-an-id", None):
            with pytest.raises(ValueError, match="coordinator ID"):
                extend_coordination(kernel, invalid, 5)
        for variable, value in ((current_scope, kernel.loop.dispatcher.scope),
                                (current_agent_run, object())):
            token = variable.set(value)
            try:
                with pytest.raises(ValueError, match="root operator"):
                    extend_coordination(kernel, row.id, 5)
            finally:
                variable.reset(token)
        child_kernel = SimpleNamespace(session=kernel.session, loop=SimpleNamespace(dispatcher=SimpleNamespace(
            scope=replace(kernel.loop.dispatcher.scope, depth=1))))
        with pytest.raises(ValueError, match="root operator"):
            extend_coordination(child_kernel, row.id, 5)
        async def wrong_loop():
            extend_coordination(kernel, row.id, 5)
        with pytest.raises(ValueError, match="owning event loop"):
            await asyncio.to_thread(lambda: asyncio.run(wrong_loop()))
        assert not grants(kernel) and not grants(other.kernel)


@pytest.mark.parametrize("target", ["root", "coordinator", "expired"])
async def test_stopping_or_elapsed_coordinator_cannot_be_revived(tmp_path, target):
    async with parked(tmp_path, provider=TeamProvider(cleanup=True)) as case:
        row, = rows(case.kernel)
        active = budget(case.kernel).coordinations._coordinations[row.id]
        if target == "root":
            cancel_run(case.kernel, row.run_id)
        elif target == "coordinator":
            active.owner.cancel()
        else:
            active.timer.reschedule(asyncio.get_running_loop().time() - .01)
        with pytest.raises(ValueError, match="cancelling|elapsed"):
            extend_coordination(case.kernel, row.id, 20)
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in case.provider.cleaning.values())), 3)
        assert rows(case.kernel) and not case.work.done() and budget(case.kernel).busy
        with pytest.raises(ValueError, match="cancelling"):
            extend_coordination(case.kernel, row.id, 20)
        assert not grants(case.kernel)


async def test_new_deadline_still_expires_and_is_recorded(tmp_path):
    async with parked(tmp_path, seconds=.2) as case:
        row, = rows(case.kernel)
        extend_coordination(case.kernel, row.id, .2)
        await asyncio.wait_for(case.work, 3)
        report = saved_report(case.kernel.session)
        assert report.timeout_seconds == .4 and report.result.reason == "coordination deadline"
        assert not rows(case.kernel) and not budget(case.kernel).busy


async def test_root_timer_and_member_request_timers_remain_binding(tmp_path):
    async with parked(tmp_path / "root", overrides={"task_timeout_seconds": .3}) as case:
        row, = rows(case.kernel)
        extend_coordination(case.kernel, row.id, 5)
        with pytest.raises(TimeoutError, match="task time budget"):
            await asyncio.wait_for(case.work, 3)
        assert saved_report(case.kernel.session).result.status == "cancelled"
        assert not rows(case.kernel)
    async with parked(tmp_path / "request", overrides={"inference_timeout_seconds": .3}) as case:
        row, = rows(case.kernel)
        extend_coordination(case.kernel, row.id, 5)
        await asyncio.wait_for(case.work, 3)
        report = saved_report(case.kernel.session)
        assert report.result.status == "failed" and report.result.reason != "coordination deadline"
        assert all(m.result.status != "completed" for m in report.members)


async def test_root_extension_does_not_extend_coordinator(tmp_path):
    async with parked(tmp_path, seconds=.3) as case:
        row, = rows(case.kernel)
        extend_execution(case.kernel, row.run_id, 20)
        assert rows(case.kernel)[0].timeout_seconds == .3
        await asyncio.wait_for(case.work, 3)
        assert saved_report(case.kernel.session).result.reason == "coordination deadline"
        assert not grants(case.kernel)


async def test_nested_child_grant_keeps_outer_timer_and_exports_root_intent(tmp_path):
    async with parked(tmp_path, seconds=.5, provider=TeamProvider(nested=True)) as case:
        kernel = case.kernel
        outer = next(r for r in rows(kernel) if r.session_id == kernel.session.id)
        inner = next(r for r in rows(kernel) if r.session_id != kernel.session.id)
        extend_coordination(kernel, inner.id, 10)
        assert next(r for r in rows(kernel) if r.id == outer.id).timeout_seconds == .5
        await asyncio.wait_for(case.work, 3)
        assert saved_report(kernel.session).result.reason == "coordination deadline"
        assert grants(kernel)[0].event.target_session_id == inner.session_id
        child_events = read_session(tmp_path, inner.session_id)
        assert not any(isinstance(e.event, CoordinationBudgetExtended) for e in child_events)
        assert any(isinstance(e.event, CoordinationFinished) and e.event.status == "cancelled" for e in child_events)
        package, _ = task_package(tmp_path, kernel.session.id)
        assert package["coordination_budget_extensions"][0]["coordination_id"] == inner.id
        assert not rows(kernel) and not budget(kernel).busy


async def test_parent_stop_immediately_blocks_a_nested_coordinator_grant(tmp_path):
    async with parked(tmp_path, provider=TeamProvider(nested=True, cleanup=True)) as case:
        outer = next(r for r in rows(case.kernel) if r.session_id == case.kernel.session.id)
        inner = next(r for r in rows(case.kernel) if r.session_id != case.kernel.session.id)
        cancel_run(case.kernel, outer.run_id)
        # Do not yield: cancellation has not propagated to the child worker yet.
        with pytest.raises(ValueError, match="enclosing agent run"):
            extend_coordination(case.kernel, inner.id, 20)
        assert not grants(case.kernel)


async def test_parallel_grants_refuse_ambiguous_ids_and_leave_sibling_unchanged(tmp_path, monkeypatch):
    import harness.mixture
    identities = iter([SimpleNamespace(hex="12345678" + f"{i:024x}") for i in (1, 2)])
    monkeypatch.setattr(harness.mixture, "uuid4", lambda: next(identities))
    provider = HeldProvider("a", "b")
    kernel = build_kernel(base_dir=tmp_path, model="root", provider=provider)
    await kernel.loop.start()
    tasks = [asyncio.create_task(run_strategy_result("ensemble", kernel.runner, kernel.session,
        "work", [Expert(alias)])) for alias in ("a", "b")]
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in provider.entered.values())), 3)
        first, second = rows(kernel)
        with pytest.raises(ValueError, match="one live coordinator"):
            extend_coordination(kernel, first.id[:8], 5)
        assert not grants(kernel)
        extend_coordination(kernel, first.id, 5)
        assert next(r for r in rows(kernel) if r.id == second.id).timeout_seconds == second.timeout_seconds
        provider.release["a"].set()
        assert (await asyncio.wait_for(tasks[0], 3)).status == "completed"
        assert not tasks[1].done() and len(rows(kernel)) == 1
        with pytest.raises(ValueError, match="one live coordinator"):
            extend_coordination(kernel, first.id, 5)
    finally:
        provider.unblock()
        await asyncio.gather(*tasks, return_exceptions=True)
        kernel.session.close()


async def test_handoff_context_captures_fixed_coordinator_authority(tmp_path):
    from tests.test_mixture import FakeRunner
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingRunner(FakeRunner):
        async def run_result(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().run_result(**kwargs)

    kernel = build_kernel(base_dir=tmp_path, model="root", provider=FakeProvider([]))
    await kernel.loop.start()
    runner = WaitingRunner({"a": "answer"})
    runner._scopes[kernel.session.id] = kernel.loop.dispatcher.scope
    token = current_handoff.set(object())
    try:
        task = asyncio.create_task(run_strategy_result("ensemble", runner, kernel.session, "work", [Expert("a")]))
    finally:
        current_handoff.reset(token)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        row, = rows(kernel)
        assert "handoff" in row.extension_blocked
        with pytest.raises(ValueError, match="handoff"):
            extend_coordination(kernel, row.id, 20)
        assert not grants(kernel)
    finally:
        release.set()
        await task
        kernel.session.close()


async def test_exports_do_not_attach_grants_to_an_unrelated_task(tmp_path):
    async with parked(tmp_path) as case:
        kernel = case.kernel
        row, = rows(kernel)
        extend_coordination(kernel, row.id, 5)
        case.provider.unblock()
        await case.work
        first, _ = task_package(tmp_path, kernel.session.id)
        kernel.tasks.create("Unrelated next task")
        await kernel.loop.run_task(kernel.tasks.prepare("other work"))
        second, _ = task_package(tmp_path, kernel.session.id)
        assert first["coordination_budget_extensions"]
        assert not second["coordination_budget_extensions"] and not second["coordination"]


async def test_missing_terminal_keeps_grant_evidence_without_live_authority(tmp_path, monkeypatch):
    async with parked(tmp_path) as case:
        kernel = case.kernel
        row, = rows(kernel)
        extend_coordination(kernel, row.id, 5)
        append = kernel.session.append

        def fail(event):
            if isinstance(event, CoordinationFinished):
                raise OSError("cannot publish aggregate terminal")
            return append(event)

        monkeypatch.setattr(kernel.session, "append", fail)
        case.provider.unblock()
        await asyncio.wait_for(case.work, 3)
        assert not rows(kernel) and not budget(kernel).busy
        with pytest.raises(ValueError, match="one live coordinator"):
            extend_coordination(kernel, row.id, 5)
        package, _ = task_package(tmp_path, kernel.session.id)
        assert package["coordination"][0]["status"] == "unconfirmed"
        assert package["coordination"][0]["timeout_seconds"] == 10
        assert package["coordination_budget_extensions"][0]["timeout_seconds"] == 15
        assert "completion unconfirmed" in render_coordination(tmp_path, kernel.session.id)
        assert "recorded intent, not a live timer" in render_coordination(tmp_path, kernel.session.id)


@pytest.mark.parametrize("change", [{"timeout_seconds": 10.0}, {"timeout_seconds": float("inf")},
                                  {"previous_timeout_seconds": float("nan")}, {"actor": "model"}])
def test_grant_event_rejects_invalid_authority_or_time(change):
    with pytest.raises(ValueError):
        CoordinationBudgetExtended(**{"coordination_id": "a" * 32, "target_session_id": "root",
            "previous_timeout_seconds": 10.0, "timeout_seconds": 20.0, **change})


@pytest.mark.parametrize("width", [60, 120])
async def test_terminal_grant_preserves_draft_queue_and_protects_core_command(tmp_path, width):
    from textual.widgets import Input
    from tests.test_tui import make_app
    from tests.test_tui_tasks import command
    from tests.test_tui_queue import screen_text
    provider = TeamProvider()
    app = make_app(tmp_path, provider=provider, model="root")
    async with app.run_test(size=(width, 45)) as pilot:
        try:
            await command(app, pilot, "work")
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in provider.entered.values())), 3)
            await command(app, pilot, "next")
            prompt = app.query_one("#prompt", Input)
            prompt.value = "keep unsent draft"
            row, = rows(app.kernel)
            before = read_session(tmp_path, app.kernel.session.id)
            app._plugin_commands["execution"] = SimpleNamespace(body="must never execute")
            await app._run_command(SlashCommand("execution", ""))
            assert read_session(tmp_path, app.kernel.session.id) == before
            await app._run_command(SlashCommand("execution", f"extend-coordinator {row.id[:8]} 10"))
            await pilot.pause()
            assert rows(app.kernel)[0].timeout_seconds == row.timeout_seconds + 10
            assert len(grants(app.kernel)) == 1
            assert "budget extended" in " ".join(screen_text(app).split())
            assert prompt.value == "keep unsent draft" and len(app.controller.pending) == 1
            await app._run_command(SlashCommand("execution", "extend-coordinator"))
            await pilot.pause()
            assert len(grants(app.kernel)) == 1
            assert "Usage:" in " ".join(screen_text(app).split())
        finally:
            provider.unblock()
