"""Direct coordination belongs to a task by recorded run ID, not time overlap."""

import asyncio

import pytest

from harness.agent import AgentOutput, AgentTask, execute_task
from harness.cli import build_kernel
from harness.events import CoordinationFinished, CoordinationStarted
from harness.log import read_session
from harness.mixture import Expert, run_strategy_result
from harness.portable import ExportError, task_package
from harness.provider import FakeProvider, text_turn, tool_call_turn


async def direct(kernel, task_id, body=None):
    async def execute():
        result = await run_strategy_result("ensemble", kernel.runner, kernel.session, "public fixture", [Expert("fake")])
        if body is not None:
            await body()
        return AgentOutput(result.text)

    return await execute_task(kernel.session, AgentTask(id=task_id, prompt="public fixture",
                              acceptance_criteria=kernel.tasks.state().items[task_id].criteria),
                              runtime="harness", model=None, execute=execute)


@pytest.fixture
async def kernel(tmp_path):
    kernel = build_kernel(base_dir=tmp_path, model="fake",
                          provider=FakeProvider([text_turn("retained output")] * 4))
    await kernel.loop.start()
    try:
        yield kernel
    finally:
        kernel.session.close()


def saved(kernel):
    return [e for e in read_session(kernel.session.base, kernel.session.id)
            if isinstance(e.event, (CoordinationStarted, CoordinationFinished))]


async def test_exports_only_the_selected_tasks_direct_coordinations(kernel):
    one = kernel.tasks.create("First task").id
    two = kernel.tasks.create("Second task").id
    first = await direct(kernel, one)
    second = await direct(kernel, two)
    for identity, result in ((one, first), (two, second)):
        package, artifacts = task_package(kernel.session.base, kernel.session.id, task_id=identity)
        row, = package["coordination"]
        assert row["run_id"] == result.run_id and row["call_id"] is None
        assert row["status"] == "completed"
        assert len(package["child_sessions"]) == 1
        assert package["child_sessions"][0]["run_id"] == result.run_id
        assert artifacts[row["output"]["path"]] == b"retained output"
    assert [e.event.agent_run_id for e in saved(kernel)] == [first.run_id] * 2 + [second.run_id] * 2


async def test_concurrent_unowned_coordination_is_not_inferred_from_time_overlap(kernel):
    task_id = kernel.tasks.create("Owned task").id
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    work = asyncio.create_task(direct(kernel, task_id, hold))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await run_strategy_result("ensemble", kernel.runner, kernel.session, "unowned", [Expert("fake")])
        assert saved(kernel)[-1].event.agent_run_id is None
    finally:
        release.set()
        await work
    package, _ = task_package(kernel.session.base, kernel.session.id)
    row, = package["coordination"]
    assert row["id"] == saved(kernel)[0].event.id
    assert len(package["child_sessions"]) == 1


async def test_rejected_admission_keeps_direct_outcome_without_start(kernel):
    from dataclasses import replace
    value = kernel.loop.dispatcher.scope.budget
    value.limits = replace(value.limits, max_active_coordinators=0)
    result = await direct(kernel, kernel.tasks.create("Blocked work").id)
    terminal, = saved(kernel)
    assert isinstance(terminal.event, CoordinationFinished)
    package, _ = task_package(kernel.session.base, kernel.session.id)
    row, = package["coordination"]
    assert row["status"] == "blocked" and row["run_id"] == result.run_id
    assert "started_seq" not in row


@pytest.mark.parametrize("change", ["owner", "call", "duplicate"])
async def test_changed_direct_terminal_cannot_hide_or_reassign_the_owned_start(kernel, monkeypatch, change):
    await direct(kernel, kernel.tasks.create("Owned work").id)
    log = read_session(kernel.session.base, kernel.session.id)
    terminal = next(e for e in log if isinstance(e.event, CoordinationFinished))
    if change == "duplicate":
        log.insert(log.index(terminal) + 1, terminal)
    else:
        event = terminal.event.model_copy(update={"agent_run_id" if change == "owner" else "call_id": "unrelated"})
        log[log.index(terminal)] = terminal.model_copy(update={"event": event})
    monkeypatch.setattr("harness.portable.read_session", lambda *args, **kwargs: log)
    with pytest.raises(ExportError, match="ambiguous coordination terminal"):
        task_package(kernel.session.base, kernel.session.id)


async def test_direct_event_outside_its_run_is_rejected(kernel, monkeypatch):
    await direct(kernel, kernel.tasks.create("Owned work").id)
    log = read_session(kernel.session.base, kernel.session.id)
    terminal = next(e for e in log if isinstance(e.event, CoordinationFinished))
    log.remove(terminal)
    log.append(terminal)
    monkeypatch.setattr("harness.portable.read_session", lambda *args, **kwargs: log)
    with pytest.raises(ExportError, match="outside its agent run"):
        task_package(kernel.session.base, kernel.session.id)


async def test_missing_direct_terminal_remains_unconfirmed(kernel, monkeypatch):
    await direct(kernel, kernel.tasks.create("Interrupted publication").id)
    log = [e for e in read_session(kernel.session.base, kernel.session.id)
           if not isinstance(e.event, CoordinationFinished)]
    monkeypatch.setattr("harness.portable.read_session", lambda *args, **kwargs: log)
    package, _ = task_package(kernel.session.base, kernel.session.id)
    row, = package["coordination"]
    assert row["status"] == "unconfirmed" and row["report"] is None


async def test_legacy_direct_records_do_not_get_guessed_owners(kernel, monkeypatch):
    from harness.events import SubagentSpawned
    await direct(kernel, kernel.tasks.create("Legacy direct operations").id)
    log = read_session(kernel.session.base, kernel.session.id)
    log = [e.model_copy(update={"event": e.event.model_copy(update={"agent_run_id": None})})
           if isinstance(e.event, (CoordinationStarted, CoordinationFinished, SubagentSpawned)) else e for e in log]
    monkeypatch.setattr("harness.portable.read_session", lambda *args, **kwargs: log)
    package, _ = task_package(kernel.session.base, kernel.session.id)
    assert not package["coordination"] and not package["child_sessions"]


async def test_legacy_tool_owned_coordination_remains_exportable(kernel, monkeypatch):
    kernel.set_provider(FakeProvider([
        tool_call_turn("team", "ensemble", {"prompt": "public fixture", "models": ["fake"]}),
        text_turn("retained output"), text_turn("root finished")]))
    kernel.tasks.create("Legacy tool ownership")
    await kernel.loop.run_task(kernel.tasks.prepare("Run ensemble"))
    log = read_session(kernel.session.base, kernel.session.id)
    log = [e.model_copy(update={"event": e.event.model_copy(update={"agent_run_id": None})})
           if isinstance(e.event, (CoordinationStarted, CoordinationFinished)) else e for e in log]
    monkeypatch.setattr("harness.portable.read_session", lambda *args, **kwargs: log)
    package, _ = task_package(kernel.session.base, kernel.session.id)
    row, = package["coordination"]
    assert row["status"] == "completed" and row["run_id"] is None and row["call_id"]


async def test_explicit_owner_must_match_recorded_tool_owner(kernel, monkeypatch):
    kernel.set_provider(FakeProvider([
        tool_call_turn("team", "ensemble", {"prompt": "public fixture", "models": ["fake"]}),
        text_turn("retained output"), text_turn("root finished")]))
    kernel.tasks.create("Conflicting owners")
    await kernel.loop.run_task(kernel.tasks.prepare("Run ensemble"))
    log = read_session(kernel.session.base, kernel.session.id)
    log = [e.model_copy(update={"event": e.event.model_copy(update={"agent_run_id": "unrelated"})})
           if isinstance(e.event, CoordinationStarted) else e for e in log]
    monkeypatch.setattr("harness.portable.read_session", lambda *args, **kwargs: log)
    with pytest.raises(ExportError, match="owner does not match"):
        task_package(kernel.session.base, kernel.session.id)
