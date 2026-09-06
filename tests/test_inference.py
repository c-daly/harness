"""Owned inference is bounded, tool-free unless requested, and never an agent run."""

import asyncio

import pytest

from harness.catalog import Catalog
from harness.errors import ContextOverflow, ProviderError
from harness.inference import InferenceRequest, infer
from harness.messages import Message
from harness.provider import FakeProvider, StreamStop, TextDelta, ThinkingDelta, collect, text_turn
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId


def make_dispatcher(session, hooks=None):
    from harness.dispatcher import Dispatcher
    from harness.hooks import HookBus
    from harness.interaction import HeadlessResolver
    from harness.tools import ToolRegistry
    return Dispatcher(session=session, registry=ToolRegistry(), hooks=hooks or HookBus(),
                      resolver=HeadlessResolver(), retry_delays=())


def request(**kwargs):
    return InferenceRequest(model=ModelId("fake"), messages=(Message.user_text("classify"),),
                            purpose="intent", **kwargs)


async def test_input_limit_rejects_before_provider_call():
    provider = FakeProvider([text_turn("unused")])
    with pytest.raises(ContextOverflow):
        await infer(provider, request(max_input_bytes=2))
    assert provider.calls == []


async def test_output_limit_counts_reasoning_and_stops_before_callback():
    provider = FakeProvider([[ThinkingDelta("éé"), TextDelta("overflow")]])
    seen = []
    with pytest.raises(ProviderError, match="output"):
        await infer(provider, request(max_output_bytes=4), on_chunk=seen.append)
    assert seen == [ThinkingDelta("éé")]


async def test_timeout_and_cancellation_close_source():
    closed = asyncio.Event()

    class Hanging:
        async def infer(self, request):
            try:
                await asyncio.Event().wait()
                yield TextDelta("never")
            finally:
                closed.set()

    with pytest.raises(TimeoutError):
        await infer(Hanging(), request(timeout_seconds=0.01))
    assert closed.is_set()
    closed.clear()
    task = asyncio.create_task(infer(Hanging(), request()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_schema_is_validated_locally_and_tool_proposals_are_rejected():
    schema = {"type": "object", "properties": {"kind": {"const": "ack"}},
              "required": ["kind"], "additionalProperties": False}
    response = await infer(FakeProvider([text_turn('{"kind":"ack"}')]),
                           request(response_schema=schema))
    assert response.structured == {"kind": "ack"}
    with pytest.raises(ProviderError, match="schema"):
        await infer(FakeProvider([text_turn('{"kind":"done"}')]),
                    request(response_schema=schema))
    from harness.provider import tool_call_turn
    from harness.types import ToolName
    with pytest.raises(ProviderError, match="unadvertised"):
        await infer(FakeProvider([tool_call_turn("", ToolName("write_file"), {})]), request())


async def test_abrupt_stream_is_not_success_and_missing_usage_is_unknown():
    with pytest.raises(ProviderError, match="terminal"):
        await infer(FakeProvider([[TextDelta("partial")]]), request())
    response = await infer(FakeProvider([[TextDelta("ok"), StreamStop("end_turn")]]), request())
    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None
    _, usage, _ = await collect(FakeProvider([[TextDelta("ok")]]).complete(
        model=ModelId("fake"), messages=()))
    assert usage.input_tokens is None


async def test_inference_cannot_invoke_catalog_agent_even_with_legacy_alias(monkeypatch):
    monkeypatch.setattr("harness.catalog._cost_map_lookup", lambda _: {})
    catalog = Catalog({"fake": {"route": "codex/default", "backend": "codex"}})
    provider = CatalogProvider(catalog, codex=FakeProvider([text_turn("must not run")]))
    assert catalog.resolve("fake").execution_kind == "agent"
    with pytest.raises(ProviderError, match="agent runtime"):
        await infer(provider, request())
    assert provider.codex.calls == []


async def test_sampling_and_limits_reach_inference_adapter(monkeypatch):
    captured = []

    async def source(**kwargs):
        captured.append(kwargs)
        yield TextDelta("ok")
        yield StreamStop("end_turn")

    monkeypatch.setattr("harness.provider_litellm._acomplete", source)
    await infer(CatalogProvider(Catalog()), request(max_output_tokens=37, temperature=0))
    assert captured[0]["request"].max_output_tokens == 37
    assert captured[0]["request"].temperature == 0


def test_remote_schema_references_and_inconsistent_catalog_kinds_are_rejected(monkeypatch):
    with pytest.raises(ValueError, match="external"):
        request(response_schema={"$ref": "https://example.invalid/schema"})
    monkeypatch.setattr("harness.catalog._cost_map_lookup", lambda _: {})
    with pytest.raises(ValueError, match="execution_kind"):
        Catalog({"bad": {"route": "codex/default", "backend": "codex",
                         "execution_kind": "inference"}}).resolve("bad")


async def test_dispatch_checks_effective_runtime_after_routing(tmp_path, monkeypatch):
    from harness.hooks import HookBus, ProposedModelCall, Rewrite
    from harness.session import Session
    from harness.types import SessionId
    monkeypatch.setattr("harness.catalog._cost_map_lookup", lambda _: {})
    hooks = HookBus()
    hooks.register_dispatch("route", lambda action: Rewrite(action=ProposedModelCall(
        call_id=action.call_id, model=ModelId("agent"))))
    provider = CatalogProvider(Catalog({"agent": {"route": "codex/default", "backend": "codex"}}),
                               codex=FakeProvider([text_turn("never")]))
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        with pytest.raises(ProviderError, match="agent runtime"):
            await make_dispatcher(session, hooks).dispatch_inference(provider=provider, request=request())
    assert provider.codex.calls == []
    from harness.log import read_session
    terminal = [e.event.type for e in read_session(tmp_path, SessionId("s"))
                if e.event.type.startswith("model_call_")]
    assert terminal == ["model_call_proposed", "model_call_failed"]


async def test_external_agent_failure_is_not_retried(tmp_path):
    from harness.errors import NetworkFailed
    from harness.session import Session
    from harness.types import SessionId

    class Agent:
        execution_kind = "agent"
        calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            raise NetworkFailed("may have already performed work")
            yield

    provider = Agent()
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        dispatcher = make_dispatcher(session)
        dispatcher.retry_delays = (0, 0)
        with pytest.raises(NetworkFailed):
            await dispatcher.dispatch_model(provider=provider, model=ModelId("agent"),
                                            messages=[], tools=())
    assert provider.calls == 1


async def test_retry_delay_cannot_extend_whole_inference_deadline(tmp_path):
    from harness.errors import Overloaded
    from harness.session import Session
    from harness.types import SessionId

    class Busy:
        async def infer(self, request):
            raise Overloaded("busy")
            yield

    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        dispatcher = make_dispatcher(session)
        dispatcher.retry_delays = (10,)
        with pytest.raises(TimeoutError):
            await dispatcher.dispatch_inference(provider=Busy(), request=request(timeout_seconds=0.01))
        assert dispatcher.scope.budget.model_calls == 1


async def test_oversized_blob_rejected_before_read(tmp_path, monkeypatch):
    from harness.blobs import BlobRef
    from harness.session import Session
    from harness.types import CallId, SessionId
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        monkeypatch.setattr(session.blobs, "get", lambda _: pytest.fail("must not read huge blob"))
        req = InferenceRequest(model=ModelId("fake"), purpose="intent", max_input_bytes=2048,
                               messages=(Message.tool_result(CallId("a"),
                                         blob=BlobRef(sha256="a" * 64, size=10**9)),))
        with pytest.raises(ContextOverflow):
            await make_dispatcher(session).dispatch_inference(provider=FakeProvider([]), request=req)


async def test_sdk_receives_options_and_stream_is_closed(monkeypatch):
    import litellm
    from types import SimpleNamespace
    from harness.provider_litellm import LiteLLMProvider
    captured = []
    closed = []

    async def raw():
        try:
            yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                delta=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")])
        finally:
            closed.append(True)

    async def acompletion(**kwargs):
        captured.append(kwargs)
        return raw()

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    req = request(max_output_tokens=19, temperature=0,
                  response_schema={"type": "object", "required": ["ok"]})
    result = await infer(LiteLLMProvider(), req)
    assert result.structured == {"ok": True}
    assert captured[0]["max_tokens"] == 19 and captured[0]["temperature"] == 0
    assert captured[0]["num_retries"] == 0
    assert captured[0]["response_format"]["json_schema"]["schema"] == req.response_schema
    assert closed == [True]


def test_unknown_usage_and_partial_cost_survive_projection():
    from harness.events import Envelope, ModelCallCompleted, SessionStarted
    from harness.telemetry import index_envelopes, open_store_memory, run_rollup
    from harness.types import CallId, SessionId
    conn = open_store_memory()
    try:
        events = [SessionStarted(), ModelCallCompleted(
            call_id=CallId("known"), model=ModelId("fake"), message={},
            usage={"input_tokens": 1, "output_tokens": 2},
            pricing={"input_cost_per_token": 1, "output_cost_per_token": 1}),
            ModelCallCompleted(call_id=CallId("unknown"), model=ModelId("fake"), message={},
                               usage={"input_tokens": 10, "output_tokens": None},
                               pricing={"input_cost_per_token": 1, "output_cost_per_token": 1})]
        index_envelopes(conn, [Envelope(session_id=SessionId("s"), seq=i+1, ts=0, event=e)
                               for i, e in enumerate(events)])
        rollup = run_rollup(conn, "s")
        assert rollup["input_tokens"] == 11
        assert rollup["output_tokens"] is None and rollup["cost"] is None
    finally:
        conn.close()
