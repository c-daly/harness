import asyncio

from harness.events import SubagentFinished, SubagentSpawned
from harness.hooks import HookBus
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.session import Session
from harness.subagent import DispatchAgentTool, SubagentRunner
from harness.tools import ToolRegistry
from harness.types import ModelId, SessionId


def _runner(tmp_path, provider):
    return SubagentRunner(
        base=tmp_path, provider=provider, registry=ToolRegistry(),
        hooks=HookBus(), resolver=HeadlessResolver(), default_model=ModelId("fake"),
    )


async def test_subagent_runs_and_parent_records_lifecycle(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("child says hi")]))
    result = await runner.run(prompt="greet", model=None, parent=parent)
    parent.close()
    assert result == "child says hi"
    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    spawned = [e for e in events if isinstance(e, SubagentSpawned)]
    finished = [e for e in events if isinstance(e, SubagentFinished)]
    assert spawned and finished and finished[0].status == "ok"
    # child log exists with parent linkage
    child_envs = read_session(tmp_path, spawned[0].child_session_id)
    assert child_envs[0].event.parent_session_id == "parent"


async def test_child_failure_returns_typed_error_to_parent(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([]))  # exhausted script -> child errors
    result = await runner.run(prompt="doomed", model=None, parent=parent)
    parent.close()
    assert result.startswith("[subagent error]")
    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    assert any(isinstance(e, SubagentFinished) and e.status == "error" for e in events)


async def test_cancellation_cascades_and_is_recorded(tmp_path):
    class NeverProvider:
        async def complete(self, *, model, messages, tools=()):
            await asyncio.sleep(60)
            yield  # pragma: no cover

    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, NeverProvider())
    task = asyncio.create_task(runner.run(prompt="hang", model=None, parent=parent))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    parent.close()
    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    assert any(isinstance(e, SubagentFinished) and e.status == "cancelled" for e in events)


async def test_dispatch_agent_tool_wraps_runner(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("delegated done")]))
    tool = DispatchAgentTool(runner=runner, parent=parent)
    assert tool.spec.name == "dispatch_agent"
    result = await tool({"prompt": "do the thing"})
    parent.close()
    assert result == "delegated done"
