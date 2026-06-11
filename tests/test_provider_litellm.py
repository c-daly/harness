# tests/test_provider_litellm.py
from types import SimpleNamespace

import pytest

from harness.errors import AuthFailed, RateLimited
from harness.messages import Message, Role, TextBlock, ThinkingBlock, ToolCallBlock
from harness.provider import StreamStop, TextDelta, ThinkingDelta, ToolCallDelta, UsageReport
from harness.provider_litellm import (
    LiteLLMProvider,
    _messages_to_openai,
    _normalize_chunk,
    map_exception,
)
from harness.types import CallId, ToolName


def _chunk(**delta_fields):
    delta = SimpleNamespace(**{"content": None, "tool_calls": None, "reasoning_content": None, **delta_fields})
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


def test_text_delta_normalizes():
    chunks = _normalize_chunk(_chunk(content="hello"))
    assert chunks == [TextDelta(text="hello")]


def test_tool_call_first_and_continuation_fragments():
    # Use chr() to avoid quote escaping issues in test file
    first_args = chr(123) + chr(34) + "comm"     # {"comm
    cont_args = "and" + chr(34) + ": " + chr(34) + "ls" + chr(34) + chr(125)  # and": "ls"}
    first = _chunk(tool_calls=[SimpleNamespace(
        index=0, id="call_abc",
        function=SimpleNamespace(name="bash", arguments=first_args), type="function",
    )])
    cont = _chunk(tool_calls=[SimpleNamespace(
        index=0, id=None, function=SimpleNamespace(name=None, arguments=cont_args), type=None,
    )])
    [d1] = _normalize_chunk(first)
    [d2] = _normalize_chunk(cont)
    assert d1 == ToolCallDelta(index=0, call_id=CallId("call_abc"), tool=ToolName("bash"), args_json=first_args)
    assert d2 == ToolCallDelta(index=0, call_id=None, tool=None, args_json=cont_args)


def test_finish_reason_maps_to_stream_stop():
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None, reasoning_content=None), finish_reason="length")],
        usage=None,
    )
    assert _normalize_chunk(chunk) == [StreamStop(stop_reason="max_tokens")]


def test_usage_chunk_with_cache_fields():
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=7,
        cache_read_input_tokens=80, cache_creation_input_tokens=20,
    )
    chunk = SimpleNamespace(choices=[], usage=usage)
    [report] = _normalize_chunk(chunk)
    assert isinstance(report, UsageReport)
    assert report.usage.input_tokens == 100 and report.usage.cache_read_tokens == 80


def test_message_translation_round():
    messages = [
        Message.system_text("be brief"),
        Message.user_text("run ls", cache_hint=True),
        Message(role=Role.ASSISTANT, blocks=(
            ThinkingBlock(text="hmm", provider_extras={"signature": "s1"}),
            TextBlock(text="on it"),
            ToolCallBlock(call_id=CallId("c1"), tool=ToolName("bash"), args={"command": "ls"}),
        )),
        Message.tool_result(CallId("c1"), text="file.txt"),
    ]
    out = _messages_to_openai(messages)
    assert out[0] == {"role": "system", "content": [{"type": "text", "text": "be brief"}]}
    assert out[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assistant = out[2]
    assert assistant["tool_calls"][0]["id"] == "c1"
    assert assistant["tool_calls"][0]["function"]["name"] == "bash"
    assert assistant["thinking_blocks"][0]["signature"] == "s1"
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "file.txt"}


def test_image_blocks_rejected_for_now():
    from harness.blobs import BlobRef
    from harness.errors import ProviderError
    from harness.messages import ImageBlock
    msg = Message(role=Role.USER, blocks=(
        ImageBlock(media_type="image/png", blob=BlobRef(sha256="a" * 64, size=1)),
    ))
    with pytest.raises(ProviderError, match="image"):
        _messages_to_openai([msg])


def test_exception_mapping():
    import litellm
    assert isinstance(map_exception(litellm.RateLimitError("x", "openai", "gpt")), RateLimited)
    assert isinstance(
        map_exception(litellm.AuthenticationError("x", "openai", "gpt")), AuthFailed
    )


def test_provider_satisfies_protocol():
    from harness.provider import ModelProvider
    assert isinstance(LiteLLMProvider(), ModelProvider)


def test_thinking_blocks_delta_signature_captured():
    chunk = _chunk(thinking_blocks=[SimpleNamespace(thinking=None, signature="sig42")])
    [d] = _normalize_chunk(chunk)
    assert d == ThinkingDelta(text="", signature="sig42")


def test_thinking_blocks_mirrored_text_not_duplicated():
    chunk = _chunk(
        reasoning_content="step",
        thinking_blocks=[SimpleNamespace(thinking="step", signature=None)],
    )
    deltas = _normalize_chunk(chunk)
    assert deltas == [ThinkingDelta(text="step")]
