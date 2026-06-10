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


async def test_collect_drops_thinking_and_flags_abrupt_end():
    from harness.provider import ThinkingDelta
    provider = FakeProvider([[ThinkingDelta(text="hmm"), TextDelta(text="answer")]])
    message, _, stop_reason = await collect(
        provider.complete(model=ModelId("fake"), messages=[], tools=())
    )
    assert message.text() == "answer"
    assert stop_reason == "unknown"  # no StreamStop arrived
