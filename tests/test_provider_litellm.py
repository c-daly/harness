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


def test_llamacpp_structured_context_overflow_is_actionable():
    import httpx
    import litellm
    from harness.errors import ContextOverflow

    body = {"error": {"type": "exceed_context_size_error", "n_prompt_tokens": 24283, "n_ctx": 8192,
                      "message": "private upstream details"}}
    response = httpx.Response(400, json=body, request=httpx.Request("POST", "http://127.0.0.1/v1"))
    exc = litellm.BadRequestError("OpenAIException - private upstream details", "local", "openai", response=response)
    error = map_exception(exc)
    assert isinstance(error, ContextOverflow) and not error.retryable
    assert "/compact" in str(error)
    assert "OpenAI" not in str(error) and "private" not in str(error)


def test_context_error_message_alone_does_not_reclassify_bad_request():
    import litellm
    from harness.errors import ProviderError
    exc = litellm.BadRequestError("exceed_context_size_error in invalid argument", "local", "openai")
    assert type(map_exception(exc)) is ProviderError


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


def test_openai_nested_cached_tokens_mapped():
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
    )
    chunk = SimpleNamespace(choices=[], usage=usage)
    [report] = _normalize_chunk(chunk)
    assert report.usage.cache_read_tokens == 80
    assert report.usage.input_tokens == 100


def test_missing_credentials_maps_to_auth_failed():
    import litellm
    exc = litellm.InternalServerError(
        "OpenAIException - Missing credentials. Please set OPENAI_API_KEY", "openai", "gpt"
    )
    assert isinstance(map_exception(exc), AuthFailed)


def test_litellm_wrapped_connection_refusal_remains_a_network_failure():
    import httpx
    import litellm
    from harness.errors import NetworkFailed, Overloaded

    # The real offline OpenAI-compatible route wraps SDK transport failures
    # in a synthetic HTTP 500, retaining the typed cause further down-chain.
    wrapped = litellm.InternalServerError("opaque server error", "openai", "fixture")
    wrapper = RuntimeError("opaque intermediate wrapper")
    wrapper.__cause__ = httpx.ConnectError("connection refused")
    wrapped.__cause__ = wrapper
    assert isinstance(map_exception(wrapped), NetworkFailed)
    # Error-message guesses must not misclassify a real server 500.
    actual = litellm.InternalServerError("connection refused by a downstream service", "openai", "fixture")
    assert isinstance(map_exception(actual), Overloaded)


def test_exception_cause_cycle_is_bounded():
    import litellm
    from harness.errors import Overloaded
    wrapped = litellm.InternalServerError("opaque error", "openai", "fixture")
    wrapper = RuntimeError("intermediate")
    wrapped.__cause__ = wrapper
    wrapper.__cause__ = wrapped
    assert isinstance(map_exception(wrapped), Overloaded)


@pytest.mark.parametrize("endpoint, origin", [
    ("http://localhost:8080/v1", "http://127.0.0.1:8080"),
    ("http://127.0.0.1:8080/private-path", "http://127.0.0.1:8080"),
    ("http://[::1]:8080/v1", "http://[::1]:8080"),
    ("http://private-user:private-password@localhost:8080/private-path?private-query#private-fragment",
     "http://127.0.0.1:8080"),
    ("https://127.0.0.2:8443/v1?private-query", "https://127.0.0.2:8443"),
    ("http://private-user:private-password@[::1]:8080/v1#private-fragment", "http://[::1]:8080"),
    ("http://localhost.:8080/v1", "http://127.0.0.1:8080"),
    ("http://localhost:private-port/v1", "the configured local endpoint"),
    ("http://127.0.0.1:99999/v1", "the configured local endpoint"),
    ("ftp://127.0.0.1/private-path", "the configured local endpoint"),
    ("http://[::1%25private-scope]:8080/v1", "http://[::1]:8080"),
])
@pytest.mark.parametrize("failure", ["connection", "wrapped", "timeout"])
def test_local_transport_error_identifies_server_without_sdk_body(endpoint, origin, failure):
    import httpx
    import litellm
    from harness.errors import NetworkFailed

    upstream = "OpenAIException: private request data"
    if failure == "wrapped":
        exc = litellm.InternalServerError(upstream, "openai", "fixture")
        exc.__cause__ = httpx.ConnectError("refused")
    elif failure == "timeout":
        exc = litellm.Timeout(upstream, "fixture", "openai")
    else:
        exc = litellm.APIConnectionError(upstream, "openai", "fixture")
    error = map_exception(exc, api_base=endpoint)
    assert isinstance(error, NetworkFailed) and error.retryable
    assert origin in str(error)
    assert "local model server" in str(error)
    assert "on-demand startup" in str(error)
    assert "OpenAI" not in str(error)
    assert "private" not in str(error)


@pytest.mark.parametrize("endpoint", [
    None, "https://api.example.com/v1", "http://localhost.example.com/v1",
    "http://localhost:password@api.example.com/v1", "http://192.168.1.2:8080/v1",
])
def test_remote_transport_error_is_not_described_as_local(endpoint):
    import litellm
    from harness.errors import NetworkFailed

    exc = litellm.APIConnectionError("upstream connection failure", "openai", "fixture")
    error = map_exception(exc, api_base=endpoint)
    assert isinstance(error, NetworkFailed)
    assert str(error) == str(exc)


@pytest.mark.parametrize("endpoint", [
    "http://localhost:8080/v1",
    "http://private-user:private-password@localhost:8080/v1?private-query#private-fragment",
])
async def test_catalog_inference_passes_endpoint_to_error_description(monkeypatch, endpoint):
    import litellm
    from harness.catalog import Catalog
    from harness.errors import NetworkFailed
    from harness.inference import InferenceRequest, infer
    from harness.provider_litellm import CatalogProvider
    from harness.types import ModelId

    async def unavailable(**kwargs):
        raise litellm.APIConnectionError("OpenAIException: connection refused", "openai", "fixture")

    monkeypatch.setattr(litellm, "acompletion", unavailable)
    provider = CatalogProvider(Catalog({"local": {
        "route": "openai/fixture", "api_base": endpoint,
    }}))
    with pytest.raises(NetworkFailed, match="Local inference connection failed") as caught:
        await infer(provider, InferenceRequest(model=ModelId("local"),
                    messages=(Message.user_text("hello"),), purpose="conversation"))
    assert "OpenAI" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_malformed_endpoint_diagnostic_omits_upstream_body():
    import litellm
    from harness.errors import NetworkFailed

    exc = litellm.APIConnectionError("OpenAIException: private request data", "openai", "fixture")
    error = map_exception(exc, api_base="http://[::1/private-path?private-query")
    assert isinstance(error, NetworkFailed) and error.retryable
    assert "configured endpoint" in str(error)
    assert "OpenAI" not in str(error) and "private" not in str(error)
