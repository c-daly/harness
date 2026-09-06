"""External-agent responses obey stream bounds before observation or persistence."""

import asyncio

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.errors import ProviderError
from harness.fold import fold
from harness.inference import InferenceRequest
from harness.log import read_session
from harness.messages import Message
from harness.provider import StreamStop, TextDelta, ThinkingDelta, ToolCallDelta, Usage, UsageReport
from harness.provider_litellm import CatalogProvider
from harness.session import Session
from harness.types import CallId, ModelId, SessionId, ToolName
from tests.test_inference import make_dispatcher


class ExternalAgent:
    execution_kind = "agent"

    def __init__(self, chunks=(), *, hang=False):
        self.chunks = chunks
        self.hang = hang
        self.calls = 0
        self.produced = 0
        self.entered = asyncio.Event()
        self.closed = False
        # Retain the iterator so GC cannot hide a missing aclose in catalog forwarding.
        self.stream = None

    def complete(self, **kwargs):
        self.calls += 1
        self.stream = self._source()
        return self.stream

    async def _source(self):
        try:
            self.entered.set()
            if self.hang:
                await asyncio.Event().wait()
            for chunk in self.chunks:
                self.produced += 1
                yield chunk
        finally:
            self.closed = True

    async def close(self):
        if self.stream is not None:
            await self.stream.aclose()


def route(backend, kind, monkeypatch):
    if kind == "direct":
        return backend
    monkeypatch.setattr("harness.catalog._cost_map_lookup", lambda _: {})
    catalog = Catalog({"external": {"route": f"{kind}/default", "backend": kind}})
    field = "claude_code" if kind == "claude-code" else kind
    return CatalogProvider(catalog, **{field: backend})


def request(**limits):
    return InferenceRequest(model=ModelId("external"), purpose="conversation",
                            messages=(Message.user_text("work"),), tool_choice="auto", **limits)


@pytest.mark.parametrize("kind", ["direct", "codex", "claude-code", "antigravity"])
@pytest.mark.parametrize("bad_chunk, limits", [
    (TextDelta("é" * 1024), {"max_output_bytes": 128}),
    (ThinkingDelta("", signature="é" * 1024), {"max_output_bytes": 128}),
    (ToolCallDelta(0, CallId("call"), ToolName("write"), '{"value":"' + "é" * 1024 + '"}'),
     {"max_output_bytes": 128}),
    (TextDelta(""), {"max_stream_chunks": 1}),
    (UsageReport(Usage(output_tokens=9)), {"max_output_tokens": 8}),
], ids=["text-bytes", "signature-bytes", "argument-bytes", "empty-frames", "reported-tokens"])
async def test_agent_limit_stops_before_callback_and_completion(tmp_path, monkeypatch, kind, bad_chunk, limits):
    first = TextDelta("ok")
    backend = ExternalAgent([first, bad_chunk, StreamStop("end_turn")])
    provider = route(backend, kind, monkeypatch)
    seen = []
    try:
        with Session(tmp_path, SessionId("limited")) as session:
            session.start()
            dispatcher = make_dispatcher(session)
            dispatcher.retry_delays = (0, 0)
            original_append = session.append

            def append(event):
                if event.type == "model_call_failed":
                    assert backend.closed, "agent must close before terminal failure is recorded"
                return original_append(event)

            monkeypatch.setattr(session, "append", append)
            with pytest.raises(ProviderError, match="limit"):
                await dispatcher.dispatch_response(provider=provider, request=request(**limits),
                                                   on_chunk=seen.append)
            assert backend.closed and backend.calls == 1
            assert backend.produced == 2 and seen == [first]
        events = read_session(tmp_path, SessionId("limited"))
        terminal = [e.event.type for e in events if e.event.type in
                    {"model_call_completed", "model_call_failed", "model_call_cancelled"}]
        assert terminal == ["model_call_failed"]
        assert not fold(events).open_model_intents
    finally:
        await backend.close()


@pytest.mark.parametrize("kind", ["direct", "codex", "claude-code", "antigravity"])
@pytest.mark.parametrize("mode", ["deadline", "cancel"])
async def test_agent_deadline_and_cancel_close_forwarded_source(tmp_path, monkeypatch, kind, mode):
    backend = ExternalAgent(hang=True)
    provider = route(backend, kind, monkeypatch)
    with Session(tmp_path, SessionId("waiting")) as session:
        session.start()
        dispatcher = make_dispatcher(session)
        task = asyncio.create_task(dispatcher.dispatch_response(
            provider=provider, request=request(timeout_seconds=0.01 if mode == "deadline" else 30)))
        try:
            await asyncio.wait_for(backend.entered.wait(), 1)
            if mode == "cancel":
                task.cancel()
            with pytest.raises(TimeoutError if mode == "deadline" else asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 0.3)
            assert task.done(), "the request deadline must finish the dispatch"
            assert backend.closed and backend.calls == 1
            events = read_session(tmp_path, session.id)
            assert not fold(events).open_model_intents
            terminal = [e.event.type for e in events if e.event.type in
                        {"model_call_completed", "model_call_failed", "model_call_cancelled"}]
            assert terminal == ["model_call_failed" if mode == "deadline" else "model_call_cancelled"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await backend.close()


@pytest.mark.parametrize("chunks", [
    [TextDelta("partial")],
    [StreamStop("end_turn"), StreamStop("end_turn")],
    [StreamStop("end_turn"), TextDelta("late")],
], ids=["missing-terminal", "duplicate-terminal", "late-content"])
async def test_malformed_agent_stream_is_not_persisted_as_completion(tmp_path, chunks):
    backend = ExternalAgent(chunks)
    with Session(tmp_path, SessionId("malformed")) as session:
        session.start()
        try:
            with pytest.raises(ProviderError, match="terminal"):
                await make_dispatcher(session).dispatch_response(provider=backend, request=request())
            assert backend.closed
            assert not any(e.event.type == "model_call_completed"
                           for e in read_session(tmp_path, session.id))
        finally:
            await backend.close()


@pytest.mark.parametrize("reported", [5, None])
async def test_bounded_catalog_agent_can_complete_with_known_or_unknown_usage(tmp_path, monkeypatch, reported):
    backend = ExternalAgent([TextDelta("answer"), UsageReport(Usage(output_tokens=reported)),
                             StreamStop("end_turn")])
    provider = route(backend, "codex", monkeypatch)
    with Session(tmp_path, SessionId("complete")) as session:
        session.start()
        try:
            result = await make_dispatcher(session).dispatch_response(
                provider=provider, request=request(max_stream_chunks=3, max_output_tokens=5))
            assert result.message.text() == "answer" and result.usage.output_tokens == reported
            assert backend.closed and backend.calls == 1
            complete, = [e.event for e in read_session(tmp_path, session.id)
                         if e.event.type == "model_call_completed"]
            assert complete.execution_kind == "agent"
        finally:
            await backend.close()


@pytest.mark.parametrize("limits, chunks, produced", [
    (TaskLimits(max_stream_chunks=2), [TextDelta("")] * 3, 3),
    (TaskLimits(max_response_bytes=128), [TextDelta("é" * 1024)], 1),
    (TaskLimits(max_output_tokens=8), [TextDelta("ok"), UsageReport(Usage(output_tokens=9))], 2),
], ids=["frames", "bytes", "tokens"])
async def test_native_task_limits_reach_external_stream_before_persistence(tmp_path, limits, chunks, produced):
    backend = ExternalAgent([*chunks, StreamStop("end_turn")])
    kernel = build_kernel(base_dir=tmp_path, provider=backend, model=ModelId("external"))
    try:
        await kernel.loop.start()
        with pytest.raises(ProviderError, match="limit"):
            await kernel.loop.run_task(AgentTask(prompt="work", limits=limits))
        assert backend.closed and backend.produced == produced
        events = read_session(tmp_path, kernel.session.id)
        assert not any(e.event.type == "model_call_completed" for e in events)
        state = fold(events)
        assert not state.open_model_intents and not state.open_agent_runs
        result, = state.agent_runs.values()
        assert result.status == "failed" and result.output is None
        assert not list((tmp_path / "sessions" / str(kernel.session.id) / "blobs").iterdir())
    finally:
        await backend.close()
        kernel.session.close()
