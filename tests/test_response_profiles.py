"""Conversation generation settings are explicit, bounded and resumable."""

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.cli import build_kernel
from harness.context import ContextPolicy, render_context_policy
from harness.errors import ContextOverflow, ProviderError
from harness.inference import InferenceRequest
from harness.log import read_session
from harness.messages import Message
from harness.provider import FakeProvider, StreamStop, TextDelta, Usage, text_turn
from harness.types import ModelId


class Capturing(FakeProvider):
    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    def infer(self, request):
        self.requests.append(request)
        return super().infer(request)


def profile(**kwargs):
    return ContextPolicy(response=kwargs)


async def test_profile_bounds_requests_records_limits_and_survives_resume(tmp_path):
    policy = profile(temperature=0, max_output_tokens=64, max_output_bytes=256,
                     instructions="Answer the requested facts first. Keep the answer concise.")
    provider = Capturing([text_turn("first"), text_turn("continued")])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"), context_policy=policy)
    try:
        await kernel.loop.start()
        await kernel.loop.run_task(AgentTask(prompt="work", limits=TaskLimits(max_output_tokens=32)))
        req = provider.requests[0]
        assert req.temperature == 0 and req.max_output_tokens == 32 and req.max_output_bytes == 256
        assert sum(policy.response.instructions in m.text() for m in req.messages) == 1
        run = next(e.event for e in read_session(tmp_path, kernel.session.id) if e.event.type == "agent_run_started")
        assert run.limits["max_output_tokens"] == 32 and run.limits["max_response_bytes"] == 256
        assert "64 output tokens" in render_context_policy(policy, ())
        assert "temperature 0" in render_context_policy(policy, ())
    finally:
        kernel.session.close()
    resumed = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"),
                           resume_session_id=kernel.session.id)
    try:
        assert resumed.context_policy == policy
        await resumed.loop.run_turn("continue")
        assert provider.requests[-1].temperature == 0
        assert sum(policy.response.instructions in m.text() for m in provider.requests[-1].messages) == 1
    finally:
        resumed.session.close()


@pytest.mark.parametrize("purpose", ["conversation", "agent-task", "semantic"])
async def test_direct_dispatch_cannot_widen_conversation_bounds_or_sampling(tmp_path, purpose):
    provider = Capturing([text_turn("ok")])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"),
        context_policy=profile(temperature=0, max_output_tokens=16, max_output_bytes=256))
    try:
        await kernel.loop.start()
        await kernel.loop.dispatcher.dispatch_inference(provider=provider, request=InferenceRequest(
            model=ModelId("test"), messages=(Message.user_text("hi"),), purpose=purpose,
            temperature=1, max_output_tokens=128, max_output_bytes=512))
        req = provider.requests[0]
        if purpose == "semantic":
            assert (req.temperature, req.max_output_tokens, req.max_output_bytes) == (1, 128, 512)
        else:
            assert (req.temperature, req.max_output_tokens, req.max_output_bytes) == (0, 16, 256)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("failure", ["bytes", "tokens", "max_tokens"])
async def test_output_limits_never_turn_truncation_into_success(tmp_path, failure):
    if failure == "bytes":
        script = [TextDelta("x" * 300), StreamStop("end_turn")]
    elif failure == "tokens":
        script = text_turn("x", usage=Usage(1, 65, 0, 0))
    else:
        script = [TextDelta("partial"), StreamStop("max_tokens")]
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([script]), model=ModelId("test"),
        context_policy=profile(max_output_tokens=64, max_output_bytes=256))
    try:
        await kernel.loop.start()
        if failure == "max_tokens":
            result = await kernel.loop.run_task(AgentTask(prompt="work"))
            assert result.status == "incomplete" and result.acceptance == "unverified"
        else:
            with pytest.raises(ProviderError, match="limit"):
                await kernel.loop.run_task(AgentTask(prompt="work"))
    finally:
        kernel.session.close()


async def test_agent_cannot_silently_ignore_explicit_sampling(tmp_path):
    class External:
        execution_kind = "agent"
        called = False

        async def complete(self, **kwargs):
            self.called = True
            yield StreamStop("end_turn")

    provider = External()
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("external"),
                          context_policy=profile(temperature=0))
    try:
        await kernel.loop.start()
        with pytest.raises(ProviderError, match="inference"):
            await kernel.loop.run_turn("work")
        assert not provider.called
    finally:
        kernel.session.close()


async def test_answer_guidance_counts_toward_input_budget(tmp_path):
    provider = Capturing([text_turn("ok")])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"),
        context_policy=ContextPolicy(max_input_bytes=1024, tools=(),
                                     response={"instructions": "x" * 1500}))
    try:
        await kernel.loop.start()
        with pytest.raises(ContextOverflow):
            await kernel.loop.run_turn("work")
        assert not provider.requests
    finally:
        kernel.session.close()


async def test_agent_can_use_guidance_and_local_bounds_without_sampling(tmp_path):
    class External:
        execution_kind = "agent"
        messages = ()

        async def complete(self, **kwargs):
            self.messages = kwargs["messages"]
            yield TextDelta("ok")
            yield StreamStop("end_turn")

    provider = External()
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("external"),
        context_policy=profile(instructions="Answer concisely.", max_output_bytes=256))
    try:
        await kernel.loop.start()
        assert await kernel.loop.run_turn("work") == "ok"
        assert any("Answer concisely." in m.text() for m in provider.messages)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("fields", [{"temperature": float("nan")}, {"temperature": "0"},
    {"temperature": True}, {"max_output_tokens": 0}, {"max_output_tokens": "64"},
    {"max_output_bytes": -1}, {"instructions": " "}, {"instructions": "x" * 2001}])
def test_response_profile_rejects_ambiguous_or_unbounded_values(fields):
    with pytest.raises(ValueError):
        profile(**fields)
