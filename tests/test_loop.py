# tests/test_loop.py
import asyncio

import pytest

from harness.events import CustomEvent, ErrorRaised, UserMessage
from harness.fold import fold
from harness.hooks import HookBus, Inject, Emit, LifecyclePoint
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.loop import AgentLoop
from harness.messages import Role, ToolResultBlock
from harness.provider import FakeProvider, StreamStop, TextDelta, ToolCallDelta, Usage, UsageReport, text_turn, tool_call_turn
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import ModelId, SessionId, ToolName


class Echo:
    spec = ToolSpec(name=ToolName("echo"), description="", parameters={})

    async def __call__(self, args):
        return args["text"]


class Gate:
    """Two concurrent callers must both arrive before either returns."""

    def __init__(self):
        self.barrier = asyncio.Barrier(2)

    @property
    def spec(self):
        return ToolSpec(name=ToolName("gate"), description="", parameters={})

    async def __call__(self, args):
        async with asyncio.timeout(2):
            await self.barrier.wait()
        return "through"


def _loop(tmp_path, provider, hooks=None, registry=None, max_iterations=10):
    session = Session(tmp_path, SessionId("s1"))
    reg = registry or ToolRegistry()
    if registry is None:
        reg.register(Echo())
    return session, AgentLoop(
        session=session, provider=provider, registry=reg,
        hooks=hooks or HookBus(), resolver=HeadlessResolver(),
        model=ModelId("fake"), system_prompt="be brief",
        max_iterations=max_iterations,
    )


async def test_text_only_turn(tmp_path):
    session, loop = _loop(tmp_path, FakeProvider([text_turn("hello")]))
    await loop.start()
    assert await loop.run_turn("hi") == "hello"
    session.close()
    events = [e.event for e in read_session(tmp_path, SessionId("s1"))]
    assert any(isinstance(e, UserMessage) and e.text == "hi" for e in events)


async def test_tool_round_trip(tmp_path):
    provider = FakeProvider([
        tool_call_turn("calling", ToolName("echo"), {"text": "pong"}),
        text_turn("the tool said pong"),
    ])
    session, loop = _loop(tmp_path, provider)
    await loop.start()
    result = await loop.run_turn("ping the tool")
    session.close()
    assert result == "the tool said pong"
    # the second model call saw the tool result in its transcript
    last_call = provider.calls[-1]
    assert any("pong" in str(b) for m in last_call for b in m.blocks)


async def test_parallel_tool_calls_in_one_turn_run_concurrently(tmp_path):
    gate = Gate()
    reg = ToolRegistry()
    reg.register(gate)
    # one assistant turn proposing TWO gate calls; both must run concurrently to pass the barrier
    from harness.types import new_call_id
    turn = [
        TextDelta(text="two"),
        ToolCallDelta(index=0, call_id=new_call_id(), tool=ToolName("gate"), args_json="{}"),
        ToolCallDelta(index=1, call_id=new_call_id(), tool=ToolName("gate"), args_json="{}"),
        UsageReport(usage=Usage()),
        StreamStop(stop_reason="tool_use"),
    ]
    provider = FakeProvider([turn, text_turn("both through")])
    session, loop = _loop(tmp_path, provider, registry=reg)
    await loop.start()
    assert await loop.run_turn("open both") == "both through"
    session.close()


async def test_max_iterations_guard(tmp_path):
    endless = [tool_call_turn("again", ToolName("echo"), {"text": "x"}) for _ in range(5)]
    session, loop = _loop(tmp_path, FakeProvider(endless), max_iterations=3)
    await loop.start()
    result = await loop.run_turn("loop forever")
    session.close()
    assert "max iterations" in result
    events = [e.event for e in read_session(tmp_path, SessionId("s1"))]
    assert any(isinstance(e, ErrorRaised) for e in events)


async def test_session_start_lifecycle_injects_and_emits(tmp_path):
    hooks = HookBus()
    hooks.register_lifecycle(
        "memory-brief", LifecyclePoint.SESSION_START,
        lambda ctx: (Inject(text="remember: prefer uv"),
                     Emit(namespace="memory", name="brief_served", data={"n": 1})),
    )
    provider = FakeProvider([text_turn("ok")])
    session, loop = _loop(tmp_path, provider, hooks=hooks)
    await loop.start()
    await loop.run_turn("hi")
    session.close()
    assert "prefer uv" in loop.system_prompt
    events = [e.event for e in read_session(tmp_path, SessionId("s1"))]
    custom = [e for e in events if isinstance(e, CustomEvent)]
    assert custom and custom[0].namespace == "memory"


async def test_session_end_contributions_are_processed(tmp_path):
    hooks = HookBus()
    hooks.register_lifecycle(
        "teardown", LifecyclePoint.SESSION_END,
        lambda ctx: (Emit(namespace="memory", name="flushed", data={"n": 2}),),
    )
    session, loop = _loop(tmp_path, FakeProvider([text_turn("ok")]), hooks=hooks)
    await loop.start()
    await loop.run_turn("hi")
    await loop.end()
    session.close()
    events = [e.event for e in read_session(tmp_path, SessionId("s1"))]
    custom = [e for e in events if isinstance(e, CustomEvent)]
    assert custom and custom[0].name == "flushed"
    assert events[-1].type == "session_ended"


async def test_end_twice_raises(tmp_path):
    import pytest
    session, loop = _loop(tmp_path, FakeProvider([text_turn("ok")]))
    await loop.start()
    await loop.end()
    with pytest.raises(RuntimeError, match="already called"):
        await loop.end()
    session.close()


async def test_dispatch_infrastructure_failure_logs_and_raises(tmp_path):
    import pytest
    provider = FakeProvider([tool_call_turn("go", ToolName("echo"), {"text": "x"})])
    session, loop = _loop(tmp_path, provider)
    await loop.start()

    async def broken(call):
        raise RuntimeError("disk on fire")

    loop.dispatcher.dispatch_tool = broken
    with pytest.raises(RuntimeError, match="disk on fire"):
        await loop.run_turn("do it")
    session.close()
    events = [e.event for e in read_session(tmp_path, SessionId("s1"))]
    assert any(
        isinstance(e, ErrorRaised) and e.where == "loop:tool_dispatch" for e in events
    )

async def test_loop_threads_on_chunk_to_dispatch(tmp_path):
    from harness.provider import TextDelta

    seen: list = []
    session, loop = _loop(tmp_path, FakeProvider([text_turn("hi back")]))
    loop.on_chunk = seen.append
    await loop.start()
    await loop.run_turn("hi")
    session.close()
    assert any(isinstance(c, TextDelta) for c in seen)


# ---- interrupt tests ----


class NeverProvider:
    """complete() waits forever without yielding."""

    async def complete(self, *, model, messages, tools=()):
        await asyncio.sleep(3600)
        if False:
            yield  # type: ignore[misc]


class StallTool:
    """Sleeps forever when called."""

    spec = ToolSpec(name=ToolName("stall"), description="", parameters={})

    async def __call__(self, args):
        await asyncio.sleep(3600)
        return "never"


def _interrupt_loop(tmp_path, provider, registry=None):
    """Build a (session, loop) pair for interrupt tests."""
    session = Session(tmp_path, SessionId("s2"))
    reg = registry or ToolRegistry()
    return session, AgentLoop(
        session=session,
        provider=provider,
        registry=reg,
        hooks=HookBus(),
        resolver=HeadlessResolver(),
        model=ModelId("fake"),
        system_prompt="be brief",
    )


def _read_envelopes_for(tmp_path):
    return read_session(tmp_path, SessionId("s2"))


_CANCELLED_TEXT = "(call did not complete)"


async def test_interrupt_during_model_is_benign(tmp_path):
    session, loop = _interrupt_loop(tmp_path, NeverProvider())
    await loop.start()

    task = asyncio.create_task(loop.run_turn("hi"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    loop.interrupt_turn()

    event_types = [e.event.type for e in _read_envelopes_for(tmp_path)]
    assert "user_interrupt" in event_types
    assert "tool_call_cancelled" not in event_types

    # resurrection: loop is still usable
    loop.provider = FakeProvider([text_turn("recovered")])
    assert await loop.run_turn("again") == "recovered"
    session.close()


async def test_interrupt_during_tool_gather_repairs_history(tmp_path):
    reg = ToolRegistry()
    reg.register(StallTool())

    session, loop = _interrupt_loop(
        tmp_path,
        FakeProvider([tool_call_turn("calling", ToolName("stall"), {}), text_turn("never reached")]),
        registry=reg,
    )
    await loop.start()

    task = asyncio.create_task(loop.run_turn("go"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    loop.interrupt_turn()

    tool_call_ids = {block.call_id for msg in loop.history for block in msg.tool_calls()}
    result_call_ids = {
        block.call_id for msg in loop.history if msg.role == Role.TOOL
        for block in msg.blocks if isinstance(block, ToolResultBlock)
    }
    assert tool_call_ids, "expected at least one tool call in history"
    assert tool_call_ids == result_call_ids, "all tool_use calls must be paired with results"

    for msg in loop.history:
        if msg.role == Role.TOOL:
            for block in msg.blocks:
                if isinstance(block, ToolResultBlock) and block.call_id in tool_call_ids:
                    assert block.text == _CANCELLED_TEXT
                    assert block.is_error is True

    event_types = [e.event.type for e in _read_envelopes_for(tmp_path)]
    assert "tool_call_cancelled" in event_types
    assert "user_interrupt" in event_types

    loop.provider = FakeProvider([text_turn("recovered")])
    assert await loop.run_turn("again") == "recovered"
    session.close()


async def test_interrupt_history_matches_fold_of_log(tmp_path):
    """After tool-gather interrupt + repair, fold(log) agrees with in-memory history."""
    reg = ToolRegistry()
    reg.register(StallTool())

    session, loop = _interrupt_loop(
        tmp_path,
        FakeProvider([tool_call_turn("calling", ToolName("stall"), {}), text_turn("never reached")]),
        registry=reg,
    )
    await loop.start()

    task = asyncio.create_task(loop.run_turn("go"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    loop.interrupt_turn()
    session.close()

    envelopes = _read_envelopes_for(tmp_path)
    folded = fold(envelopes)

    cancelled_in_history: dict[str, str] = {}
    for msg in loop.history:
        if msg.role == Role.TOOL:
            for block in msg.blocks:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    cancelled_in_history[block.call_id] = block.text or ""

    cancelled_in_fold: dict[str, str] = {}
    for msg in folded.messages:
        if msg.role == Role.TOOL:
            for block in msg.blocks:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    cancelled_in_fold[block.call_id] = block.text or ""

    assert cancelled_in_history, "expected cancelled calls in history"
    assert cancelled_in_history == cancelled_in_fold, (
        "in-memory history and fold of log must agree on cancelled tool results"
    )
    for text in cancelled_in_history.values():
        assert text == _CANCELLED_TEXT


async def test_interrupt_with_clean_history_only_records_interrupt(tmp_path):
    """Completed-turn loop: interrupt_turn appends only UserInterrupt."""
    session, loop = _interrupt_loop(tmp_path, FakeProvider([text_turn("done")]))
    await loop.start()
    await loop.run_turn("hello")

    loop.interrupt_turn()
    session.close()

    event_types = [e.event.type for e in _read_envelopes_for(tmp_path)]
    assert event_types.count("user_interrupt") == 1
    assert "tool_call_cancelled" not in event_types
