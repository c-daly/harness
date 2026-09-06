"""A single-call profile constrains proposals before any tool in a batch executes."""

import json

import pytest

from harness.cli import build_kernel
from harness.context import ContextPolicy, render_context_policy
from harness.errors import MalformedStreamError, ProviderError
from harness.fold import fold
from harness.inference import InferenceRequest, infer
from harness.log import read_session
from harness.messages import Message
from harness.provider import FakeProvider, StreamStop, ToolCallDelta, text_turn, tool_call_turn
from harness.types import CallId, ModelId, ToolName


def batch():
    return [ToolCallDelta(index=0, call_id=CallId("read"), tool=ToolName("read_file"),
                         args_json=json.dumps({"file_path": "FACTS.json"})),
            ToolCallDelta(index=1, call_id=CallId("write"), tool=ToolName("write_file"),
                         args_json=json.dumps({"file_path": "RESULT.json", "content": "guessed"})),
            StreamStop("tool_use")]


@pytest.mark.parametrize("parallel", [None, True, False])
async def test_inference_rejects_multiple_proposals_only_when_requested(parallel):
    request = InferenceRequest(model=ModelId("test"), messages=(Message.user_text("copy"),),
                               purpose="conversation", tool_choice="auto", parallel_tool_calls=parallel)
    provider = FakeProvider([batch()])
    if parallel is False:
        with pytest.raises(MalformedStreamError, match="one tool call"):
            await infer(provider, request)
    else:
        assert len((await infer(provider, request)).message.tool_calls()) == 2


async def test_scope_rejects_entire_batch_before_side_effects_and_settles_log(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "FACTS.json").write_text("source")
    base = tmp_path / "sessions"
    kernel = build_kernel(base_dir=base, provider=FakeProvider([batch()]), model=ModelId("test"),
        workspace_root=workspace, native_tools=True,
        context_policy=ContextPolicy(tools=("read_file", "write_file"), parallel_tool_calls=False))
    try:
        await kernel.loop.start()
        with pytest.raises(MalformedStreamError, match="one tool call"):
            await kernel.loop.run_turn("copy FACTS.json to RESULT.json")
        assert not (workspace / "RESULT.json").exists()
        log = read_session(base, kernel.session.id)
        assert not any(e.event.type == "tool_call_proposed" for e in log)
        assert any(e.event.type == "model_call_failed" for e in log)
        assert [e.event.error_type for e in log if e.event.type == "model_call_failed"] == [
            "ToolCallLimitExceeded"]
        state = fold(log)
        assert not state.open_model_intents and not state.open_intents and not state.open_agent_runs
    finally:
        kernel.session.close()


async def test_single_calls_remain_usable_and_profile_survives_resume(tmp_path):
    base, workspace = tmp_path / "sessions", tmp_path / "project"
    workspace.mkdir()
    (workspace / "FACTS.json").write_text("source")
    provider = FakeProvider([tool_call_turn("", ToolName("read_file"), {"file_path": "FACTS.json"}),
                             text_turn("source"), text_turn("resumed")])
    policy = ContextPolicy(tools=("read_file",), parallel_tool_calls=False)
    kernel = build_kernel(base_dir=base, provider=provider, model=ModelId("test"),
                          context_policy=policy, native_tools=True, workspace_root=workspace)
    try:
        await kernel.loop.start()
        assert await kernel.loop.run_turn("read the source") == "source"
        await kernel.loop.end()
    finally:
        kernel.session.close()
    resumed = build_kernel(base_dir=base, provider=provider, model=ModelId("test"),
                           resume_session_id=kernel.session.id)
    try:
        assert resumed.context_policy == policy
        assert "one tool call per response" in render_context_policy(policy, ())
        assert await resumed.loop.run_turn("continue") == "resumed"
    finally:
        resumed.session.close()


async def test_profile_cannot_be_bypassed_by_a_wider_request(tmp_path):
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([batch()]), model=ModelId("test"),
                          context_policy=ContextPolicy(parallel_tool_calls=False))
    try:
        await kernel.loop.start()
        request = InferenceRequest(model=ModelId("test"), messages=(Message.user_text("copy"),),
            purpose="conversation", tool_choice="auto", parallel_tool_calls=True)
        with pytest.raises(MalformedStreamError, match="one tool call"):
            await kernel.loop.dispatcher.dispatch_inference(provider=kernel.provider, request=request)
    finally:
        kernel.session.close()


async def test_external_agent_is_rejected_before_provider_execution(tmp_path):
    class Agent:
        execution_kind = "agent"
        called = False

        async def complete(self, **kwargs):
            self.called = True
            yield StreamStop("end_turn")

    provider = Agent()
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("external"),
                          context_policy=ContextPolicy(parallel_tool_calls=False))
    try:
        await kernel.loop.start()
        with pytest.raises(ProviderError, match="inference model"):
            await kernel.loop.run_turn("work")
        assert not provider.called
    finally:
        kernel.session.close()


@pytest.mark.parametrize("parallel,with_tools", [(None, True), (False, True), (True, True), (False, False)])
async def test_sdk_option_is_forwarded_only_for_tool_requests(monkeypatch, parallel, with_tools):
    import litellm
    from types import SimpleNamespace
    from harness.provider_litellm import LiteLLMProvider
    from harness.tools import ToolSpec

    captured = []

    async def stream():
        yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content="done"), finish_reason="stop")])

    async def acompletion(**kwargs):
        captured.append(kwargs)
        return stream()

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    req = InferenceRequest(model=ModelId("test"), messages=(Message.user_text("work"),),
        purpose="conversation", tools=(ToolSpec(ToolName("read"), "Read", {}),) if with_tools else (),
        tool_choice="auto" if with_tools else "none", parallel_tool_calls=parallel)
    await infer(LiteLLMProvider(), req)
    if with_tools and parallel is not None:
        assert captured[0]["parallel_tool_calls"] is parallel
    else:
        assert "parallel_tool_calls" not in captured[0]


@pytest.mark.parametrize("value", [0, 1, "false"])
def test_profile_does_not_coerce_ambiguous_flags(value):
    with pytest.raises(ValueError):
        ContextPolicy(parallel_tool_calls=value)
