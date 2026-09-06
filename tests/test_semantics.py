"""Semantic suggestions are bounded evidence and never task-control authority."""

import asyncio

import pytest

from harness.cli import build_kernel
from harness.events import SemanticObserved, parse_envelope_line
from harness.fold import fold
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.semantics import MessagePrompt, SemanticLimits, read_semantics
from harness.types import ModelId, ToolName


async def kernel_for(tmp_path, provider, **kwargs):
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"), **kwargs)
    await kernel.loop.start()
    return kernel


async def test_shadow_result_has_versions_and_leaves_task_queue_and_transcript_alone(tmp_path):
    provider = FakeProvider([text_turn('{"kind":"acknowledgement"}')])
    kernel = await kernel_for(tmp_path, provider)
    try:
        pending = kernel.controller.submit("unfinished work")
        result = await kernel.semantics.interpret("thanks", model=ModelId("fake"))
        assert result.kind == "acknowledgement" and result.status == "ok"
        assert result.mode == "shadow" and result.call_id
        assert result.effective_model == "fake"
        assert kernel.controller.pending == (pending,)
        assert kernel.controller.last_result is None
        events = read_session(tmp_path, kernel.session.id)
        assert not fold(events).messages and not fold(events).open_model_intents
        assert read_semantics(tmp_path, kernel.session.id) == [result]
        assert MessagePrompt.model_validate_json(kernel.session.blobs.get(result.prompt)).instructions
        assert len(result.function_version) == 64 and len(result.input_sha256) == 64
        event = next(e for e in events if isinstance(e.event, SemanticObserved))
        assert parse_envelope_line(event.model_dump_json()) == event
        assert len(provider.calls[0]) == 2
    finally:
        kernel.session.close()


@pytest.mark.parametrize("response", [
    text_turn('{"kind":"completed"}'),
    text_turn('{"kind":"question","accepted":true}'),
    tool_call_turn("", ToolName("write_file"), {"file_path": "forbidden"}),
])
async def test_invalid_or_tool_output_abstains_without_dispatching_tools(tmp_path, response):
    kernel = await kernel_for(tmp_path, FakeProvider([response]))
    try:
        result = await kernel.semantics.interpret("Finish it", model=ModelId("fake"))
        assert result.kind == "uncertain" and result.reason == "invalid_output"
        assert not any(e.event.type == "tool_call_proposed"
                       for e in read_session(tmp_path, kernel.session.id))
    finally:
        kernel.session.close()


@pytest.mark.parametrize("option", ["disabled", "oversized", "denied", "budget"])
async def test_unavailable_semantics_do_not_call_provider(tmp_path, option):
    from harness.execution import ExecutionLimits
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    provider = FakeProvider([text_turn('{"kind":"question"}')])
    kwargs = {}
    if option == "denied":
        kwargs["permissions"] = PermissionEngine([RuleSet(
            rules=[PermissionRule("deny", "model:*")], default="deny")])
    if option == "budget":
        kwargs["execution_limits"] = ExecutionLimits(max_model_calls=0)
    kernel = await kernel_for(tmp_path, provider, **kwargs)
    try:
        result = await kernel.semantics.interpret(
            "x" * 4097 if option == "oversized" else "Question?", model=ModelId("fake"),
            enabled=option != "disabled",
        )
        assert result.kind == "uncertain" and result.status == "abstained"
        assert not provider.calls
    finally:
        kernel.session.close()


async def test_timeout_cancellation_and_busy_calls_settle_without_waiting_for_inference(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Hanging:
        async def infer(self, request):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

    kernel = await kernel_for(tmp_path, Hanging())
    try:
        result = await kernel.semantics.interpret("Question?", model=ModelId("fake"),
                                                limits=SemanticLimits(timeout_seconds=0.02))
        assert result.reason == "timeout" and closed.is_set()
        entered.clear()
        closed.clear()
        task = asyncio.create_task(kernel.semantics.interpret("Question?", model=ModelId("fake")))
        await entered.wait()
        busy = await kernel.semantics.interpret("Another?", model=ModelId("fake"))
        assert busy.reason == "busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert read_semantics(tmp_path, kernel.session.id)[-1].reason == "cancelled"
        assert not fold(read_session(tmp_path, kernel.session.id)).open_model_intents
    finally:
        kernel.session.close()


async def test_model_rewrite_cannot_be_scored_as_the_requested_model(tmp_path):
    from harness.hooks import HookBus, ProposedModelCall, Rewrite
    hooks = HookBus()

    async def redirect(call):
        from dataclasses import replace
        return Rewrite(replace(call, model=ModelId("other"))) if isinstance(call, ProposedModelCall) else None

    hooks.register_dispatch("redirect", redirect)
    kernel = await kernel_for(tmp_path, FakeProvider([text_turn('{"kind":"question"}')]), hooks=hooks)
    try:
        result = await kernel.semantics.interpret("Question?", model=ModelId("fake"))
        assert result.reason == "model_changed" and result.kind == "uncertain"
        assert result.effective_model == "other"
    finally:
        kernel.session.close()


async def test_semantics_use_replaced_provider_and_reject_agent_runtime(tmp_path):
    from harness.catalog import Catalog
    from harness.provider_litellm import CatalogProvider
    old = FakeProvider([])
    kernel = await kernel_for(tmp_path, old)
    try:
        service = kernel.semantics
        replacement = FakeProvider([text_turn('{"kind":"uncertain"}')])
        kernel.set_provider(replacement)
        result = await service.interpret("Thanks, maybe more later", model=ModelId("fake"))
        assert result.reason == "uncertain" and replacement.calls and not old.calls
        agent = FakeProvider([])
        agent.bind_dispatcher = lambda dispatcher: None
        kernel.set_provider(CatalogProvider(Catalog({"agent": {"backend": "codex", "route": "codex/x"}}),
                                            codex=agent))
        result = await service.interpret("Question?", model=ModelId("agent"))
        assert result.status == "abstained" and not agent.calls
    finally:
        kernel.session.close()


async def test_oversized_message_is_not_encoded_or_hashed_before_rejection(tmp_path):
    class Oversized(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("must reject character overflow before allocating UTF-8 bytes")

    provider = FakeProvider([])
    kernel = await kernel_for(tmp_path, provider)
    try:
        result = await kernel.semantics.interpret(Oversized("x" * 4097), model=ModelId("fake"))
        assert result.reason == "input_limit" and result.input_sha256 is None
        assert not provider.calls
    finally:
        kernel.session.close()
