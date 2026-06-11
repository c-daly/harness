# tests/test_loop.py
import asyncio

from harness.events import CustomEvent, ErrorRaised, UserMessage
from harness.hooks import HookBus, Inject, Emit, LifecyclePoint
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.loop import AgentLoop
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
