"""Coordinators share authority and total work limits without occupying worker slots."""

import asyncio

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.blobs import BlobStore
from harness.cli import build_kernel
from harness.coordination_cli import render_coordination
from harness.events import CoordinationFinished, CoordinationStarted, SubagentFinished, SubagentSpawned
from harness.execution import ExecutionLimits, current_scope
from harness.fold import fold
from harness.frontmatter import AgentDef
from harness.log import read_session
from harness.mixture import Expert, run_strategy_result
from harness.portable import task_package
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.session import Session
from harness.types import ModelId, SessionId, ToolName
from tests.test_coordination_results import saved_report
from tests.test_mixture import FakeRunner
from tests.test_subagent import _runner


@pytest.fixture
def parent(tmp_path):
    with Session(tmp_path, SessionId("root")) as session:
        session.start()
        yield session


def setup_runner(parent, provider, **limits):
    runner = _runner(parent.base, provider)
    budget = runner.scope_for(parent).budget
    budget.limits = ExecutionLimits(**limits)
    return runner, budget


@pytest.mark.parametrize("configured", [False, True])
async def test_coordinator_can_fill_all_worker_slots(parent, configured):
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Waiting:
        async def complete(self, **kwargs):
            scope = current_scope.get()
            seen.append((scope.depth, scope.budget.active_children, scope.budget.active_coordinators))
            if len(seen) == 2:
                entered.set()
            await release.wait()
            for chunk in text_turn("answer"):
                yield chunk

    runner, budget = setup_runner(parent, Waiting(), max_active_children=2)
    if configured:
        runner.agents["team"] = AgentDef(name="team", description="", body="", strategy="ensemble", experts=("a", "b"))
        work = runner.run_result(prompt="work", model=None, parent=parent, agent="team")
    else:
        work = run_strategy_result("ensemble", runner, parent, "work", [Expert("a"), Expert("b")])
    task = asyncio.create_task(work)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert budget.active_children == 2 and budget.active_coordinators == 1 and budget.children == 3
        assert seen == [(2, 1, 1), (2, 2, 1)]
    finally:
        release.set()
        result = await asyncio.wait_for(task, 3)
    assert result.status == "completed" and not budget.busy
    starts = [e.event for e in read_session(parent.base, parent.id) if isinstance(e.event, CoordinationStarted)]
    assert len(starts) == 1 and starts[0].depth == 1
    assert saved_report(parent).admitted and current_scope.get() is None


async def test_concurrent_coordinator_limit_refuses_before_spawning_and_releases_on_cancel(parent):
    entered = asyncio.Event()

    class Waiting:
        async def complete(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

    runner, budget = setup_runner(parent, Waiting(), max_active_coordinators=1)
    first = asyncio.create_task(run_strategy_result("ensemble", runner, parent, "first", [Expert("a")]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        blocked = await run_strategy_result("ensemble", runner, parent, "second", [Expert("b")])
        assert blocked.status == "blocked" and "coordinator capacity" in blocked.reason
        assert saved_report(parent).admitted is False and budget.children == 2
        assert sum(isinstance(e.event, SubagentSpawned) for e in read_session(parent.base, parent.id)) == 1
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert not budget.busy and current_scope.get() is None


@pytest.mark.parametrize("limits", [{"max_children": 0}, {"max_depth": 0}, {"max_active_coordinators": 0}])
async def test_refused_admission_consumes_no_work(parent, limits):
    runner, budget = setup_runner(parent, FakeProvider([]), **limits)
    result = await run_strategy_result("ensemble", runner, parent, "work", [Expert("a")])
    assert result.status == "blocked" and not runner.provider.calls
    assert budget.children == 0 and not budget.busy
    events = read_session(parent.base, parent.id)
    assert not any(isinstance(e.event, (CoordinationStarted, SubagentSpawned)) for e in events)
    assert saved_report(parent).admitted is False


async def test_cumulative_descendants_count_direct_coordinators_and_children(parent):
    runner, budget = setup_runner(parent, FakeProvider([text_turn("done")]), max_children=3)
    first = await run_strategy_result("ensemble", runner, parent, "one", [Expert("a")])
    second = await run_strategy_result("ensemble", runner, parent, "two", [Expert("a")])
    third = await run_strategy_result("ensemble", runner, parent, "three", [Expert("a")])
    assert first.status == "completed" and second.status == "failed" and third.status == "blocked"
    assert budget.children == 3 and not budget.busy and len(runner.provider.calls) == 1


async def test_nested_coordinators_retain_depth_limit(parent):
    runner, budget = setup_runner(parent, FakeProvider([]), max_depth=2)
    runner.agents["inner"] = AgentDef(name="inner", description="", body="", strategy="ensemble", experts=("fake",))
    result = await run_strategy_result("ensemble", runner, parent, "work", [Expert("fake", "inner")])
    assert result.status == "failed" and not runner.provider.calls and budget.children == 2
    assert not budget.busy
    events = read_session(parent.base, parent.id)
    assert [e.event.depth for e in events if isinstance(e.event, CoordinationStarted)] == [1, 2]
    assert not any(isinstance(e.event, SubagentSpawned) for e in events)


async def test_deadline_settles_children_and_retains_completed_proposal(parent):
    cleaning, release_cleanup = asyncio.Event(), asyncio.Event()

    class Partial:
        async def complete(self, *, model, **kwargs):
            if model == "proposer":
                for chunk in text_turn("useful proposal"):
                    yield chunk
                return
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()

    runner, budget = setup_runner(parent, Partial(), coordination_timeout_seconds=.1)
    task = asyncio.create_task(run_strategy_result("panel", runner, parent, "work",
                                                   [Expert("proposer"), Expert("critic")]))
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not task.done() and budget.busy
        assert not any(isinstance(e.event, CoordinationFinished) for e in read_session(parent.base, parent.id))
    finally:
        release_cleanup.set()
        result = await asyncio.wait_for(task, 3)
    assert result.status == "incomplete" and result.reason == "coordination deadline"
    assert not budget.busy and current_scope.get() is None
    report = saved_report(parent)
    assert report.timeout_seconds == .1 and report.admitted
    assert [m.result.status for m in report.members] == ["completed", "cancelled"]
    proposal = report.members[0].result
    blobs = BlobStore(parent.base / "sessions" / proposal.child_session_id / "blobs", create=False)
    assert blobs.get(proposal.output) == b"useful proposal"
    for member in report.members:
        state = fold(read_session(parent.base, member.result.child_session_id))
        assert not state.open_agent_runs and not state.open_model_intents
    events = read_session(parent.base, parent.id)
    assert isinstance(events[-1].event, CoordinationFinished)
    assert sum(isinstance(e.event, SubagentFinished) for e in events) == 2
    assert "Reason: coordination deadline" in render_coordination(parent.base, parent.id)


async def test_inner_timeout_error_is_not_mislabeled_as_coordination_deadline(parent):
    class Broken(FakeRunner):
        async def run_result(self, **kwargs):
            raise TimeoutError("internal failure")

    runner = Broken({})
    with pytest.raises(TimeoutError):
        await run_strategy_result("ensemble", runner, parent, "work", [Expert("a")])
    assert saved_report(parent).result.status == "failed"
    assert saved_report(parent).result.reason == "TimeoutError"
    assert not runner.scope_for(parent).budget.busy


@pytest.mark.parametrize("event_type", [CoordinationStarted, CoordinationFinished])
async def test_publication_failure_releases_capacity_without_fabricating_terminal(parent, monkeypatch, event_type):
    runner, budget = setup_runner(parent, FakeProvider([text_turn("done")]))
    original = parent.append

    def append(event):
        if isinstance(event, event_type):
            raise OSError("injected persistence failure")
        return original(event)

    monkeypatch.setattr(parent, "append", append)
    with pytest.raises(OSError):
        await run_strategy_result("ensemble", runner, parent, "work", [Expert("a")])
    assert not budget.busy and current_scope.get() is None
    assert not any(isinstance(e.event, CoordinationFinished) for e in read_session(parent.base, parent.id))
    if event_type is CoordinationFinished:
        assert "completion unconfirmed" in render_coordination(parent.base, parent.id)


async def test_ancestor_deadline_still_cancels_coordination(tmp_path):
    class Waiting(FakeProvider):
        async def complete(self, **kwargs):
            if self.calls:
                await asyncio.Event().wait()
            async for chunk in super().complete(**kwargs):
                yield chunk

    provider = Waiting([tool_call_turn("", ToolName("ensemble"), {"prompt": "work", "models": ["fake"]})])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"))
    try:
        await kernel.loop.start()
        with pytest.raises(TimeoutError):
            await kernel.loop.run_task(AgentTask(prompt="work", limits=TaskLimits(timeout_seconds=.1)))
        assert saved_report(kernel.session).result.status == "cancelled"
        assert not kernel.loop.dispatcher.scope.budget.busy
        assert not fold(read_session(tmp_path, kernel.session.id)).open_agent_runs
    finally:
        kernel.session.close()


async def test_improvement_and_handoff_controls_are_not_idle_while_coordinating(tmp_path):
    from tests.test_handoff import source, specification
    kernel, _ = await source(tmp_path)
    budget = kernel.loop.dispatcher.scope.budget
    budget.reserve_coordinator(1)
    try:
        assert budget.active_children == 0
        with pytest.raises(ValueError, match="idle"):
            kernel.handoffs.record(specification(kernel))
        with pytest.raises(ValueError, match="idle"):
            await kernel.improvement_service.propose(model=ModelId("local"))
    finally:
        budget.release_coordinator()
        kernel.session.close()


async def test_handoff_retains_new_source_limits(tmp_path):
    from tests.test_handoff import source, specification
    kernel, _ = await source(tmp_path, limits=ExecutionLimits(max_active_coordinators=2, coordination_timeout_seconds=15))
    try:
        record = kernel.handoffs.record(specification(kernel))
        budget = kernel.loop.dispatcher.scope.budget
        budget.limits = ExecutionLimits(max_active_coordinators=10, coordination_timeout_seconds=100)
        await kernel.handoffs.run(record.id)
        assert budget.limits.max_active_coordinators == 2 and budget.limits.coordination_timeout_seconds == 15
    finally:
        kernel.session.close()


@pytest.mark.parametrize("missing", ["count", "capacity", "deadline", "all"])
@pytest.mark.parametrize("tighter", [False, True])
async def test_handoff_resumes_compatible_legacy_scope_without_resetting_limits(tmp_path, monkeypatch, missing, tighter):
    from dataclasses import replace
    from harness.handoff import capture_scope, load_record
    from tests.test_handoff import permissions, source, specification

    def legacy_scope(dispatcher):
        scope = capture_scope(dispatcher)
        if missing in {"count", "all"}:
            scope["counts"].pop("active_coordinators")
        if missing in {"capacity", "all"}:
            scope["limits"].pop("max_active_coordinators")
        if missing in {"deadline", "all"}:
            scope["limits"].pop("coordination_timeout_seconds")
        return scope

    # Keep a compatible policy identity to exercise shape compatibility; a
    # genuinely different implementation must still be held separately.
    monkeypatch.setattr("harness.handoff.capture_scope", legacy_scope)
    original_limits = ExecutionLimits(max_model_calls=4, max_tool_calls=2, max_children=3,
        max_depth=2, max_active_children=1, max_active_coordinators=7, coordination_timeout_seconds=60)
    kernel, provider = await source(tmp_path, limits=original_limits)
    try:
        record = kernel.handoffs.record(specification(kernel))
        checkpoint_bytes = load_record(kernel.session, record)[0].encoded()
        session_id = kernel.session.id
    finally:
        kernel.session.close()
    monkeypatch.setattr("harness.handoff.capture_scope", capture_scope)
    current_limits = ExecutionLimits(max_active_coordinators=2 if tighter else 32,
                                    coordination_timeout_seconds=15 if tighter else 1200)
    kernel = build_kernel(base_dir=tmp_path / "sessions", provider=provider, model=ModelId("local"),
        resume_session_id=session_id, native_tools=True, workspace_root=provider.root,
        permissions=permissions(), execution_limits=current_limits)
    provider.steps = ["new", "done"]
    try:
        result = await kernel.handoffs.run(record.id)
        assert result.status == "completed" and (provider.root / "B.txt").read_text() == "stage B\n"
        defaults = ExecutionLimits()
        capacity = defaults.max_active_coordinators if missing in {"capacity", "all"} else 7
        timeout = defaults.coordination_timeout_seconds if missing in {"deadline", "all"} else 60
        budget = kernel.loop.dispatcher.scope.budget
        assert budget.limits == replace(original_limits,
            max_active_coordinators=min(capacity, current_limits.max_active_coordinators),
            coordination_timeout_seconds=min(timeout, current_limits.coordination_timeout_seconds))
        assert budget.model_calls == 3 and budget.tool_calls == 2
        assert load_record(kernel.session, record)[0].encoded() == checkpoint_bytes
    finally:
        kernel.session.close()


async def test_legacy_handoff_from_different_policy_remains_held(tmp_path, monkeypatch):
    from harness.handoff import capture_scope
    from tests.test_handoff import source, specification

    def legacy_scope(dispatcher):
        scope = capture_scope(dispatcher)
        scope["version"] = "previous-implementation"
        scope["counts"].pop("active_coordinators")
        scope["limits"].pop("max_active_coordinators")
        scope["limits"].pop("coordination_timeout_seconds")
        return scope

    monkeypatch.setattr("harness.handoff.capture_scope", legacy_scope)
    kernel, provider = await source(tmp_path)
    try:
        record = kernel.handoffs.record(specification(kernel))
        monkeypatch.setattr("harness.handoff.capture_scope", capture_scope)
        before = read_session(kernel.session.base, kernel.session.id)
        with pytest.raises(ValueError, match="source authority is not portable"):
            await kernel.handoffs.run(record.id)
        assert read_session(kernel.session.base, kernel.session.id) == before
        assert not provider.requests and not (provider.root / "B.txt").exists()
    finally:
        kernel.session.close()


@pytest.mark.parametrize("counter", ["active_children", "active_coordinators"])
async def test_legacy_handoff_defaults_do_not_clear_recorded_activity(tmp_path, monkeypatch, counter):
    from harness.handoff import capture_scope
    from tests.test_handoff import source, specification

    def legacy_scope(dispatcher):
        scope = capture_scope(dispatcher)
        scope["counts"][counter] = 1
        scope["limits"].pop("max_active_coordinators")
        scope["limits"].pop("coordination_timeout_seconds")
        return scope

    monkeypatch.setattr("harness.handoff.capture_scope", legacy_scope)
    kernel, provider = await source(tmp_path)
    try:
        record = kernel.handoffs.record(specification(kernel))
        monkeypatch.setattr("harness.handoff.capture_scope", capture_scope)
        with pytest.raises(ValueError, match="reconciliation/accounting"):
            await kernel.handoffs.run(record.id)
        assert not provider.requests and not (provider.root / "B.txt").exists()
    finally:
        kernel.session.close()


async def test_handoff_holds_unreconciled_coordination_even_without_children(tmp_path):
    from tests.test_handoff import source, specification
    kernel, _ = await source(tmp_path)
    try:
        runner = kernel.registry.get(ToolName("dispatch_agent")).runner
        token = current_scope.set(kernel.loop.dispatcher.scope)
        try:
            result = await run_strategy_result("ensemble", runner, kernel.session, "invalid", [])
        finally:
            current_scope.reset(token)
        assert result.status == "blocked" and not kernel.loop.dispatcher.scope.budget.busy
        record = kernel.handoffs.record(specification(kernel))
        with pytest.raises(ValueError, match="reconciliation/accounting"):
            await kernel.handoffs.run(record.id)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("configured", [False, True])
async def test_export_retains_unconfirmed_coordination_start(tmp_path, monkeypatch, configured):
    tool = "dispatch_agent" if configured else "ensemble"
    args = {"prompt": "work", "agent": "team"} if configured else {"prompt": "work", "models": ["fake"]}
    provider = FakeProvider([tool_call_turn("", ToolName(tool), args),
                             text_turn("child work"), text_turn("root done")])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"))
    if configured:
        runner = kernel.registry.get(ToolName("dispatch_agent")).runner
        runner.agents["team"] = AgentDef(name="team", description="", body="", strategy="ensemble", experts=("fake",))
    original = kernel.session.append

    def append(event):
        if isinstance(event, CoordinationFinished):
            raise OSError("terminal publication failed")
        return original(event)

    try:
        await kernel.loop.start()
        kernel.tasks.create("Keep uncertain work inspectable")
        monkeypatch.setattr(kernel.session, "append", append)
        await kernel.loop.run_task(kernel.tasks.prepare("work"))
        package, _ = task_package(tmp_path, kernel.session.id)
        row, = package["coordination"]
        assert row["status"] == "unconfirmed" and row["report"] is None
        assert row["started_seq"] == row["source_seq"] and row["timeout_seconds"] == 600
        assert not package["task"]["accepted"]
        assert "completion unconfirmed" in render_coordination(tmp_path, kernel.session.id)
        from harness.tui_panel import fold_agents
        aggregate = next(r for r in fold_agents(read_session(tmp_path, kernel.session.id)) if r.call_id == row["call_id"])
        assert aggregate.status == "unconfirmed"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "1"])
def test_invalid_deadline_configuration_is_rejected(value):
    with pytest.raises(ValueError, match="positive and finite"):
        ExecutionLimits(coordination_timeout_seconds=value)


async def test_older_report_does_not_invent_admission_metadata(parent):
    import json
    from harness.coordination import load_report
    await run_strategy_result("ensemble", FakeRunner({"a": "answer"}), parent, "work", [Expert("a")])
    report = saved_report(parent).model_dump(mode="json", exclude={"admitted", "timeout_seconds"})
    terminal = read_session(parent.base, parent.id)[-1].event
    legacy = terminal.model_copy(update={"report": parent.blobs.put(json.dumps(report).encode())})
    restored = load_report(parent.blobs, legacy, parent.id)
    assert restored.result.status == "completed"
    assert restored.admitted is None and restored.timeout_seconds is None
    report["admitted"] = True
    partial = terminal.model_copy(update={"report": parent.blobs.put(json.dumps(report).encode())})
    with pytest.raises(ValueError, match="recorded together"):
        load_report(parent.blobs, partial, parent.id)
