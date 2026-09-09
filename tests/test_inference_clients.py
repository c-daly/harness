"""Real SDK requests must release owned clients without disturbing other calls."""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import ClientSession, web

from harness.catalog import Catalog
from harness.errors import MalformedStreamError, ProviderError
from harness.inference import InferenceRequest, infer
from harness.messages import Message
from harness.provider import TextDelta
from harness.provider_litellm import CatalogProvider, LiteLLMProvider
from harness.types import ModelId


def request(text="ok", **kwargs):
    return InferenceRequest(model=ModelId("local"), purpose="conversation",
        messages=(Message.user_text(text),), **kwargs)


@pytest.fixture
async def endpoint(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm
    from litellm.caching.llm_caching_handler import LLMClientCache

    # Isolate this test's SDK cache; production must never sweep a global cache.
    cache = LLMClientCache()
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", cache)
    clients, sessions = [], []
    original = httpx.AsyncClient.__init__
    original_session = ClientSession.__init__

    def track(self, *args, **kwargs):
        original(self, *args, **kwargs)
        clients.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", track)

    def track_session(self, *args, **kwargs):
        original_session(self, *args, **kwargs)
        sessions.append(self)

    monkeypatch.setattr(ClientSession, "__init__", track_session)
    waiting, release, streaming = asyncio.Event(), asyncio.Event(), asyncio.Event()
    bodies, credentials = [], []

    async def chat(req):
        data = await req.json()
        bodies.append(data)
        credentials.append(req.headers.get("Authorization"))
        content = data["messages"][-1]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        if text == "error":
            return web.json_response({"error": {"message": "fixture failure"}}, status=503)
        if text == "wait":
            waiting.set()
            await release.wait()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(req)

        async def send(delta, finish=None):
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                "model": data["model"], "choices": [{"index": 0, "delta": delta,
                                                       "finish_reason": finish}]}
            await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())

        if text.endswith("tool"):
            await send({"tool_calls": [{"index": 0, "id": "partial-write", "type": "function", "function": {
                "name": "write_file", "arguments": '{"file_path":"B.txt","content":"partial"}'}}]})
        else:
            await send({"content": "x" * 500 if text == "large" else "ok"})
        if text.startswith("eof-"):
            return response  # Valid HTTP EOF, but no provider finish_reason.
        if text.startswith("disconnect-"):
            req.transport.abort()  # A severed HTTP stream, also without a finish_reason.
            return response
        if text == "stream":
            streaming.set()
            await release.wait()
        await send({}, "tool_calls" if text.endswith("tool") else "stop")
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    async def disconnected(req):
        try:
            return await chat(req)
        except ConnectionResetError:
            return web.Response(status=499)  # expected after client cancellation

    app.router.add_post("/v1/chat/completions", disconnected)
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    url = f"http://127.0.0.1:{port}/v1"
    provider = CatalogProvider(Catalog(entries={"local": {
        "route": "openai/test-model", "api_base": url}}))
    try:
        yield SimpleNamespace(provider=provider, clients=clients, cache=cache, url=url,
                              sessions=sessions, waiting=waiting, streaming=streaming,
                              release=release, bodies=bodies, credentials=credentials)
    finally:
        release.set()
        # Failed assertions must not leave test-created SDK resources alive.
        for client in clients:
            await client.aclose()
        for session in sessions:
            await session.close()
        await runner.cleanup()


def assert_closed(endpoint):
    assert endpoint.clients and all(c.is_closed for c in endpoint.clients)
    assert all(s.closed for s in endpoint.sessions)


async def test_repeated_deadlines_leave_no_owned_clients_or_sdk_cache_entries(endpoint):
    for index in range(4):
        result = await infer(endpoint.provider, request(timeout_seconds=10 + index / 100))
        assert result.message.text() == "ok"
        assert_closed(endpoint)
    assert not endpoint.cache.cache_dict
    assert len(endpoint.bodies) == 4


@pytest.mark.parametrize("failure", ["eof-text", "eof-tool", "disconnect-text", "disconnect-tool"])
async def test_missing_provider_finish_cannot_complete_text_or_tool_proposals(endpoint, failure):
    # Real SDK normalization used to fabricate a successful terminal on EOF,
    # even for syntactically complete tool arguments. No result may escape.
    with pytest.raises(MalformedStreamError, match="without a provider finish reason"):
        await infer(endpoint.provider, request(failure, tool_choice="auto"))
    assert len(endpoint.bodies) == 1
    assert_closed(endpoint)


async def test_real_provider_tool_finish_remains_usable(endpoint):
    result = await infer(endpoint.provider, request("complete-tool", tool_choice="auto"))
    assert result.stop_reason == "tool_use"
    assert result.message.tool_calls()[0].args == {"file_path": "B.txt", "content": "partial"}
    assert_closed(endpoint)


@pytest.mark.parametrize("failure", ["error", "large"])
async def test_failed_and_oversized_responses_close_clients(endpoint, failure):
    with pytest.raises(ProviderError):
        await infer(endpoint.provider, request(failure, max_output_bytes=256))
    assert_closed(endpoint)
    assert len(endpoint.bodies) == 1  # SDK retries cannot escape the task budget.


@pytest.mark.parametrize("stage", ["wait", "stream"])
async def test_cancellation_closes_clients_before_returning(endpoint, stage):
    task = asyncio.create_task(infer(endpoint.provider, request(stage)))
    ready = endpoint.waiting if stage == "wait" else endpoint.streaming
    try:
        await asyncio.wait_for(ready.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_closed(endpoint)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_finishing_one_request_keeps_concurrent_client_alive(endpoint):
    slow = asyncio.create_task(infer(endpoint.provider, request("wait")))
    try:
        await asyncio.wait_for(endpoint.waiting.wait(), 5)
        slow_clients = list(endpoint.clients)
        assert (await infer(endpoint.provider, request())).message.text() == "ok"
        assert all(not c.is_closed for c in slow_clients)
        assert all(c.is_closed for c in endpoint.clients if c not in slow_clients)
        endpoint.release.set()
        assert (await slow).message.text() == "ok"
        assert_closed(endpoint)
    finally:
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)


async def test_deadline_closes_clients(endpoint):
    with pytest.raises((TimeoutError, ProviderError)):
        await infer(endpoint.provider, request("wait", timeout_seconds=0.25))
    assert endpoint.waiting.is_set()
    assert_closed(endpoint)


async def test_anyio_cancellation_closes_clients(endpoint):
    import anyio

    async with anyio.create_task_group() as group:
        group.start_soon(infer, endpoint.provider, request("stream"))
        await asyncio.wait_for(endpoint.streaming.wait(), 5)
        group.cancel_scope.cancel()
    assert_closed(endpoint)


async def test_stream_close_failure_still_closes_owned_transport(endpoint, monkeypatch):
    import litellm

    class BadStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            raise RuntimeError("fixture close failure")

    async def completion(**kwargs):
        return BadStream()

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(RuntimeError, match="fixture close failure"):
        await infer(endpoint.provider, request())
    assert_closed(endpoint)


async def test_caller_owned_transport_is_not_closed_or_replaced(endpoint, monkeypatch):
    import litellm
    shared = httpx.AsyncClient()
    monkeypatch.setattr(litellm, "aclient_session", shared)
    assert (await infer(endpoint.provider, request())).message.text() == "ok"
    assert not shared.is_closed
    assert endpoint.clients == [shared]
    assert (await infer(endpoint.provider, request())).message.text() == "ok"
    assert not shared.is_closed


@pytest.mark.parametrize("legacy", [False, True])
async def test_direct_endpoint_uses_configured_key_and_closes(endpoint, monkeypatch, legacy):
    monkeypatch.setenv("HARNESS_FIXTURE_KEY", "fixture-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LiteLLMProvider(api_base=endpoint.url, api_key_env="HARNESS_FIXTURE_KEY")
    req = request().model_copy(update={"model": ModelId("openai/test-model")})
    if legacy:
        async with aclosing(provider.complete(model=req.model, messages=req.messages)) as stream:
            assert [c async for c in stream]
    else:
        assert (await infer(provider, req)).message.text() == "ok"
    assert endpoint.credentials == ["Bearer fixture-key"]
    assert_closed(endpoint)


@pytest.mark.parametrize("kind", ["direct", "catalog", "literal"])
async def test_legacy_stream_close_reaches_inner_generator(monkeypatch, kind):
    closed = []

    async def source(**kwargs):
        try:
            yield TextDelta("first")
            yield TextDelta("second")
        finally:
            closed.append(True)

    monkeypatch.setattr("harness.provider_litellm._acomplete", source)
    provider = LiteLLMProvider() if kind == "direct" else CatalogProvider(Catalog(entries={
        "local": {"route": "openai/test-model", "api_base": "http://localhost:8080/v1"}}))
    model = "unknown/literal" if kind == "literal" else "local"
    async with aclosing(provider.complete(model=ModelId(model), messages=[])) as stream:
        assert await anext(stream) == TextDelta("first")
    assert closed == [True]
