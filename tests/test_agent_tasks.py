"""Task completion is distinct from model termination and acceptance evidence."""

import asyncio

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.cli import build_kernel
from harness.fold import fold
from harness.log import read_session
from harness.provider import FakeProvider, StreamStop, TextDelta, ToolCallDelta, text_turn, tool_call_turn
from harness.tools import ToolSpec
from harness.types import CallId, ModelId, ToolName


async def test_iteration_exhaustion_is_incomplete_and_criteria_remain(tmp_path):
    provider = FakeProvider([tool_call_turn("again", ToolName("missing"), {})])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"))
    try:
        await kernel.loop.start()
        task = AgentTask(prompt="finish the task", acceptance_criteria=("tests pass",),
                         limits=TaskLimits(max_iterations=1))
        result = await kernel.loop.run_task(task)
        assert result.status == "incomplete" and result.reason == "iteration_limit"
        assert result.acceptance == "unverified"
        assert result.remaining_criteria == task.acceptance_criteria
        assert "max iterations" in result.read_text(kernel.session.blobs)
        events = read_session(tmp_path, kernel.session.id)
        state = fold(events)
        assert state.agent_runs[result.run_id] == result
        assert not state.open_agent_runs
    finally:
        kernel.session.close()


async def test_token_cutoff_does_not_complete_agent_or_execute_partial_tools(tmp_path):
    invoked = []

    class SideEffect:
        spec = ToolSpec(name=ToolName("write"), description="", parameters={})

        async def __call__(self, args):
            invoked.append(args)
            return "written"

    provider = FakeProvider([[TextDelta("partial answer"),
                              ToolCallDelta(0, CallId("partial"), ToolName("write"), "{}"),
                              StreamStop("max_tokens")]])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"))
    kernel.registry.register(SideEffect())
    try:
        await kernel.loop.start()
        result = await kernel.loop.run_task(AgentTask(prompt="solve it"))
        assert result.status == "incomplete" and result.reason == "max_tokens"
        assert result.read_text(kernel.session.blobs) == "partial answer"
        assert not invoked
        events = read_session(tmp_path, kernel.session.id)
        assert not any(e.event.type == "tool_call_proposed" for e in events)
        cancelled = next(e.seq for e in events if e.event.type == "tool_call_cancelled")
        finished = next(e.seq for e in events if e.event.type == "agent_run_finished")
        assert cancelled < finished
        assert kernel.loop.history == fold(read_session(tmp_path, kernel.session.id)).messages
    finally:
        kernel.session.close()


async def test_completed_run_is_not_self_certified_acceptance(tmp_path):
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([text_turn("all tests pass")]),
                          model=ModelId("fake"))
    try:
        await kernel.loop.start()
        progress = []
        task = AgentTask(prompt="implement", acceptance_criteria=("held-out tests pass",))
        result = await kernel.loop.run_task(task, on_progress=progress.append)
        assert result.status == "completed" and result.acceptance == "unverified"
        assert result.remaining_criteria == task.acceptance_criteria
        assert progress and all(p.run_id == result.run_id for p in progress)
        events = read_session(tmp_path, kernel.session.id)
        model = next(e.event for e in events if e.event.type == "model_call_completed")
        assert model.task_id == task.id and model.agent_run_id == result.run_id
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_cancel_and_deadline_record_terminal_outcome_after_cleanup(tmp_path, mode):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Waiting:
        async def infer(self, request):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

    kernel = build_kernel(base_dir=tmp_path, provider=Waiting(), model=ModelId("fake"))
    try:
        await kernel.loop.start()
        task = asyncio.create_task(kernel.loop.run_task(AgentTask(
            prompt="wait", limits=TaskLimits(timeout_seconds=0.1 if mode == "deadline" else 30))))
        await entered.wait()
        if mode == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if mode == "cancel" else TimeoutError):
            await task
        assert closed.is_set()
        state = fold(read_session(tmp_path, kernel.session.id))
        result, = state.agent_runs.values()
        assert result.status == ("cancelled" if mode == "cancel" else "incomplete")
        assert not state.open_agent_runs and not state.open_model_intents
    finally:
        kernel.session.close()


async def test_parent_records_incomplete_child_instead_of_success(tmp_path):
    # A child uses the default 20 iterations; its parent must receive an explicit
    # failure marker that existing coordination strategies will not accept as success.
    provider = FakeProvider([tool_call_turn("again", ToolName("missing"), {}) for _ in range(20)])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"))
    try:
        await kernel.loop.start()
        text = await kernel.runner.run(prompt="finish", model=None, parent=kernel.session)
        assert text.startswith("[subagent error] incomplete")
        events = read_session(tmp_path, kernel.session.id)
        terminal = next(e.event for e in events if e.event.type == "subagent_finished")
        assert terminal.status == "incomplete"
    finally:
        kernel.session.close()


async def test_child_output_cap_cannot_remove_incomplete_marker(tmp_path):
    from harness.frontmatter import AgentDef

    provider = FakeProvider([[TextDelta("partial answer"), StreamStop("max_tokens")]])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"))
    kernel.runner.agents["terse"] = AgentDef(name="terse", description="", body="",
                                            max_output_chars=1)
    try:
        await kernel.loop.start()
        text = await kernel.runner.run(prompt="work", model=None, parent=kernel.session, agent="terse")
        assert text.startswith("[subagent error] incomplete (max_tokens): p")
        assert text.endswith("[truncated]")
    finally:
        kernel.session.close()


async def test_failure_records_one_safe_task_outcome(tmp_path):
    class Broken:
        async def infer(self, request):
            raise RuntimeError("sensitive runtime detail")
            yield

    kernel = build_kernel(base_dir=tmp_path, provider=Broken(), model=ModelId("fake"))
    try:
        await kernel.loop.start()
        with pytest.raises(RuntimeError, match="sensitive"):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        events = read_session(tmp_path, kernel.session.id)
        result, = [e.event.result for e in events if e.event.type == "agent_run_finished"]
        assert result.status == "failed" and result.reason == "RuntimeError"
        assert not fold(events).open_agent_runs
    finally:
        kernel.session.close()


async def test_terminal_write_failure_does_not_publish_contradictory_outcome(tmp_path, monkeypatch):
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([text_turn("done")]),
                          model=ModelId("fake"))
    try:
        await kernel.loop.start()
        original = kernel.session.append
        attempts = []

        def append(event):
            if event.type == "agent_run_finished":
                attempts.append(event.result.status)
                raise OSError("disk failure")
            return original(event)

        monkeypatch.setattr(kernel.session, "append", append)
        with pytest.raises(OSError, match="disk failure"):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert attempts == ["completed"]
        assert len(fold(read_session(tmp_path, kernel.session.id)).open_agent_runs) == 1
    finally:
        kernel.session.close()


async def test_concurrent_task_is_rejected_before_writing_or_disturbing_active_run(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def infer(self, request):
            entered.set()
            await release.wait()
            for chunk in text_turn("done"):
                yield chunk

    kernel = build_kernel(base_dir=tmp_path, provider=Waiting(), model=ModelId("fake"))
    try:
        await kernel.loop.start()
        first = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="first")))
        await entered.wait()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await kernel.loop.run_task(AgentTask(prompt="second"))
        finally:
            release.set()
            result = await first
        assert result.status == "completed"
        events = read_session(tmp_path, kernel.session.id)
        assert [e.event.text for e in events if e.event.type == "user_message"] == ["first"]
        assert len(fold(events).agent_runs) == 1
    finally:
        kernel.session.close()


async def test_nested_agent_run_links_to_parent_and_inference_sees_criteria(tmp_path):
    seen = []

    class Delegating(FakeProvider):
        async def complete(self, **kwargs):
            seen.extend(m.text() for m in kwargs["messages"] if m.role == "system")
            async for chunk in super().complete(**kwargs):
                yield chunk

    provider = Delegating([
        tool_call_turn("delegate", ToolName("dispatch_agent"), {"prompt": "child work"}),
        text_turn("child result"), text_turn("parent result"),
    ])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"))
    try:
        await kernel.loop.start()
        result = await kernel.loop.run_task(AgentTask(
            prompt="work", acceptance_criteria=("check the held-out fixture",)))
        parent_events = read_session(tmp_path, kernel.session.id)
        spawn, = [e.event for e in parent_events if e.event.type == "subagent_spawned"]
        child_events = read_session(tmp_path, spawn.child_session_id)
        start, = [e.event for e in child_events if e.event.type == "agent_run_started"]
        assert start.parent_run_id == result.run_id
        assert start.run_id != result.run_id
        assert any("check the held-out fixture" in text for text in seen)
        assert not fold(child_events).open_agent_runs
    finally:
        kernel.session.close()
