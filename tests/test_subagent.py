import asyncio

from harness.events import ErrorRaised, SubagentFinished, SubagentSpawned
from harness.frontmatter import AgentDef
from harness.hooks import HookBus
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.messages import ToolResultBlock
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.session import Session
from harness.subagent import DispatchAgentTool, SubagentRunner
from harness.tools import ToolRegistry, ToolSpec
from harness.types import ModelId, SessionId, ToolName


def _runner(tmp_path, provider):
    return SubagentRunner(
        base=tmp_path,
        provider=provider,
        registry=ToolRegistry(),
        hooks=HookBus(),
        resolver=HeadlessResolver(),
        default_model=ModelId("fake"),
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


async def test_teardown_failure_still_returns_result(tmp_path, monkeypatch):
    from harness.loop import AgentLoop

    async def broken_end(self):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(AgentLoop, "end", broken_end)
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = _runner(tmp_path, FakeProvider([text_turn("the answer")]))
    result = await runner.run(prompt="work", model=None, parent=parent)
    parent.close()
    assert result == "the answer"
    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    assert any(isinstance(e, SubagentFinished) and e.status == "ok" for e in events)
    assert any(isinstance(e, ErrorRaised) and e.where == "subagent:teardown" for e in events)


async def test_child_model_calls_carry_pricing(tmp_path):
    from harness.events import ModelCallCompleted

    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = SubagentRunner(
        base=tmp_path,
        provider=FakeProvider([text_turn("done")]),
        registry=ToolRegistry(),
        hooks=HookBus(),
        resolver=HeadlessResolver(),
        default_model=ModelId("fake"),
        pricing={"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
    )
    await runner.run(prompt="work", model=None, parent=parent)
    parent.close()
    envs = read_session(tmp_path, SessionId("parent"))
    child_id = next(
        e.event.child_session_id for e in envs if type(e.event).__name__ == "SubagentSpawned"
    )
    child = read_session(tmp_path, child_id)
    completed = [e.event for e in child if isinstance(e.event, ModelCallCompleted)]
    assert completed[0].pricing["input_cost_per_token"] == 1e-6


async def test_agent_definition_parametrizes_subagent(tmp_path):
    """Curator AgentDef narrows registry to echo only; calling forbidden tool yields error result."""

    class EchoTool:
        spec = ToolSpec(
            name=ToolName("echo"),
            description="Echo back",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )

        async def __call__(self, args: dict) -> str:
            return args["text"]

    class ForbiddenTool:
        spec = ToolSpec(
            name=ToolName("forbidden"),
            description="Forbidden",
            parameters={"type": "object", "properties": {}},
        )

        async def __call__(self, args: dict) -> str:  # pragma: no cover
            return "should not reach here"

    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(ForbiddenTool())

    curator = AgentDef(
        name="curator",
        description="d",
        body="You are the curator.",
        tools=("echo",),
        model=None,
    )
    provider = FakeProvider(
        [
            # turn 1: child calls the forbidden tool
            tool_call_turn("thinking", ToolName("forbidden"), {}),
            # turn 2: child returns final answer after seeing the error result
            text_turn("curator done"),
        ]
    )
    runner = SubagentRunner(
        base=tmp_path,
        provider=provider,
        registry=registry,
        hooks=HookBus(),
        resolver=HeadlessResolver(),
        default_model=ModelId("fake"),
        agents={"curator": curator},
    )
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    result = await runner.run(prompt="go", model=None, parent=parent, agent="curator")
    parent.close()
    assert result == "curator done"

    # First call messages[0] is the system message — assert curator prompt used
    first_call_messages = provider.calls[0]
    assert "You are the curator." in first_call_messages[0].text()

    # Second call messages must include a tool-result with is_error=True (forbidden call failed)
    second_call_messages = provider.calls[1]
    error_results = [
        block
        for msg in second_call_messages
        for block in msg.blocks
        if isinstance(block, ToolResultBlock) and block.is_error
    ]
    assert error_results, "expected an is_error tool result for the forbidden tool call"


async def test_unknown_agent_is_error_result(tmp_path):
    """Dispatching with an unknown agent name returns an error string; no SubagentSpawned."""
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    runner = SubagentRunner(
        base=tmp_path,
        provider=FakeProvider([]),
        registry=ToolRegistry(),
        hooks=HookBus(),
        resolver=HeadlessResolver(),
        default_model=ModelId("fake"),
        agents={"curator": AgentDef(name="curator", description="d", body="x")},
    )
    result = await runner.run(prompt="go", model=None, parent=parent, agent="nope")
    parent.close()
    assert "[subagent error]" in result
    assert "nope" in result
    assert "curator" in result  # available agents listed
    events = [e.event for e in read_session(tmp_path, SessionId("parent"))]
    assert not any(isinstance(e, SubagentSpawned) for e in events)
