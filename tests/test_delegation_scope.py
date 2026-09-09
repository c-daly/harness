"""Authority and budgets follow the active caller through nested coordination."""

import pytest
import asyncio

from harness.dispatcher import Dispatcher
from harness.events import SubagentSpawned
from harness.frontmatter import AgentDef
from harness.hooks import HookBus, ProposedToolCall
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.provider import text_turn, tool_call_turn
from harness.session import Session
from harness.subagent import DispatchAgentTool, SubagentRunner
from harness.tools import ToolRegistry, ToolSpec
from harness.types import CallId, ModelId, SessionId, ToolName


@pytest.mark.parametrize("coordinated", [False, True])
async def test_grandchild_inherits_restrictions_and_actual_parent(tmp_path, coordinated):
    executed = []

    class Forbidden:
        spec = ToolSpec(name=ToolName("forbidden"), description="", parameters={})

        async def __call__(self, args):
            executed.append(True)
            return "escaped"

    class Nested:
        async def complete(self, *, messages, tools, **kwargs):
            prompt = next(m.text() for m in reversed(messages) if m.role == "user")
            if messages[-1].role == "tool":
                chunks = text_turn("finished")
            elif prompt == "child":
                chunks = tool_call_turn("delegate", ToolName("dispatch_agent"),
                                        {"prompt": "grandchild", "agent": "wide"})
            else:
                chunks = tool_call_turn("try excluded tool", ToolName("forbidden"), {})
            for chunk in chunks:
                yield chunk

    registry = ToolRegistry()
    registry.register(Forbidden())
    with Session(tmp_path, SessionId("root")) as root:
        root.start()
        runner = SubagentRunner(
            base=tmp_path, provider=Nested(), registry=registry, hooks=HookBus(),
            resolver=HeadlessResolver(), default_model=ModelId("fake"), agents={
                "restricted": AgentDef(name="restricted", description="", body="",
                                       tools=("dispatch_agent",)),
                "wide": AgentDef(name="wide", description="", body="",
                                 tools=("dispatch_agent", "forbidden"),
                                 strategy="ensemble" if coordinated else None,
                                 experts=("fake",) if coordinated else ()),
            },
        )
        registry.register(DispatchAgentTool(runner=runner, parent=root))
        dispatcher = Dispatcher(session=root, registry=registry, hooks=runner.hooks,
                                resolver=runner.resolver)
        result = await dispatcher.dispatch_tool(ProposedToolCall(
            call_id=CallId("outer"), tool=ToolName("dispatch_agent"),
            args={"prompt": "child", "agent": "restricted"},
        ))
        assert not result.is_error
    assert executed == []
    roots = [e.event for e in read_session(tmp_path, root.id) if isinstance(e.event, SubagentSpawned)]
    assert len(roots) == 1
    child_events = read_session(tmp_path, roots[0].child_session_id)
    nested = [e for e in child_events if isinstance(e.event, SubagentSpawned)]
    assert len(nested) == 1
    grandchild = read_session(tmp_path, nested[0].event.child_session_id)
    assert grandchild[0].event.parent_session_id == roots[0].child_session_id
    assert grandchild[0].event.parent_seq == nested[0].seq


async def test_recursive_delegation_consumes_one_shared_budget(tmp_path):
    from harness.execution import ExecutionBudget, ExecutionLimits, ExecutionScope

    class Recursive:
        async def complete(self, *, messages, **kwargs):
            chunks = (text_turn("done") if messages[-1].role == "tool" else
                      tool_call_turn("delegate", ToolName("dispatch_agent"), {"prompt": "again"}))
            for chunk in chunks:
                yield chunk

    registry = ToolRegistry()
    budget = ExecutionBudget(ExecutionLimits(max_children=2, max_depth=8))
    with Session(tmp_path, SessionId("root")) as root:
        root.start()
        runner = SubagentRunner(base=tmp_path, provider=Recursive(), registry=registry,
                                hooks=HookBus(), resolver=HeadlessResolver(),
                                default_model=ModelId("fake"))
        registry.register(DispatchAgentTool(runner=runner, parent=root))
        dispatcher = Dispatcher(session=root, registry=registry, hooks=runner.hooks,
                                resolver=runner.resolver,
                                scope=ExecutionScope(root, registry, budget))
        await dispatcher.dispatch_tool(ProposedToolCall(
            call_id=CallId("outer"), tool=ToolName("dispatch_agent"), args={"prompt": "again"},
        ))
    assert budget.children == 2
    assert budget.active_children == 0
    assert len(list((tmp_path / "sessions").glob("*.jsonl"))) == 3
    assert any("budget" in path.read_text() for path in (tmp_path / "sessions").glob("*.jsonl"))


@pytest.mark.parametrize("tool_args", [{}, {"prompt": 1}])
async def test_invalid_arguments_never_reach_tool(tmp_path, tool_args):
    calls = []

    class Strict:
        spec = ToolSpec(name=ToolName("strict"), description="", parameters={
            "type": "object", "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        })

        async def __call__(self, args):
            calls.append(args)
            return "side effect"

    registry = ToolRegistry()
    registry.register(Strict())
    with Session(tmp_path, SessionId("root")) as root:
        root.start()
        dispatcher = Dispatcher(session=root, registry=registry, hooks=HookBus(),
                                resolver=HeadlessResolver())
        result = await dispatcher.dispatch_tool(ProposedToolCall(
            call_id=CallId("bad"), tool=ToolName("strict"), args=tool_args,
        ))
    assert calls == []
    assert result.is_error and "validation" in result.read_text()


async def test_shared_model_budget_counts_retries(tmp_path):
    from harness.errors import Overloaded
    from harness.execution import BudgetExceeded, ExecutionBudget, ExecutionLimits, ExecutionScope

    calls = []

    class Retry:
        async def complete(self, **kwargs):
            calls.append(True)
            raise Overloaded("busy")
            yield

    with Session(tmp_path, SessionId("root")) as root:
        root.start()
        registry = ToolRegistry()
        budget = ExecutionBudget(ExecutionLimits(max_model_calls=1))
        dispatcher = Dispatcher(session=root, registry=registry, hooks=HookBus(),
                                resolver=HeadlessResolver(), retry_delays=(0, 0),
                                scope=ExecutionScope(root, registry, budget))
        with pytest.raises(BudgetExceeded):
            await dispatcher.dispatch_model(provider=Retry(), model=ModelId("fake"),
                                            messages=[], tools=())
    assert calls == [True]
    events = read_session(tmp_path, root.id)
    assert events[-1].event.error_type == "BudgetExceeded"


async def test_cancellation_releases_shared_child_capacity(tmp_path):
    from harness.execution import current_scope

    entered = asyncio.Event()

    class Waiting:
        async def complete(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield

    with Session(tmp_path, SessionId("root")) as root:
        root.start()
        registry = ToolRegistry()
        runner = SubagentRunner(base=tmp_path, provider=Waiting(), registry=registry,
                                hooks=HookBus(), resolver=HeadlessResolver(),
                                default_model=ModelId("fake"))
        registry.register(DispatchAgentTool(runner=runner, parent=root))
        dispatcher = Dispatcher(session=root, registry=registry, hooks=runner.hooks,
                                resolver=runner.resolver)
        task = asyncio.create_task(dispatcher.dispatch_tool(ProposedToolCall(
            call_id=CallId("outer"), tool=ToolName("dispatch_agent"), args={"prompt": "wait"},
        )))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert current_scope.get() is None
        assert dispatcher.scope.budget.active_children == 0
    events = read_session(tmp_path, root.id)
    assert sum(e.event.type == "tool_call_cancelled" for e in events) == 1
    assert sum(e.event.type == "subagent_finished" for e in events) == 1


async def test_spawn_initialization_failure_has_terminal_fact(tmp_path, monkeypatch):
    import harness.subagent as module

    def broken(*args, **kwargs):
        raise OSError("injected child storage failure")

    with Session(tmp_path, SessionId("root")) as root:
        root.start()
        runner = SubagentRunner(base=tmp_path, provider=None, registry=ToolRegistry(),
                                hooks=HookBus(), resolver=HeadlessResolver(),
                                default_model=ModelId("fake"))
        monkeypatch.setattr(module, "Session", broken)
        result = await runner.run(prompt="go", model=None, parent=root)
        assert result.startswith("[subagent error]")
        assert runner._root_scopes[str(root.id)].budget.active_children == 0
    events = read_session(tmp_path, root.id)
    assert events[-1].event.type == "subagent_finished"
    assert events[-1].event.status == "error"
