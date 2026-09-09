"""File ownership survives cancellation until the actual mutation has settled."""

import asyncio
import threading

import pytest

from harness.native_tools import EditFileTool, ReadState, WriteFileTool


def mutation(root, kind, *, target="shared.txt", before="v0", after="v1"):
    reads = ReadState({str((root / target).resolve())})
    if kind == "write":
        return (WriteFileTool(workspace_root=root, read_state=reads), "_write",
                {"file_path": target, "content": after})
    return (EditFileTool(workspace_root=root, read_state=reads), "_edit",
            {"file_path": target, "old_string": before, "new_string": after})


@pytest.mark.parametrize("first_kind", ["write", "edit"])
@pytest.mark.parametrize("second_kind", ["write", "edit"])
async def test_cancelled_owner_keeps_canonical_path_until_thread_settles(tmp_path, monkeypatch, first_kind, second_kind):
    (tmp_path / "shared.txt").write_text("v0")
    (tmp_path / "alias.txt").symlink_to("shared.txt")
    entered, release, settled, successor_entered = (threading.Event() for _ in range(4))
    first, first_method, first_args = mutation(tmp_path, first_kind)
    second, second_method, second_args = mutation(tmp_path, second_kind, target="alias.txt", before="v1", after="v2")
    first_operation, second_operation = getattr(first, first_method), getattr(second, second_method)

    def paused(*args):
        entered.set()
        try:
            assert release.wait(5)
            return first_operation(*args)
        finally:
            settled.set()

    def successor(*args):
        successor_entered.set()
        assert settled.is_set(), "successor overlapped a cancelled mutation"
        return second_operation(*args)

    monkeypatch.setattr(first, first_method, paused)
    monkeypatch.setattr(second, second_method, successor)
    owner = asyncio.create_task(first(first_args))
    waiting = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        waiting = asyncio.create_task(second(second_args))
        for _ in range(2):
            owner.cancel()
            await asyncio.sleep(.02)
            assert not owner.done() and not successor_entered.is_set()
        # The reservation is per path: unrelated changes can still complete.
        other, _, args = mutation(tmp_path, "write", target="other.txt", after="independent")
        await asyncio.wait_for(other(args), 1)
        assert not waiting.done() and (tmp_path / "other.txt").read_text() == "independent"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await asyncio.wait_for(waiting, 3)
        assert successor_entered.is_set() and (tmp_path / "shared.txt").read_text() == "v2"
    finally:
        release.set()
        await asyncio.gather(owner, *([waiting] if waiting else []), return_exceptions=True)
        assert await asyncio.to_thread(settled.wait, 3)


@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_cancelling_a_waiter_never_starts_its_mutation(tmp_path, monkeypatch, kind):
    (tmp_path / "shared.txt").write_text("v0")
    entered, release, settled = (threading.Event() for _ in range(3))
    first, method, args = mutation(tmp_path, "write")
    operation = getattr(first, method)
    second, second_method, second_args = mutation(tmp_path, kind, before="v1", after="unwanted")
    attempted = []

    def paused(*args):
        entered.set()
        try:
            assert release.wait(5)
            return operation(*args)
        finally:
            settled.set()

    monkeypatch.setattr(first, method, paused)
    monkeypatch.setattr(second, second_method, lambda *args: attempted.append(True))
    owner = asyncio.create_task(first(args))
    waiting = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        waiting = asyncio.create_task(second(second_args))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, 1)
        assert not owner.done() and not attempted
        release.set()
        await asyncio.wait_for(owner, 3)
        assert (tmp_path / "shared.txt").read_text() == "v1" and not attempted
    finally:
        release.set()
        await asyncio.gather(owner, *([waiting] if waiting else []), return_exceptions=True)
        assert await asyncio.to_thread(settled.wait, 3)


@pytest.mark.parametrize("kind", ["write", "edit"])
async def test_cancelled_mutation_observes_late_thread_failure_and_releases_lock(tmp_path, monkeypatch, kind):
    (tmp_path / "shared.txt").write_text("v0")
    entered, release, settled = (threading.Event() for _ in range(3))
    tool, method, args = mutation(tmp_path, kind)

    def fail(*args):
        entered.set()
        try:
            assert release.wait(5)
            raise OSError("injected mutation failure")
        finally:
            settled.set()

    monkeypatch.setattr(tool, method, fail)
    task = asyncio.create_task(tool(args))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        successor, _, successor_args = mutation(tmp_path, "write", after="recovered")
        await asyncio.wait_for(successor(successor_args), 3)
        assert (tmp_path / "shared.txt").read_text() == "recovered"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(settled.wait, 3)


async def test_event_loop_shutdown_waits_for_mutation_and_preserves_worker_context(tmp_path, monkeypatch):
    from contextvars import ContextVar
    entered, release, settled, shutdown, terminal = (threading.Event() for _ in range(5))
    context = ContextVar("test_mutation_owner", default=None)
    tool, method, args = mutation(tmp_path, "write")
    operation = getattr(tool, method)
    failures = []

    def paused(*args):
        entered.set()
        try:
            assert context.get() == "owner context"
            assert release.wait(5)
            return operation(*args)
        finally:
            settled.set()

    async def mutate():
        try:
            await tool(args)
        finally:
            terminal.set()

    async def program():
        context.set("owner context")
        task = asyncio.create_task(mutate())
        assert await asyncio.to_thread(entered.wait, 3)
        assert not task.done()
        shutdown.set()
        # Returning triggers asyncio.run's cancellation of all remaining tasks.

    def run():
        try:
            asyncio.run(program())
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(tool, method, paused)
    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert await asyncio.to_thread(shutdown.wait, 3)
        await asyncio.sleep(.02)
        assert not terminal.is_set()
        release.set()
        await asyncio.to_thread(runner.join, 3)
        assert not runner.is_alive() and not failures
        assert settled.is_set() and terminal.is_set()
        assert (tmp_path / "shared.txt").read_text() == "v1"
    finally:
        release.set()
        await asyncio.to_thread(runner.join, 3)


@pytest.mark.parametrize("delegated,cause", [(False, "cancel"), (False, "deadline"),
                                           (True, "cancel"), (True, "deadline")])
@pytest.mark.parametrize("notice_fails", [False, True])
async def test_task_and_delegation_settle_mutation_before_terminal_facts(
        tmp_path, monkeypatch, delegated, cause, notice_fails):
    from harness.agent import AgentTask, TaskLimits
    from harness.cli import build_kernel
    from harness.events import CoordinationFinished, CustomEvent, SubagentFinished, SubagentSpawned, ToolCallCancelled
    from harness.fold import fold
    from harness.log import read_session
    from harness.provider import FakeProvider, text_turn, tool_call_turn
    from harness.session import Session
    from harness.types import ModelId, ToolName
    from tests.test_handoff import permissions

    entered, release, settled = (threading.Event() for _ in range(3))
    notified = asyncio.Event()
    owners = []
    original_write, original_append = WriteFileTool._write, Session.append

    def paused(tool, *args):
        entered.set()
        try:
            assert release.wait(5)
            return original_write(tool, *args)
        finally:
            settled.set()

    def append(session, event):
        if isinstance(event, CustomEvent) and event.namespace == "files":
            owners.append(session.id)
            notified.set()
            if notice_fails:
                raise OSError("injected notice persistence failure")
        return original_append(session, event)

    script = [tool_call_turn("", ToolName("write_file"), {"file_path": "result.txt", "content": "retained"}),
              text_turn("must not continue after cancellation")]
    if delegated:
        script.insert(0, tool_call_turn("", ToolName("ensemble"), {"prompt": "write", "models": ["fake"]}))
    provider = FakeProvider(script)
    kernel = build_kernel(base_dir=tmp_path / "sessions", workspace_root=tmp_path, native_tools=True,
                          provider=provider, model=ModelId("fake"), permissions=permissions())
    monkeypatch.setattr(WriteFileTool, "_write", paused)
    monkeypatch.setattr(Session, "append", append)
    work = None
    try:
        await kernel.loop.start()
        work = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="write", limits=TaskLimits(
            timeout_seconds=.15 if cause == "deadline" else 10))))
        assert await asyncio.to_thread(entered.wait, 3)
        if cause == "cancel":
            work.cancel()
        await asyncio.wait_for(notified.wait(), 3)
        root_events = read_session(kernel.session.base, kernel.session.id)
        assert not work.done() and fold(root_events).open_agent_runs
        assert not any(isinstance(e.event, (CoordinationFinished, SubagentFinished)) for e in root_events)
        if delegated:
            spawn, = [e.event for e in root_events if isinstance(e.event, SubagentSpawned)]
            assert owners == [spawn.child_session_id]
            assert kernel.loop.dispatcher.scope.budget.active_children == 1
            assert kernel.loop.dispatcher.scope.budget.active_coordinators == 1
            assert fold(read_session(kernel.session.base, spawn.child_session_id)).open_agent_runs
        else:
            assert owners == [kernel.session.id]
        release.set()
        with pytest.raises(TimeoutError if cause == "deadline" else asyncio.CancelledError):
            await work
        assert settled.is_set() and (tmp_path / "result.txt").read_text() == "retained"
        assert not kernel.loop.dispatcher.scope.budget.busy
        root_events = read_session(kernel.session.base, kernel.session.id)
        assert not fold(root_events).open_agent_runs and not fold(root_events).open_intents
        owner_events = read_session(kernel.session.base, owners[0])
        assert not fold(owner_events).open_agent_runs and not fold(owner_events).open_intents
        assert any(isinstance(e.event, ToolCallCancelled) for e in owner_events)
        assert len(provider.calls) == (2 if delegated else 1)
        if delegated:
            terminal, = [e.event for e in root_events if isinstance(e.event, CoordinationFinished)]
            assert terminal.status == "cancelled"
    finally:
        release.set()
        if work is not None:
            await asyncio.gather(work, return_exceptions=True)
        if entered.is_set():
            assert await asyncio.to_thread(settled.wait, 3)
        kernel.session.close()


async def test_cancelled_handoff_waiter_never_enters_file_worker(tmp_path, monkeypatch):
    from harness.events import ToolCallProposed
    from harness.fold import fold
    from harness.log import read_session
    from tests.test_handoff import source, specification

    kernel, provider = await source(tmp_path)
    provider.steps = ["new", "done"]
    record = kernel.handoffs.record(specification(kernel))
    entered, release, settled = (threading.Event() for _ in range(3))
    proposed = asyncio.Event()
    attempted = []
    blocker, method, args = mutation(provider.root, "write", target="B.txt", after="another owner")
    blocking_write = getattr(blocker, method)
    original_write, original_append = WriteFileTool._write, kernel.session.append

    def paused(*args):
        entered.set()
        try:
            assert release.wait(5)
            return blocking_write(*args)
        finally:
            settled.set()

    def observe_write(tool, path, content):
        attempted.append(content)
        return original_write(tool, path, content)

    def append(event):
        if isinstance(event, ToolCallProposed) and event.tool == "write_file":
            proposed.set()
        return original_append(event)

    monkeypatch.setattr(blocker, method, paused)
    monkeypatch.setattr(WriteFileTool, "_write", observe_write)
    monkeypatch.setattr(kernel.session, "append", append)
    owner = asyncio.create_task(blocker(args))
    waiting = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        waiting = asyncio.create_task(kernel.handoffs.run(record.id))
        await asyncio.wait_for(proposed.wait(), 3)
        await asyncio.sleep(.02)
        waiting.cancel()
        done, _ = await asyncio.wait({waiting}, timeout=.5)
        assert waiting in done and not owner.done()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not attempted and not fold(read_session(kernel.session.base, kernel.session.id)).open_agent_runs
        release.set()
        await asyncio.wait_for(owner, 3)
        assert (provider.root / "B.txt").read_text() == "another owner" and not attempted
    finally:
        release.set()
        await asyncio.gather(owner, *([waiting] if waiting else []), return_exceptions=True)
        assert await asyncio.to_thread(settled.wait, 3)
        kernel.session.close()


async def test_terminal_explains_mutation_cleanup_and_preserves_draft(tmp_path, monkeypatch):
    from textual.widgets import Input
    from harness.fold import fold
    from harness.log import read_session
    from harness.provider import FakeProvider, tool_call_turn
    from harness.types import ToolName
    from tests.test_handoff import permissions
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text

    entered, release, settled = (threading.Event() for _ in range(3))
    original = WriteFileTool._write

    def paused(tool, *args):
        entered.set()
        try:
            assert release.wait(8)
            return original(tool, *args)
        finally:
            settled.set()

    monkeypatch.setattr(WriteFileTool, "_write", paused)
    provider = FakeProvider([tool_call_turn("", ToolName("write_file"),
                                           {"file_path": "result.txt", "content": "retained"})])
    app = make_app(tmp_path / "sessions", provider=provider, native_tools=True, workspace_root=tmp_path,
                   permissions=permissions())
    try:
        async with app.run_test(size=(150, 45)) as pilot:
            try:
                await pilot.click("#prompt")
                app.query_one("#prompt", Input).value = "Write the file"
                await pilot.press("enter")
                assert await asyncio.to_thread(entered.wait, 3)
                app.query_one("#prompt", Input).value = "keep this draft"
                await pilot.press("escape")
                async with asyncio.timeout(3):
                    while "waiting for the started file change to finish" not in screen_text(app):
                        await pilot.pause(.02)
                assert app._interrupting and app.query_one("#prompt", Input).value == "keep this draft"
                assert fold(read_session(app.kernel.session.base, app.kernel.session.id)).open_agent_runs
                release.set()
                async with asyncio.timeout(3):
                    while app._interrupting:
                        await pilot.pause(.02)
                assert settled.is_set() and (tmp_path / "result.txt").read_text() == "retained"
                assert app.query_one("#prompt", Input).value == "keep this draft"
                assert "interrupted" in screen_text(app)
                assert not fold(read_session(app.kernel.session.base, app.kernel.session.id)).open_agent_runs
            finally:
                release.set()
    finally:
        release.set()
        if entered.is_set():
            assert await asyncio.to_thread(settled.wait, 3)
        app.kernel.session.close()
