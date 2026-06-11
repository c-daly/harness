from harness.messages import Message, Role
from harness.provider import (
    FakeProvider,
    ModelProvider,
    StreamStop,
    TextDelta,
    Usage,
    collect,
    text_turn,
    tool_call_turn,
)
from harness.types import CallId, ModelId, ToolName


async def test_fake_provider_replays_scripted_turns():
    provider = FakeProvider([text_turn("hello there")])
    chunks = [c async for c in provider.complete(
        model=ModelId("fake"), messages=[Message.user_text("hi")], tools=()
    )]
    assert any(isinstance(c, TextDelta) for c in chunks)
    assert isinstance(chunks[-1], StreamStop)


async def test_collect_builds_assistant_message_and_usage():
    provider = FakeProvider([text_turn("hello", usage=Usage(input_tokens=10, output_tokens=2))])
    stream = provider.complete(model=ModelId("fake"), messages=[], tools=())
    message, usage, stop_reason = await collect(stream)
    assert message.role == Role.ASSISTANT
    assert message.text() == "hello"
    assert usage.input_tokens == 10
    assert stop_reason == "end_turn"


async def test_collect_parses_tool_call_turn():
    provider = FakeProvider([
        tool_call_turn("checking", ToolName("bash"), {"command": "ls"}, call_id=CallId("c1")),
    ])
    message, _, stop_reason = await collect(provider.complete(model=ModelId("fake"), messages=[], tools=()))
    calls = message.tool_calls()
    assert len(calls) == 1
    assert calls[0].tool == "bash" and calls[0].args == {"command": "ls"}
    assert calls[0].call_id == "c1"
    assert stop_reason == "tool_use"


async def test_fake_provider_exhausted_raises():
    import pytest
    provider = FakeProvider([])
    with pytest.raises(RuntimeError, match="script exhausted"):
        async for _ in provider.complete(model=ModelId("fake"), messages=[], tools=()):
            pass


def test_fake_provider_satisfies_protocol():
    assert isinstance(FakeProvider([]), ModelProvider)


async def test_collect_flags_abrupt_end():
    from harness.messages import ThinkingBlock
    from harness.provider import ThinkingDelta
    provider = FakeProvider([[ThinkingDelta(text="hmm"), TextDelta(text="answer")]])
    message, _, stop_reason = await collect(
        provider.complete(model=ModelId("fake"), messages=[], tools=())
    )
    assert message.text() == "answer"
    assert stop_reason == "unknown"  # no StreamStop arrived
    thinking = [b for b in message.blocks if isinstance(b, ThinkingBlock)]
    assert len(thinking) == 1 and thinking[0].text == "hmm"


async def test_collect_accumulates_fragmented_tool_call():
    from harness.provider import StreamStop, ToolCallDelta, UsageReport
    chunks = [
        ToolCallDelta(index=0, call_id=CallId("c1"), tool=ToolName("bash"), args_json='{"comm'),
        ToolCallDelta(index=0, call_id=None, tool=None, args_json='and": "ls"}'),
        UsageReport(usage=Usage()),
        StreamStop(stop_reason="tool_use"),
    ]
    provider = FakeProvider([chunks])
    message, _, stop = await collect(provider.complete(model=ModelId("fake"), messages=[], tools=()))
    calls = message.tool_calls()
    assert calls[0].args == {"command": "ls"} and calls[0].call_id == "c1"
    assert stop == "tool_use"


async def test_collect_two_interleaved_tool_calls():
    from harness.provider import StreamStop, ToolCallDelta
    chunks = [
        ToolCallDelta(index=0, call_id=CallId("a"), tool=ToolName("t1"), args_json='{"x"'),
        ToolCallDelta(index=1, call_id=CallId("b"), tool=ToolName("t2"), args_json='{"y"'),
        ToolCallDelta(index=0, call_id=None, tool=None, args_json=': 1}'),
        ToolCallDelta(index=1, call_id=None, tool=None, args_json=': 2}'),
        StreamStop(stop_reason="tool_use"),
    ]
    provider = FakeProvider([chunks])
    message, _, _ = await collect(provider.complete(model=ModelId("fake"), messages=[], tools=()))
    calls = message.tool_calls()
    assert [c.args for c in calls] == [{"x": 1}, {"y": 2}]
    assert [c.call_id for c in calls] == ["a", "b"]


async def test_collect_builds_thinking_block():
    from harness.messages import ThinkingBlock
    from harness.provider import StreamStop, ThinkingDelta
    chunks = [
        ThinkingDelta(text="step one; "),
        ThinkingDelta(text="step two", signature="sig9"),
        TextDelta(text="done"),
        StreamStop(stop_reason="end_turn"),
    ]
    provider = FakeProvider([chunks])
    message, _, _ = await collect(provider.complete(model=ModelId("fake"), messages=[], tools=()))
    thinking = [b for b in message.blocks if isinstance(b, ThinkingBlock)]
    assert thinking[0].text == "step one; step two"
    assert thinking[0].provider_extras == {"signature": "sig9"}
    assert message.text() == "done"


async def test_collect_malformed_args_json_raises_provider_error():
    import pytest
    from harness.errors import MalformedStreamError
    from harness.provider import StreamStop, ToolCallDelta
    chunks = [
        ToolCallDelta(index=0, call_id=CallId("c"), tool=ToolName("t"), args_json="{not json"),
        StreamStop(stop_reason="tool_use"),
    ]
    provider = FakeProvider([chunks])
    with pytest.raises(MalformedStreamError, match="c"):
        await collect(provider.complete(model=ModelId("fake"), messages=[], tools=()))
