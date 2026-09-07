"""Correction is bounded new inference, never execution of a rejected proposal."""

import asyncio
import json

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.cli import build_kernel
from harness.context import ContextPolicy, render_context_policy
from harness.errors import MalformedStreamError, ToolCallLimitExceeded
from harness.events import Envelope, UnknownEvent, parse_envelope_line
from harness.execution import BudgetExceeded, ExecutionLimits
from harness.fold import fold
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, StreamStop, TextDelta, ToolCallDelta, Usage, text_turn, tool_call_turn
from harness.types import CallId, ModelId, ToolName


def rejected():
    return [ToolCallDelta(index=i, call_id=CallId(f"bad-{i}"), tool=ToolName("write_file"),
                         args_json=json.dumps({"file_path": "forbidden", "content": "PRIVATE"}))
            for i in range(2)] + [StreamStop("tool_use")]


def kernel_for(tmp_path, provider, *, attempts=2, **kwargs):
    return build_kernel(base_dir=tmp_path / "sessions", workspace_root=tmp_path,
        provider=provider, model=ModelId("test"), native_tools=True,
        context_policy=ContextPolicy(parallel_tool_calls=False, tool_recovery_attempts=attempts,
                                     tools=("read_file", "write_file")), **kwargs)


def settled(kernel):
    log = read_session(kernel.session.base, kernel.session.id)
    state = fold(log)
    assert not state.open_intents and not state.open_model_intents and not state.open_agent_runs
    assert state.messages == kernel.loop.history
    return [e.event for e in log]


async def test_correction_is_durable_and_replays_without_rejected_tools(tmp_path):
    (tmp_path / "source").write_text("grounded")
    provider = FakeProvider([rejected(),
        tool_call_turn("", ToolName("read_file"), {"file_path": "source"}, usage=Usage(1, 1, 0, 0)),
        text_turn("grounded", usage=Usage(1, 1, 0, 0))])
    kernel = kernel_for(tmp_path, provider)
    try:
        await kernel.loop.start()
        result = await kernel.loop.run_task(AgentTask(prompt="read source"))
        assert result.status == "completed" and result.acceptance == "unverified"
        assert result.usage.input_tokens is None  # rejected generation is not free/known
        events = settled(kernel)
        corrections = [e for e in events if e.type == "model_correction_requested"]
        assert len(corrections) == 1
        correction = corrections[0]
        assert correction.failed_call_id == next(e.call_id for e in events if e.type == "model_call_failed")
        assert correction.task_id == result.task_id and correction.agent_run_id == result.run_id
        assert correction.attempt == 1 and "None of those tool calls ran" in correction.instruction
        assert any(m.text() == correction.instruction for m in provider.calls[1])
        assert "PRIVATE" not in json.dumps([e.model_dump(mode="json") for e in events])
        assert not (tmp_path / "forbidden").exists()
        assert kernel.loop.dispatcher.scope.budget.model_calls == 3
        env = Envelope(session_id=kernel.session.id, seq=1, ts=0, event=correction)
        assert parse_envelope_line(env.model_dump_json()).event == correction
        raw = env.model_dump(mode="json")
        raw["event"]["type"] = "future_correction"
        assert isinstance(parse_envelope_line(json.dumps(raw)).event, UnknownEvent)
        assert "2 correction attempts per task" in render_context_policy(kernel.context_policy, ())
    finally:
        kernel.session.close()
    resumed = build_kernel(base_dir=tmp_path / "sessions", provider=FakeProvider([text_turn("ok")]),
                           model=ModelId("test"), resume_session_id=kernel.session.id)
    try:
        assert resumed.context_policy.tool_recovery_attempts == 2
        assert resumed.loop.history == kernel.loop.history
        assert await resumed.loop.run_turn("continue") == "ok"
    finally:
        resumed.session.close()


@pytest.mark.parametrize("attempts,max_iterations,corrections,calls", [(0, 8, 0, 1), (2, 8, 2, 3),
                                                                   (2, 1, 0, 1), (2, 2, 1, 2)])
async def test_retry_and_iteration_limits_never_dispatch_rejected_calls(
        tmp_path, attempts, max_iterations, corrections, calls):
    kernel = kernel_for(tmp_path, FakeProvider([rejected()] * 4), attempts=attempts)
    try:
        await kernel.loop.start()
        with pytest.raises(ToolCallLimitExceeded):
            await kernel.loop.run_task(AgentTask(prompt="work", limits=TaskLimits(max_iterations=max_iterations)))
        events = settled(kernel)
        assert sum(e.type == "model_correction_requested" for e in events) == corrections
        assert sum(e.type == "model_call_failed" for e in events) == calls
        assert not any(e.type == "tool_call_proposed" for e in events)
    finally:
        kernel.session.close()


async def test_root_budget_is_not_extended_for_correction(tmp_path):
    kernel = kernel_for(tmp_path, FakeProvider([rejected(), text_turn("should not run")]),
                        execution_limits=ExecutionLimits(max_model_calls=1))
    try:
        await kernel.loop.start()
        with pytest.raises(BudgetExceeded):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        result = next(e.result for e in settled(kernel) if e.type == "agent_run_finished")
        assert result.status == "incomplete" and result.reason == "budget"
        assert kernel.loop.dispatcher.scope.budget.model_calls == 1
        settled(kernel)
    finally:
        kernel.session.close()


async def test_corrected_proposal_still_requires_tool_permission(tmp_path):
    provider = FakeProvider([rejected(), tool_call_turn("", ToolName("write_file"),
        {"file_path": "forbidden", "content": "write"}), text_turn("denied")])
    permissions = PermissionEngine([RuleSet(rules=[PermissionRule("deny", "write_file")], default="allow")])
    kernel = kernel_for(tmp_path, provider, permissions=permissions)
    try:
        await kernel.loop.start()
        await kernel.loop.run_task(AgentTask(prompt="work"))
        events = settled(kernel)
        assert sum(e.type == "model_correction_requested" for e in events) == 1
        assert any(e.type == "tool_call_completed" and e.is_error for e in events)
        assert not (tmp_path / "forbidden").exists()
    finally:
        kernel.session.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_malformed_and_cancelled_responses_do_not_get_correction(tmp_path, cancel):
    class Broken:
        async def infer(self, request):
            if cancel:
                raise asyncio.CancelledError
            raise MalformedStreamError("private invalid arguments")
            yield

    kernel = kernel_for(tmp_path, Broken())
    try:
        await kernel.loop.start()
        with pytest.raises(asyncio.CancelledError if cancel else MalformedStreamError):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert not any(e.type == "model_correction_requested" for e in settled(kernel))
    finally:
        kernel.session.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_and_deadline_during_correction_settle_without_replaying(tmp_path, cancel):
    class Paused(FakeProvider):
        entered = asyncio.Event()
        closed = False

        async def infer(self, request):
            if not self.calls:
                async for chunk in super().infer(request):
                    yield chunk
                return
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.closed = True

    provider = Paused([rejected()])
    kernel = kernel_for(tmp_path, provider)
    try:
        await kernel.loop.start()
        running = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work",
            limits=TaskLimits(timeout_seconds=10 if cancel else 0.2))))
        await asyncio.wait_for(provider.entered.wait(), 2)
        if cancel:
            running.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await running
        assert provider.closed
        events = settled(kernel)
        assert sum(e.type == "model_correction_requested" for e in events) == 1
        assert not any(e.type == "tool_call_proposed" for e in events)
        assert not (tmp_path / "forbidden").exists()
    finally:
        kernel.session.close()


async def test_tui_announces_correction_and_discards_rejected_live_text(tmp_path):
    from textual.widgets import Input
    from harness.tui import HarnessApp

    class Gated(FakeProvider):
        streaming = asyncio.Event()
        release = asyncio.Event()

        async def infer(self, request):
            async for chunk in super().infer(request):
                yield chunk
                if len(self.calls) == 2 and isinstance(chunk, TextDelta):
                    self.streaming.set()
                    await self.release.wait()

    provider = Gated([[TextDelta(text="UNACCEPTED_DRAFT"), *rejected()], text_turn("Recovered answer")])
    kernel = kernel_for(tmp_path, provider)
    app = HarnessApp(kernel)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause(0.1)
        composer = app.query_one("#prompt", Input)
        composer.post_message(Input.Submitted(composer, "work"))
        await asyncio.wait_for(provider.streaming.wait(), 2)
        await pilot.pause(0.2)
        try:
            rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            assert "requesting one next call (correction 1)" in rendered
            assert "Recovered answer" in rendered
            assert "UNACCEPTED_DRAFT" not in rendered
        finally:
            provider.release.set()
        await pilot.pause(0.2)
        assert app.controller.active is None


@pytest.mark.parametrize("value", [-1, 3, True, "1"])
def test_correction_bound_is_strict(value):
    with pytest.raises(ValueError):
        ContextPolicy(parallel_tool_calls=False, tool_recovery_attempts=value)


@pytest.mark.parametrize("parallel", [None, True])
def test_correction_requires_single_tool_bound(parallel):
    with pytest.raises(ValueError):
        ContextPolicy(parallel_tool_calls=parallel, tool_recovery_attempts=1)
