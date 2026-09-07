"""Fallback preserves a live assignment; it never replays task execution."""

import asyncio
from dataclasses import replace

import httpx
import pytest

from harness.agent import AgentTask, TaskLimits
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.context import ContextPolicy, ContextSource
from harness.dispatcher import ModelDispatchBlocked
from harness.errors import AuthFailed, LocalUnavailable, MalformedStreamError, NetworkFailed, Overloaded, RateLimited
from harness.events import parse_envelope_line
from harness.execution import BudgetExceeded, ExecutionLimits
from harness.fallback import FallbackPolicy
from harness.fold import fold
from harness.hooks import Allow, HookBus, ProposedModelCall, ProposedToolCall, Rewrite
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import TextDelta, text_turn, tool_call_turn
from harness.resources import LocalResources
from harness.routing import RoutingConfigError, RoutingRuleSet
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName, new_call_id


def catalog():
    return Catalog({
        "primary": {"route": "openai/remote"},
        "local": {"route": "openai/local", "api_base": "http://127.0.0.1:8080/v1",
                  "tags": ["tools"], "local": {}},
        "spare": {"route": "openai/spare", "api_base": "http://127.0.0.1:8081/v1",
                  "tags": ["tools"], "local": {}},
        "agent": {"route": "codex/default", "backend": "codex"},
    })


class Scripted:
    def __init__(self, *steps):
        self.catalog = catalog()
        self.steps, self.calls = list(steps), []

    def execution_kind(self, model):
        return self.catalog.resolve(str(model)).execution_kind

    async def infer(self, request):
        self.calls.append(request)
        step = self.steps.pop(0)
        if callable(step):
            step = await step(request)
        if isinstance(step, BaseException):
            raise step
        for chunk in step:
            yield chunk


class RecordTool:
    spec = ToolSpec(name=ToolName("record"), description="Record one operation", parameters={})

    def __init__(self):
        self.calls = []

    async def __call__(self, args):
        self.calls.append(args)
        return "project context"


async def kernel_for(tmp_path, provider, **kwargs):
    kwargs.setdefault("fallback_policy", FallbackPolicy(models=("local",)))
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("primary"), **kwargs)
    kernel.loop.dispatcher.retry_delays = ()
    await kernel.loop.start()
    return kernel


def state_for(kernel):
    events = read_session(kernel.session.base, kernel.session.id)
    state = fold(events)
    assert not (state.open_intents or state.open_model_intents or state.open_agent_runs)
    assert all(parse_envelope_line(e.model_dump_json()) == e for e in events)
    return state


@pytest.mark.parametrize("failure", [NetworkFailed, AuthFailed, RateLimited, Overloaded, LocalUnavailable])
async def test_same_task_request_context_and_budget_survive_first_call_failure(tmp_path, failure):
    provider = Scripted(failure("private provider error"), text_turn("done"))
    kernel = await kernel_for(tmp_path, provider, context_policy=ContextPolicy(sources=(
        ContextSource(id="normal-context", tool="record", required=True),)))
    context = RecordTool()
    kernel.registry.register(context)
    task = AgentTask(prompt="finish", acceptance_criteria=("independent check",))
    progress = []
    try:
        result = await kernel.loop.run_task(task, on_progress=progress.append)
        assert result.status == "completed" and result.acceptance == "unverified"
        assert result.task_id == task.id and result.remaining_criteria == task.acceptance_criteria
        first, second = provider.calls
        assert first.model == "primary" and second.model == "local"
        assert first.model_dump(exclude={"model", "timeout_seconds"}) == second.model_dump(
            exclude={"model", "timeout_seconds"})
        assert 0 < second.timeout_seconds <= first.timeout_seconds
        assert len(context.calls) == 1
        assert kernel.loop.dispatcher.scope.budget.model_calls == 2
        assert kernel.loop.dispatcher.scope.budget.tool_calls == 1
        assert result.usage.output_tokens is None  # Failed inference is not free.
        state = state_for(kernel)
        assert len(state.agent_runs) == 1
        decision, = state.fallback_decisions
        assert decision.status == "selected" and decision.to_model == "local"
        assert decision.task_id == task.id and decision.run_id == result.run_id
        events = read_session(tmp_path, kernel.session.id)
        failed = next(e for e in events if e.event.type == "model_call_failed")
        chosen = next(e for e in events if e.event.type == "fallback_decided")
        resumed = next(e for e in events if e.event.type == "model_call_proposed" and e.event.model == "local")
        assert decision.failed_call_id == failed.event.call_id and failed.seq < chosen.seq < resumed.seq
        assert "private provider error" not in str(chosen.event.model_dump())
        assert [m.text() for m in state.messages] == ["finish", "done"]
        assert any(p.phase == "fallback" for p in progress)
        assert kernel.loop.model == "primary" and kernel.loop.active_model is None
    finally:
        kernel.session.close()


async def test_selected_alias_stays_for_tools_but_next_task_uses_user_default(tmp_path):
    provider = Scripted(NetworkFailed(), tool_call_turn("", ToolName("record"), {}),
                        text_turn("done"), text_turn("next"))
    kernel = await kernel_for(tmp_path, provider)
    tool = RecordTool()
    kernel.registry.register(tool)
    try:
        await kernel.loop.run_task(AgentTask(prompt="work"))
        assert len(tool.calls) == 1
        await kernel.loop.run_task(AgentTask(prompt="new task"))
        assert [r.model for r in provider.calls] == ["primary", "local", "local", "primary"]
        assert len(state_for(kernel).fallback_decisions) == 1
    finally:
        kernel.session.close()


@pytest.mark.parametrize("when", ["after_response", "during_inference"])
async def test_side_effects_hold_for_reconciliation_even_on_first_inference(tmp_path, when):
    tool = RecordTool()
    provider = Scripted()
    kernel = await kernel_for(tmp_path, provider)
    kernel.registry.register(tool)

    async def unexpected_tool(request):
        await kernel.loop.dispatcher.dispatch_tool(ProposedToolCall(new_call_id(), ToolName("record"), {}))
        return NetworkFailed()

    provider.steps = ([tool_call_turn("", ToolName("record"), {}), NetworkFailed()]
                      if when == "after_response" else [unexpected_tool])
    if when == "during_inference":
        kernel.loop.dispatcher.retry_delays = (0, 0)
        provider.steps = [unexpected_tool] * 3
    try:
        with pytest.raises(NetworkFailed):
            await kernel.loop.run_task(AgentTask(prompt="write once", acceptance_criteria=("check write",)))
        assert len(tool.calls) == 1 and all(r.model == "primary" for r in provider.calls)
        state = state_for(kernel)
        assert state.fallback_decisions[-1].reason == "reconciliation_required"
        result, = state.agent_runs.values()
        assert result.remaining_criteria == ("check write",) and result.status == "failed"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["pinned", "disabled", "denied", "budget", "malformed"])
async def test_fallback_does_not_override_authority_or_contract_failures(tmp_path, mode):
    provider = Scripted(MalformedStreamError() if mode == "malformed" else NetworkFailed())
    kwargs = {}
    if mode == "pinned":
        kwargs["model_pinned"] = True
    if mode == "disabled":
        kwargs["fallback_policy"] = None
    if mode == "denied":
        kwargs["permissions"] = PermissionEngine([RuleSet(rules=[PermissionRule("deny", "model:*")])])
    if mode == "budget":
        kwargs["execution_limits"] = ExecutionLimits(max_model_calls=0)
    kernel = await kernel_for(tmp_path, provider, **kwargs)
    try:
        expected = {"denied": ModelDispatchBlocked, "budget": BudgetExceeded,
                    "malformed": MalformedStreamError}.get(mode, NetworkFailed)
        with pytest.raises(expected):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert len(provider.calls) == (0 if mode in ("denied", "budget") else 1)
        decisions = state_for(kernel).fallback_decisions
        assert [d.reason for d in decisions] == (["pinned"] if mode == "pinned" else [])
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["deny_destination", "budget_spent", "rewrite_destination"])
async def test_destination_passes_dispatch_again_with_shared_authority(tmp_path, mode):
    kwargs = {}
    if mode == "deny_destination":
        kwargs["permissions"] = PermissionEngine([RuleSet(rules=[
            PermissionRule("allow", "model:primary"), PermissionRule("deny", "model:local")])])
    elif mode == "budget_spent":
        kwargs["execution_limits"] = ExecutionLimits(max_model_calls=1)
    else:
        hooks = HookBus()
        hooks.register_dispatch("rewrite", lambda call: Rewrite(replace(call, model=ModelId("primary")))
                                if isinstance(call, ProposedModelCall) and call.model == "local" else Allow())
        kwargs["hooks"] = hooks
    provider = Scripted(NetworkFailed(), text_turn("must not run"))
    kernel = await kernel_for(tmp_path, provider, **kwargs)
    try:
        with pytest.raises(BudgetExceeded if mode == "budget_spent" else ModelDispatchBlocked):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert len(provider.calls) == 1
        assert state_for(kernel).fallback_decisions[-1].status == "selected"
        assert kernel.loop.dispatcher.scope.budget.model_calls == 1
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["unknown", "remote", "agent", "tags", "busy"])
async def test_skips_ineligible_destination_then_uses_finite_next_candidate(tmp_path, mode):
    provider = Scripted(NetworkFailed(), text_turn("done"))
    candidate = {"unknown": "absent", "remote": "other-remote", "agent": "agent"}.get(mode, "local")
    provider.catalog.entries["other-remote"] = {"route": "openai/other"}
    resources = LocalResources(transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    if mode == "tags":
        provider.catalog.entries["local"]["tags"] = []
    if mode == "busy":
        await resources.check(provider.catalog.resolve("local"), emit=lambda e: None)
    kernel = await kernel_for(tmp_path, provider, resources=resources,
                              fallback_policy=FallbackPolicy(models=(candidate, "spare")))
    kernel.registry.register(RecordTool())
    try:
        await kernel.loop.run_task(AgentTask(prompt="work"))
        skipped, selected = state_for(kernel).fallback_decisions
        expected = {"unknown": "unknown_alias", "remote": "not_local_inference", "agent": "not_local_inference",
                    "tags": "missing_capability", "busy": "busy"}
        assert skipped.status == "skipped" and skipped.reason == expected[mode]
        assert selected.to_model == "spare" and len(provider.calls) == 2
    finally:
        kernel.session.close()


async def test_failing_local_chain_is_finite_and_never_restarts_task(tmp_path):
    provider = Scripted(NetworkFailed(), LocalUnavailable(), AuthFailed())
    kernel = await kernel_for(tmp_path, provider, fallback_policy=FallbackPolicy(models=("local", "spare", "primary")))
    try:
        with pytest.raises(AuthFailed):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert [r.model for r in provider.calls] == ["primary", "local", "spare"]
        state = state_for(kernel)
        assert [d.status for d in state.fallback_decisions] == ["selected", "selected", "held"]
        assert state.fallback_decisions[-1].reason == "exhausted" and len(state.agent_runs) == 1
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_interruption_during_fallback_settles_same_task(tmp_path, mode):
    entered, closed = asyncio.Event(), asyncio.Event()

    async def hanging(request):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    provider = Scripted(NetworkFailed(), hanging)
    kernel = await kernel_for(tmp_path, provider)
    try:
        task = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work", limits=TaskLimits(
            timeout_seconds=0.15 if mode == "deadline" else 10))))
        await entered.wait()
        if mode == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if mode == "cancel" else TimeoutError):
            await task
        assert closed.is_set() and len(provider.calls) == 2
        state = state_for(kernel)
        assert len(state.agent_runs) == 1 and kernel.loop.active_model is None
        assert next(iter(state.agent_runs.values())).status == ("cancelled" if mode == "cancel" else "incomplete")
    finally:
        kernel.session.close()


async def test_resume_shows_saved_choices_but_does_not_activate_historical_policy(tmp_path):
    from harness.resident import render_status
    provider = Scripted(NetworkFailed(), text_turn("done"))
    kernel = await kernel_for(tmp_path, provider)
    await kernel.loop.run_task(AgentTask(prompt="work"))
    session_id = kernel.session.id
    saved = state_for(kernel).fallback_decisions
    kernel.session.close()
    replacement = Scripted()
    resumed = build_kernel(base_dir=tmp_path, provider=replacement, model=ModelId("primary"),
                           resume_session_id=session_id)
    try:
        state = state_for(resumed)
        assert state.fallback_decisions == saved and state.fallback_policy is None
        assert resumed.loop.fallback_policy is None and not replacement.calls
        status = render_status(read_session(tmp_path, session_id))
        assert "Fallback selected: primary -> local; network" in status
        assert "Automatic local fallback: disabled" in status
    finally:
        resumed.session.close()


@pytest.mark.parametrize("typed", [False, True])
async def test_external_execution_never_automatically_hands_off(tmp_path, typed):
    from harness.agent_runtime import AgentRuntimeInfo

    class External(Scripted):
        def execution_kind(self, model):
            return "agent"

        def agent_runtime_info(self, model):
            return AgentRuntimeInfo(runtime="test-agent") if typed else None

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            raise NetworkFailed()
            yield

    provider = External()
    kernel = await kernel_for(tmp_path, provider)
    try:
        with pytest.raises(NetworkFailed):
            await kernel.loop.run_task(AgentTask(prompt="agent work"))
        state = state_for(kernel)
        assert len(provider.calls) == 1 and state.fallback_decisions[-1].reason == "external_runtime"
        assert len(state.agent_runs) == (2 if typed else 1)
    finally:
        kernel.session.close()


async def test_retry_reservations_and_deadline_are_not_refunded_at_fallback(tmp_path):
    provider = Scripted(NetworkFailed(), NetworkFailed(), text_turn("done"))
    kernel = await kernel_for(tmp_path, provider, execution_limits=ExecutionLimits(max_model_calls=3))
    kernel.loop.dispatcher.retry_delays = (0.02,)
    try:
        await kernel.loop.run_task(AgentTask(prompt="work"))
        assert [r.model for r in provider.calls] == ["primary", "primary", "local"]
        assert provider.calls[-1].timeout_seconds < provider.calls[0].timeout_seconds - 0.015
        assert kernel.loop.dispatcher.scope.budget.model_calls == 3
        assert len(state_for(kernel).fallback_decisions) == 1
    finally:
        kernel.session.close()


async def test_child_session_activity_blocks_retry_and_fallback(tmp_path):
    provider = Scripted()
    kernel = await kernel_for(tmp_path, provider)
    kernel.loop.dispatcher.retry_delays = (0, 0)

    async def child_work(request):
        assert await kernel.runner.run(prompt="child work", model=None, parent=kernel.session) == "child done"
        return NetworkFailed()

    provider.steps = [child_work, text_turn("child done"), text_turn("must not continue")]
    try:
        with pytest.raises(NetworkFailed):
            await kernel.loop.run_task(AgentTask(prompt="parent work"))
        assert len(provider.calls) == 2 and kernel.loop.dispatcher.scope.budget.children == 1
        assert state_for(kernel).fallback_decisions[-1].reason == "reconciliation_required"
        assert not any(e.event.type == "retry_attempted" for e in read_session(tmp_path, kernel.session.id))
    finally:
        kernel.session.close()


async def test_local_readiness_failure_uses_actual_provider_path_before_next_candidate(tmp_path, monkeypatch):
    from harness.provider_litellm import CatalogProvider
    calls = []

    async def complete(**kwargs):
        calls.append(str(kwargs["model"]))
        if kwargs["model"] == "openai/remote":
            raise NetworkFailed()
        for chunk in text_turn("done"):
            yield chunk

    monkeypatch.setattr("harness.provider_litellm._acomplete", complete)
    resources = LocalResources(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"data": [{"id": "spare"}]})))
    models = catalog()
    models.entries["local"]["local"]["required_files"] = [str(tmp_path / "missing-weights")]
    kernel = await kernel_for(tmp_path, CatalogProvider(models), resources=resources,
                              fallback_policy=FallbackPolicy(models=("local", "spare")))
    try:
        await kernel.loop.run_task(AgentTask(prompt="work"))
        assert calls == ["openai/remote", "openai/spare"]
        decisions = state_for(kernel).fallback_decisions
        assert [(d.to_model, d.reason) for d in decisions] == [("local", "network"), ("spare", "local_unavailable")]
        assert kernel.loop.dispatcher.scope.budget.model_calls == 3
    finally:
        kernel.session.close()


@pytest.mark.parametrize("body", ['models = ["local", "local"]', 'models = ["a", "b", "c", "d"]',
                                  'models = ["local"]\nunknown = true', 'models = ["bad\\nname"]'])
def test_bad_configuration_fails_closed(tmp_path, body):
    path = tmp_path / "routing.toml"
    path.write_text("[fallback]\n" + body)
    with pytest.raises(RoutingConfigError, match="fallback"):
        RoutingRuleSet.load(path)


def test_project_policy_can_disable_inherited_user_candidates(tmp_path):
    path = tmp_path / "routing.toml"
    path.write_text('default = "primary"\n[fallback]\nmodels = ["local"]\nrequired_tags = ["tools"]\n')
    user = RoutingRuleSet.load(path)
    assert RoutingRuleSet.merge([RoutingRuleSet(), user]).fallback.models == ("local",)
    assert RoutingRuleSet.merge([RoutingRuleSet(fallback=FallbackPolicy()), user]).fallback.models == ()


async def test_tui_clears_failed_stream_keeps_draft_and_displays_selected_model(tmp_path):
    from textual.widgets import Input
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text

    entered = asyncio.Event()
    release = asyncio.Event()

    class Streaming(Scripted):
        async def infer(self, request):
            self.calls.append(request)
            if request.model == "primary":
                yield TextDelta("DISCARDED partial answer")
                raise NetworkFailed()
            entered.set()
            await release.wait()
            for chunk in text_turn("Local result"):
                yield chunk

    provider = Streaming()
    app = make_app(tmp_path, provider=provider, model=ModelId("primary"),
                   fallback_policy=FallbackPolicy(models=("local",)))
    app.kernel.loop.dispatcher.retry_delays = ()
    async with app.run_test(size=(140, 45)) as pilot:
        composer = app.query_one("#prompt", Input)
        composer.value = "work"
        await pilot.press("enter")
        await asyncio.wait_for(entered.wait(), 5)
        composer.value = "unfinished draft"
        await pilot.pause(0.1)
        rendered = screen_text(app)
        assert "Fallback selected: primary -> local; network" in rendered
        assert "DISCARDED" not in rendered
        assert app.kernel.loop.active_model == "local"
        release.set()
        await pilot.pause(0.3)
        assert composer.value == "unfinished draft" and "Local result" in screen_text(app)
        assert [m.text() for m in state_for(app.kernel).messages] == ["work", "Local result"]
