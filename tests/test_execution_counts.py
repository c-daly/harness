"""Admission survives restart, cancellation, missing child logs and write failures."""

import asyncio
import json
import subprocess
import sys

import pytest

from harness.events import (ExecutionCountsLinked, ExecutionCountsRecorded, ModelCallStarted,
                            RetryAttempted, SessionResumed, SubagentSpawned, ToolCallProposed, UnknownEvent)
from harness.execution import BudgetExceeded, ExecutionBudget
from harness.execution_controls import configure_execution, render_execution
from harness.execution_counts import counts_snapshot, project_counts
from harness.fold import fold
from harness.hooks import ProposedToolCall
from harness.log import read_session
from harness.mixture import Expert, run_strategy_result
from harness.portable import task_package
from harness.provider import FakeProvider, text_turn
from harness.session import Session
from tests.test_usage_budget import dispatch, make_kernel


def budget(kernel):
    return kernel.loop.dispatcher.scope.budget


def totals(kernel):
    b = budget(kernel)
    return b.model_calls, b.tool_calls, b.children


async def unknown_tool(kernel, call_id="missing"):
    return await kernel.loop.dispatcher.dispatch_tool(ProposedToolCall(call_id=call_id, tool="missing", args={}))


async def test_resume_keeps_exact_admissions_and_operator_changes_do_not_refund(tmp_path):
    provider = FakeProvider([text_turn("first"), text_turn("second")])
    kernel = await make_kernel(tmp_path, provider, execution_overrides={"max_model_calls": 1, "max_tool_calls": 1})
    await dispatch(kernel)
    assert (await unknown_tool(kernel)).is_error  # Admitted, then refused by tool resolution.
    assert totals(kernel) == (1, 1, 0)
    sid = kernel.session.id
    kernel.session.close()
    kernel = await make_kernel(tmp_path, provider, resume_session_id=sid)
    try:
        with pytest.raises(BudgetExceeded, match="model_calls limit"):
            await dispatch(kernel)
        assert "tool_calls limit" in (await unknown_tool(kernel, "refused")).text
        assert totals(kernel) == (1, 1, 0) and len(provider.calls) == 1
        configure_execution(kernel, {"max_model_calls": 2})
        await dispatch(kernel)
        assert totals(kernel) == (2, 1, 0)
        assert counts_snapshot(tmp_path, sid)["model_calls"] == 2
        assert not project_counts(read_session(tmp_path, sid)).legacy
    finally:
        kernel.session.close()


async def test_root_owns_child_and_coordinator_counts_without_child_log_dependency(tmp_path):
    provider = FakeProvider([text_turn("answer") for _ in range(3)])
    kernel = await make_kernel(tmp_path, provider, execution_overrides={"max_children": 4, "max_model_calls": 3})
    result = await run_strategy_result("ensemble", kernel.runner, kernel.session, "work", [Expert("a"), Expert("b")])
    assert result.status == "completed" and totals(kernel) == (2, 0, 3)
    child = await kernel.runner.run_result(prompt="one more", model=None, parent=kernel.session)
    assert child.status == "completed" and totals(kernel) == (3, 0, 4)
    root, sid = read_session(tmp_path, kernel.session.id), kernel.session.id
    children = [e.event.child_session_id for e in root if isinstance(e.event, SubagentSpawned)]
    reservations = [e.event for e in root if isinstance(e.event, ExecutionCountsRecorded) and e.event.kind == "model"]
    assert {r.source_session_id for r in reservations} == set(children)
    for child_id in children:
        child_events = read_session(tmp_path, child_id)
        assert any(isinstance(e.event, ExecutionCountsLinked) and e.event.root_session_id == sid for e in child_events)
        assert counts_snapshot(tmp_path, child_id)["model_calls"] == 3
        assert not any(isinstance(e.event, ExecutionCountsRecorded) for e in child_events)
        (tmp_path / "sessions" / f"{child_id}.jsonl").unlink()
    kernel.session.close()
    kernel = await make_kernel(tmp_path, provider, resume_session_id=sid)
    try:
        assert totals(kernel) == (3, 0, 4) and not budget(kernel).busy
        refused = await kernel.runner.run_result(prompt="exhausted", model=None, parent=kernel.session)
        assert refused.status == "blocked" and "child limit" in refused.reason
        assert len(provider.calls) == 3 and totals(kernel) == (3, 0, 4)
    finally:
        kernel.session.close()


async def test_cancelled_descendants_keep_reservations_but_release_live_capacity(tmp_path):
    entered = asyncio.Event()

    class Waiting(FakeProvider):
        async def infer(self, request):
            entered.set()
            await asyncio.Event().wait()
            yield

    kernel = await make_kernel(tmp_path, Waiting([]))
    sid = kernel.session.id
    task = asyncio.create_task(run_strategy_result("ensemble", kernel.runner, kernel.session, "work", [Expert("a")]))
    await asyncio.wait_for(entered.wait(), 3)
    assert totals(kernel) == (1, 0, 2) and budget(kernel).busy
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert totals(kernel) == (1, 0, 2) and not budget(kernel).busy
    kernel.session.close()
    kernel = await make_kernel(tmp_path, FakeProvider([]), resume_session_id=sid)
    try:
        assert totals(kernel) == (1, 0, 2) and not budget(kernel).busy
        assert not budget(kernel).activity.snapshot()
    finally:
        kernel.session.close()


async def test_retry_attempts_are_charged_once_and_budget_refusals_stay_uncharged(tmp_path):
    from harness.errors import NetworkFailed

    class Flaky(FakeProvider):
        attempts = 0

        async def infer(self, request):
            self.attempts += 1
            if self.attempts == 1:
                raise NetworkFailed("try again")
            async for chunk in super().infer(request):
                yield chunk

    provider = Flaky([text_turn("ok")])
    kernel = await make_kernel(tmp_path, provider, execution_overrides={"max_model_calls": 2})
    kernel.loop.dispatcher.retry_delays = (0,)
    await dispatch(kernel)
    sid = kernel.session.id
    assert totals(kernel) == (2, 0, 0) and provider.attempts == 2
    kernel.session.close()
    kernel = await make_kernel(tmp_path, provider, resume_session_id=sid)
    try:
        with pytest.raises(BudgetExceeded):
            await dispatch(kernel)
        assert totals(kernel) == (2, 0, 0) and provider.attempts == 2
    finally:
        kernel.session.close()


@pytest.mark.parametrize("kind", ["model", "tool", "child", "coordinator"])
@pytest.mark.parametrize("written", [False, True])
async def test_failed_reservation_write_prevents_work_and_holds_all_admission(tmp_path, monkeypatch, kind, written):
    provider = FakeProvider([text_turn("must not run")])
    kernel = await make_kernel(tmp_path, provider)
    original = kernel.session.append

    def fail(event):
        if isinstance(event, ExecutionCountsRecorded) and event.kind == kind:
            if written:
                original(event)  # Simulate an ambiguous failure after the intent reached disk.
            raise OSError("disk unavailable")
        return original(event)

    async def work():
        if kind == "model":
            await dispatch(kernel)
        elif kind == "tool":
            await unknown_tool(kernel)
        elif kind == "child":
            await kernel.runner.run_result(prompt="work", model=None, parent=kernel.session)
        else:
            await run_strategy_result("ensemble", kernel.runner, kernel.session, "work", [Expert("a")])

    with monkeypatch.context() as scoped:
        scoped.setattr(kernel.session, "append", fail)
        with pytest.raises(OSError, match="disk unavailable"):
            await work()
        assert totals(kernel) == (0, 0, 0) and not budget(kernel).busy and provider.calls == []
        with pytest.raises(BudgetExceeded, match="accounting write failed"):
            await dispatch(kernel)
        assert "accounting write failed" in render_execution(kernel.loop.dispatcher.scope)
    sid = kernel.session.id
    kernel.session.close()
    kernel = await make_kernel(tmp_path, provider, resume_session_id=sid)
    try:
        expected = {"model": (1, 0, 0), "tool": (0, 1, 0), "child": (0, 0, 1), "coordinator": (0, 0, 1)}
        assert totals(kernel) == (expected[kind] if written else (0, 0, 0))
        await dispatch(kernel)
        assert len(provider.calls) == 1
    finally:
        kernel.session.close()


async def test_crash_after_durable_reservation_does_not_restore_allowance(tmp_path):
    script = '''
import asyncio, os, sys
from pathlib import Path
from harness.cli import build_kernel
from harness.provider import FakeProvider
async def main():
    kernel = build_kernel(base_dir=Path(sys.argv[1]), model="fake", provider=FakeProvider([]),
                          execution_overrides={"max_model_calls": 1})
    await kernel.loop.start()
    print(kernel.session.id, flush=True)
    kernel.loop.dispatcher.scope.budget.reserve_call("model", session=kernel.session, call_id="before-provider")
    os._exit(17)
asyncio.run(main())
'''
    process = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert process.returncode == 17, process.stderr
    sid = process.stdout.strip()
    provider = FakeProvider([])
    kernel = await make_kernel(tmp_path, provider, resume_session_id=sid)
    try:
        assert totals(kernel) == (1, 0, 0)
        with pytest.raises(BudgetExceeded, match="model_calls limit"):
            await dispatch(kernel)
        assert provider.calls == []
    finally:
        kernel.session.close()


@pytest.mark.parametrize("descendant", [False, True])
async def test_legacy_resume_conservatively_counts_root_work_and_holds_missing_descendants(tmp_path, descendant):
    with Session(tmp_path, "legacy") as session:
        session.start()
        session.append(ModelCallStarted(call_id="old", model="fake"))
        session.append(RetryAttempted(call_id="old", attempt=1, reason="network"))
        session.append(ToolCallProposed(call_id="old-tool", tool="missing", args={}))
        if descendant:
            session.append(SubagentSpawned(child_session_id="missing-child", call_id=None, model="fake"))
    provider = FakeProvider([text_turn("new")])
    kernel = await make_kernel(tmp_path, provider, resume_session_id="legacy")
    try:
        assert totals(kernel) == (2, 1, int(descendant))
        assert "conservatively" in render_execution(kernel.loop.dispatcher.scope)
        if descendant:
            with pytest.raises(BudgetExceeded, match="legacy descendant counts are incomplete"):
                await dispatch(kernel)
            assert provider.calls == []
            assert "Admission held" in render_execution(kernel.loop.dispatcher.scope)
        else:
            await dispatch(kernel)
            assert totals(kernel) == (3, 1, 0)
    finally:
        kernel.session.close()
    kernel = await make_kernel(tmp_path, FakeProvider([]), resume_session_id="legacy")
    try:
        assert totals(kernel) == ((2, 1, 1) if descendant else (3, 1, 0))
    finally:
        kernel.session.close()


async def test_old_binary_work_after_new_records_is_counted_on_upgrade(tmp_path):
    kernel = await make_kernel(tmp_path, FakeProvider([text_turn("new writer")]))
    await dispatch(kernel)
    sid = kernel.session.id
    # Prior binaries append this boundary but skip the new ledger event types.
    kernel.session.append(SessionResumed())
    kernel.session.append(ModelCallStarted(call_id="older-writer", model="fake"))
    kernel.session.append(ToolCallProposed(call_id="older-tool", tool="missing", args={}))
    kernel.session.close()
    kernel = await make_kernel(tmp_path, FakeProvider([]), resume_session_id=sid)
    try:
        assert totals(kernel) == (2, 1, 0) and budget(kernel).ledger.state.legacy
    finally:
        kernel.session.close()


@pytest.mark.parametrize("corruption", ["negative", "bool", "missing", "reset", "duplicate", "root-change"])
async def test_malformed_or_nonmonotone_ledger_refuses_resume_before_append(tmp_path, corruption):
    kernel = await make_kernel(tmp_path, FakeProvider([text_turn("ok")]))
    await dispatch(kernel)
    sid = kernel.session.id
    kernel.session.close()
    path = tmp_path / "sessions" / f"{sid}.jsonl"
    wire = [json.loads(line) for line in path.read_text().splitlines()]
    reservation = next(row for row in wire if row["event"]["type"] == "execution_counts_recorded" and row["event"]["kind"] == "model")
    if corruption == "missing":
        del reservation["event"]["model_calls"]
    elif corruption == "duplicate":
        duplicate = json.loads(json.dumps(reservation))
        duplicate["seq"] = wire[-1]["seq"] + 1
        wire.append(duplicate)
    elif corruption == "root-change":
        reservation["event"] = {"type": "execution_counts_linked", "root_session_id": "foreign"}
    else:
        reservation["event"]["model_calls"] = {"negative": -1, "bool": True, "reset": 0}[corruption]
    path.write_text("".join(json.dumps(row) + "\n" for row in wire))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="execution"):
        await make_kernel(tmp_path, FakeProvider([]), resume_session_id=sid)
    assert path.read_bytes() == before


async def test_redacted_reservation_cannot_authorize_work(tmp_path):
    kernel = await make_kernel(tmp_path, FakeProvider([]))
    kernel.session._redactors.append(lambda event: event.model_copy(update={"model_calls": 0})
        if isinstance(event, ExecutionCountsRecorded) and event.kind == "model" else event)
    try:
        with pytest.raises(ValueError, match="cannot be rewritten"):
            await dispatch(kernel)
        assert totals(kernel) == (0, 0, 0) and kernel.provider.calls == []
        assert not budget(kernel).ledger.healthy
    finally:
        kernel.session.close()


async def test_portable_counts_use_frozen_prefix_and_inspection_does_not_write(tmp_path):
    kernel = await make_kernel(tmp_path, FakeProvider([text_turn("ok")]))
    try:
        kernel.tasks.create("counted task")
        await kernel.loop.run_task(kernel.tasks.prepare("work"))
        prefix = read_session(tmp_path, kernel.session.id)
        package, _ = task_package(tmp_path, kernel.session.id)
        assert package["execution_counts"]["model_calls"] == 1
        assert package["execution_counts"]["through_seq"] == prefix[-1].seq
        render_execution(kernel.loop.dispatcher.scope)
        assert read_session(tmp_path, kernel.session.id) == prefix
        await unknown_tool(kernel)
        assert counts_snapshot(tmp_path, kernel.session.id, events=prefix)["tool_calls"] == 0
        assert counts_snapshot(tmp_path, kernel.session.id)["tool_calls"] == 1
    finally:
        kernel.session.close()


async def test_standalone_counts_retain_in_memory_reservations_when_attached(tmp_path):
    b = ExecutionBudget()
    b.reserve_child(1)
    b.release_child()
    with Session(tmp_path, "root") as session:
        session.start()
        b.attach(session)
        assert b.children == 1 and not b.busy
        assert project_counts(read_session(tmp_path, "root")).children == 1


async def test_tui_refuses_corrupt_target_before_teardown_and_restores_counts(tmp_path):
    from textual.widgets import Input
    from harness.tui_support import SlashCommand
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    kernel = await make_kernel(tmp_path, FakeProvider([text_turn("done")]))
    await dispatch(kernel)
    sid = kernel.session.id
    kernel.session.close()
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        await app._rebuild_kernel(sid)
        await pilot.pause(.1)
        assert totals(app.kernel) == (1, 0, 0)
        app.query_one("#prompt", Input).value = "draft survives inspection"
        await app._run_command(SlashCommand("execution", ""))
        await pilot.pause(.1)
        assert "1 model calls" in screen_text(app)
        assert "admission counts persist" in screen_text(app)
        assert app.query_one("#prompt", Input).value == "draft survives inspection"
        with Session(tmp_path, "corrupt") as corrupt:
            corrupt.start()
            corrupt.append(UnknownEvent(raw={"type": "execution_counts_recorded"}))
        before = app.kernel
        assert "invalid stored execution counts" in app._preflight_resume("corrupt")
        assert app.kernel is before and not before.session.closed
        assert not fold(read_session(tmp_path, sid)).execution_counts.legacy


async def test_independent_budget_cannot_reset_a_live_sessions_counts(tmp_path):
    kernel = await make_kernel(tmp_path, FakeProvider([text_turn("ok")]))
    try:
        await dispatch(kernel)
        before = read_session(tmp_path, kernel.session.id)
        with pytest.raises(ValueError, match="share its execution scope"):
            ExecutionBudget().reserve_call("model", session=kernel.session)
        assert totals(kernel) == (1, 0, 0)
        assert read_session(tmp_path, kernel.session.id) == before
    finally:
        kernel.session.close()


async def test_child_ownership_write_failure_holds_root_before_child_provider(tmp_path, monkeypatch):
    kernel = await make_kernel(tmp_path, FakeProvider([]))
    original = Session.append

    def fail(self, event):
        if isinstance(event, ExecutionCountsLinked):
            raise OSError("ownership write failed")
        return original(self, event)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(Session, "append", fail)
            child = await kernel.runner.run_result(prompt="work", model=None, parent=kernel.session)
            assert child.status == "failed"
        assert totals(kernel) == (0, 0, 1) and not budget(kernel).busy
        assert kernel.provider.calls == []
        with pytest.raises(BudgetExceeded, match="accounting write failed"):
            await dispatch(kernel)
    finally:
        kernel.session.close()
