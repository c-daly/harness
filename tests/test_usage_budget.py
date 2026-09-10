"""Shared admission, unknown accounting, and durable restart boundaries."""

import asyncio
from decimal import Decimal

import pytest

from harness.budget_cli import budget_snapshot, render_budget
from harness.cli import build_kernel
from harness.errors import NetworkFailed, ProviderError
from harness.events import (ModelCallCancelled, ModelCallCompleted, ModelCallFailed,
                            UsageAttemptFinished, UsageAttemptStarted, UsageBudgetBlocked)
from harness.execution import BudgetExceeded
from harness.inference import InferenceRequest
from harness.log import read_session
from harness.messages import Message
from harness.provider import FakeProvider, StreamStop, TextDelta, Usage, UsageReport, text_turn, tool_call_turn
from harness.types import ModelId, ToolName
from harness.usage_budget import UsageLimits, project_usage


class ReportedFake(FakeProvider):
    usage_accounting = "reported"


def events(kernel):
    return [e.event for e in read_session(kernel.session.base, kernel.session.id)]


async def make_kernel(tmp_path, provider, **kwargs):
    kernel = build_kernel(base_dir=tmp_path, model=ModelId("fake"), provider=provider, **kwargs)
    if not kernel.resumed:
        await kernel.loop.start()
    return kernel


async def dispatch(kernel, purpose="budget-test", **kwargs):
    return await kernel.loop.dispatcher.dispatch_inference(provider=kernel.provider,
        request=InferenceRequest(model=ModelId("fake"), messages=(Message.user_text("Hi"),), purpose=purpose, **kwargs),
        pricing=kernel.loop.pricing, pricing_for=kernel.loop.pricing_for)


@pytest.mark.parametrize("key,value", [
    ("max_input_tokens", True), ("max_output_tokens", -1), ("max_input_tokens", 1.5),
    ("max_cost_usd", True), ("max_cost_usd", float("nan")), ("max_cost_usd", float("inf")),
])
def test_invalid_limits_are_rejected(key, value):
    with pytest.raises(ValueError):
        UsageLimits(**{key: value})


@pytest.mark.parametrize("limits,provider,pricing,reason", [
    (UsageLimits(max_input_tokens=50), FakeProvider([]), None, "no declared usage"),
    (UsageLimits(max_output_tokens=0), ReportedFake([]), None, "stop limit"),
    (UsageLimits(max_cost_usd=1), ReportedFake([]), None, "token prices"),
    (UsageLimits(max_cost_usd=1), ReportedFake([]),
     {"input_cost_per_token": -1, "output_cost_per_token": 0}, "token prices"),
    (UsageLimits(max_cost_usd=1), ReportedFake([]),
     {"input_cost_per_token": 0, "output_cost_per_token": float("nan")}, "token prices"),
])
async def test_preflight_refuses_without_opening_provider(tmp_path, limits, provider, pricing, reason):
    kernel = await make_kernel(tmp_path, provider, usage_limits=limits, pricing=pricing)
    try:
        with pytest.raises(BudgetExceeded, match=reason):
            await dispatch(kernel)
        assert provider.calls == []
        assert len([e for e in events(kernel) if isinstance(e, UsageBudgetBlocked)]) == 1
        assert not any(isinstance(e, UsageAttemptStarted) for e in events(kernel))
        assert isinstance(events(kernel)[-1], ModelCallFailed)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("limits", [UsageLimits(max_input_tokens=10),
                                     UsageLimits(max_output_tokens=4), UsageLimits(max_cost_usd=.18)])
async def test_cumulative_limits_charge_once_and_stop_next_call(tmp_path, limits):
    provider = ReportedFake([text_turn("answer", usage=Usage(5, 2)) for _ in range(3)])
    kernel = await make_kernel(tmp_path, provider, usage_limits=limits,
                              pricing={"input_cost_per_token": .01, "output_cost_per_token": .02})
    try:
        await dispatch(kernel)
        await dispatch(kernel)
        with pytest.raises(BudgetExceeded, match="stop limit"):
            await dispatch(kernel)
        assert len(provider.calls) == 2
        state = project_usage(read_session(tmp_path, kernel.session.id))
        assert (state.input_tokens, state.output_tokens, state.cost_usd) == (10, 4, Decimal(".18"))
        assert len(state.finished) == 2 and not state.pending
        assert state.unknown_cost == 0
    finally:
        kernel.session.close()


async def test_missing_output_does_not_invent_zero_or_hold_known_input_limit(tmp_path):
    provider = ReportedFake([text_turn("ok", usage=Usage(input_tokens=2)) for _ in range(2)])
    kernel = await make_kernel(tmp_path, provider, usage_limits=UsageLimits(max_input_tokens=4))
    try:
        await dispatch(kernel)
        await dispatch(kernel)
        data = budget_snapshot(tmp_path, kernel.session.id)
        assert data["input_tokens"] == 4 and data["unknown_input_attempts"] == 0
        assert data["unknown_output_attempts"] == data["unknown_cost_attempts"] == 2
        assert "Output tokens: 0 reported; stop limit: unbounded; unknown attempts: 2" in render_budget(tmp_path, kernel.session.id)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("failure", ["missing", "schema", "oversized", "decreasing"])
async def test_unknown_and_rejected_stream_usage_is_retained(tmp_path, failure):
    script = text_turn("invalid json", usage=Usage(6, 3))
    options = {}
    if failure == "missing":
        script = text_turn("ok", usage=Usage(6, None))
    elif failure == "schema":
        options["response_schema"] = {"type": "object"}
    elif failure == "oversized":
        options["max_output_tokens"] = 2
    else:
        script.insert(0, UsageReport(Usage(9, 8)))
    kernel = await make_kernel(tmp_path, ReportedFake([script]), usage_limits=UsageLimits(max_output_tokens=100))
    try:
        if failure in ("schema", "oversized"):
            with pytest.raises(ProviderError):
                await dispatch(kernel, **options)
        else:
            await dispatch(kernel)
        with pytest.raises(BudgetExceeded, match="incomplete"):
            await dispatch(kernel)
        state = project_usage(read_session(tmp_path, kernel.session.id))
        assert state.input_tokens == (9 if failure == "decreasing" else 6)
        assert state.output_tokens == {"missing": 0, "schema": 3, "oversized": 3, "decreasing": 8}[failure]
        assert state.unknown_output == 1 and len(kernel.provider.calls) == 1
    finally:
        kernel.session.close()


@pytest.mark.parametrize("bounded", [True, False])
async def test_retries_are_separate_attempts_and_unknown_usage_holds_bounded_retry(tmp_path, bounded):
    class Retrying:
        usage_accounting = "reported"
        calls = 0

        async def infer(self, request):
            self.calls += 1
            yield UsageReport(Usage(3, 1))
            if self.calls == 1:
                raise NetworkFailed("retryable")
            yield TextDelta("ok")
            yield StreamStop("end_turn")

    provider = Retrying()
    kernel = await make_kernel(tmp_path, provider,
                              usage_limits=UsageLimits(max_input_tokens=100) if bounded else None)
    kernel.loop.dispatcher.retry_delays = (0,)
    try:
        if bounded:
            with pytest.raises(BudgetExceeded, match="incomplete"):
                await dispatch(kernel)
        else:
            await dispatch(kernel)
        state = project_usage(read_session(tmp_path, kernel.session.id))
        assert provider.calls == (1 if bounded else 2)
        assert state.input_tokens == 3 * provider.calls and state.output_tokens == provider.calls
        assert state.unknown_input == 1 and not state.pending
    finally:
        kernel.session.close()


async def test_cancelled_usage_settles_before_model_terminal_and_survives_restart(tmp_path):
    entered = asyncio.Event()

    class Hanging:
        usage_accounting = "reported"

        async def infer(self, request):
            yield UsageReport(Usage(7, 2))
            entered.set()
            await asyncio.Event().wait()

    kernel = await make_kernel(tmp_path, Hanging(), usage_limits=UsageLimits(max_input_tokens=100))
    sid = kernel.session.id
    task = asyncio.create_task(dispatch(kernel))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert budget_snapshot(tmp_path, sid)["pending_attempts"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recorded = events(kernel)
        assert isinstance(recorded[-2], UsageAttemptFinished) and isinstance(recorded[-1], ModelCallCancelled)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        kernel.session.close()
    resumed = await make_kernel(tmp_path, ReportedFake([]), resume_session_id=sid)
    try:
        with pytest.raises(BudgetExceeded, match="incomplete"):
            await dispatch(resumed)
        assert resumed.loop.dispatcher.scope.budget.usage.state.input_tokens == 7
        assert not resumed.provider.calls
    finally:
        resumed.session.close()


async def test_parallel_children_charge_root_once_and_new_work_stops_after_crossing(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Parallel:
        usage_accounting = "reported"
        calls = 0

        async def infer(self, request):
            self.calls += 1
            if self.calls == 2:
                entered.set()
            await release.wait()
            for chunk in text_turn("ok", usage=Usage(8, 2)):
                yield chunk

    provider = Parallel()
    kernel = await make_kernel(tmp_path, provider, usage_limits=UsageLimits(max_input_tokens=10))
    children = [asyncio.create_task(kernel.runner.run_result(prompt="work", model=None, parent=kernel.session)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert budget_snapshot(tmp_path, kernel.session.id)["pending_attempts"] == 2
        release.set()
        results = await asyncio.gather(*children)
        assert all(r.status == "completed" for r in results)
        state = kernel.loop.dispatcher.scope.budget.usage.state
        assert state.input_tokens == 16 and state.output_tokens == 4 and len(state.finished) == 2
        for result in results:
            assert budget_snapshot(tmp_path, result.child_session_id)["input_tokens"] == 16
            assert not any(isinstance(e.event, UsageAttemptStarted)
                           for e in read_session(tmp_path, result.child_session_id))
            with pytest.raises(ValueError, match="resume that root"):
                build_kernel(base_dir=tmp_path, model="fake", provider=ReportedFake([]), resume_session_id=result.child_session_id)
        with pytest.raises(BudgetExceeded, match="stop limit"):
            await dispatch(kernel)
        assert provider.calls == 2
    finally:
        release.set()
        await asyncio.gather(*children, return_exceptions=True)
        kernel.session.close()


async def test_nested_delegation_and_internal_inference_use_same_ledger(tmp_path):
    provider = ReportedFake([
        tool_call_turn("", ToolName("dispatch_agent"), {"prompt": "nested"}, usage=Usage(1, 1)),
        tool_call_turn("", ToolName("dispatch_agent"), {"prompt": "leaf"}, usage=Usage(2, 1)),
        text_turn("leaf", usage=Usage(3, 1)), text_turn("middle", usage=Usage(4, 1)),
        text_turn("root", usage=Usage(5, 1)), text_turn("internal", usage=Usage(6, 1)),
    ])
    kernel = await make_kernel(tmp_path, provider, usage_limits=UsageLimits(max_input_tokens=21))
    try:
        assert await kernel.loop.run_turn("work") == "root"
        await dispatch(kernel, purpose="semantic-test")
        with pytest.raises(BudgetExceeded, match="stop limit"):
            await dispatch(kernel)
        starts = [e for e in events(kernel) if isinstance(e, UsageAttemptStarted)]
        assert len(starts) == 6 and len({e.source_session_id for e in starts}) == 3
        assert starts[-1].purpose == "semantic-test"
        assert budget_snapshot(tmp_path, kernel.session.id)["input_tokens"] == 21
    finally:
        kernel.session.close()


async def test_routed_alias_owns_pricing_and_accounting_declaration(tmp_path):
    from harness.hooks import Allow, HookBus, ProposedModelCall, Rewrite
    hooks = HookBus()
    hooks.register_dispatch("route", lambda call: Rewrite(ProposedModelCall(call_id=call.call_id, model=ModelId("metered")))
                            if isinstance(call, ProposedModelCall) and call.model == "fake" else Allow())
    prices = {"input_cost_per_token": .01, "output_cost_per_token": .02}

    class Routed(ReportedFake):
        def usage_accounting(self, model):
            return "reported" if model == "metered" else "unknown"

        async def infer(self, request):
            assert request.model == "metered"
            prices.update(input_cost_per_token=100, output_cost_per_token=100)
            async for chunk in super().infer(request):
                yield chunk

    kernel = await make_kernel(tmp_path, Routed([text_turn("ok", usage=Usage(2, 3))]),
        hooks=hooks, usage_limits=UsageLimits(max_cost_usd=1),
        pricing_for=lambda model: prices if model == "metered" else {})
    try:
        await dispatch(kernel)
        state = project_usage(read_session(tmp_path, kernel.session.id))
        assert state.cost_usd == Decimal(".08")
        completed = next(e for e in events(kernel) if isinstance(e, ModelCallCompleted))
        assert completed.model == "metered" and completed.pricing["input_cost_per_token"] == .01
    finally:
        kernel.session.close()


async def test_resume_keeps_child_totals_and_limits_without_child_log_dependency(tmp_path):
    from harness.events import SubagentSpawned
    kernel = await make_kernel(tmp_path, ReportedFake([text_turn("child", usage=Usage(5, 2))]),
                              usage_limits=UsageLimits(max_input_tokens=5))
    sid = kernel.session.id
    await kernel.runner.run_result(prompt="work", model=None, parent=kernel.session)
    child_id = next(e.child_session_id for e in events(kernel) if isinstance(e, SubagentSpawned))
    kernel.session.close()
    (tmp_path / "sessions" / f"{child_id}.jsonl").unlink()
    resumed = await make_kernel(tmp_path, ReportedFake([]), resume_session_id=sid)
    try:
        assert resumed.loop.dispatcher.scope.budget.usage.limits.max_input_tokens == 5
        with pytest.raises(BudgetExceeded, match="stop limit"):
            await dispatch(resumed)
        assert budget_snapshot(tmp_path, sid)["input_tokens"] == 5
    finally:
        resumed.session.close()
    before = read_session(tmp_path, sid)
    with pytest.raises(ValueError, match="cannot increase"):
        build_kernel(base_dir=tmp_path, model="fake", provider=ReportedFake([]), resume_session_id=sid,
                     usage_limits=UsageLimits(max_input_tokens=6))
    assert read_session(tmp_path, sid) == before


async def test_interrupted_attempt_is_unconfirmed_then_repaired_conservatively(tmp_path):
    kernel = await make_kernel(tmp_path, ReportedFake([]), usage_limits=UsageLimits(max_cost_usd=1))
    sid = kernel.session.id
    kernel.loop.dispatcher.scope.budget.usage.begin(kernel.session, call_id="crashed-call", model="fake",
        purpose="conversation", attempt=0, accounting="reported",
        pricing={"input_cost_per_token": .01, "output_cost_per_token": .02})
    kernel.session.close()
    before = read_session(tmp_path, sid)
    assert "1 pending (usage unconfirmed)" in render_budget(tmp_path, sid)
    assert read_session(tmp_path, sid) == before
    resumed = await make_kernel(tmp_path, ReportedFake([]), resume_session_id=sid)
    try:
        data = budget_snapshot(tmp_path, sid)
        assert data["pending_attempts"] == 0 and data["unknown_cost_attempts"] == 1
        assert next(e for e in events(resumed) if isinstance(e, UsageAttemptFinished)).status == "aborted"
        with pytest.raises(BudgetExceeded, match="incomplete"):
            await dispatch(resumed)
    finally:
        resumed.session.close()


async def test_enabling_limit_on_untracked_legacy_work_does_not_reset_usage(tmp_path):
    from harness.events import ModelCallStarted
    from harness.session import Session
    session = Session(tmp_path, "legacy")
    session.start()
    session.append(ModelCallStarted(call_id="old-call", model="fake"))
    session.append(ModelCallFailed(call_id="old-call", model="fake", error_type="old", message="old"))
    session.close()
    kernel = await make_kernel(tmp_path, ReportedFake([]), resume_session_id="legacy",
                              usage_limits=UsageLimits(max_output_tokens=100))
    try:
        with pytest.raises(BudgetExceeded, match="incomplete"):
            await dispatch(kernel)
        assert budget_snapshot(tmp_path, "legacy")["unknown_output_attempts"] == 1
    finally:
        kernel.session.close()


@pytest.mark.parametrize("declared", ["reported", "unknown"])
async def test_external_response_accounting_uses_same_admission_and_settlement(tmp_path, declared):
    class External(FakeProvider):
        execution_kind = "agent"
        usage_accounting = declared

    provider = External([text_turn("external output", usage=Usage(11, 4))])
    kernel = await make_kernel(tmp_path, provider, usage_limits=UsageLimits(max_input_tokens=10))
    request = InferenceRequest(model="fake", purpose="agent-task", messages=(Message.user_text("work"),))
    try:
        # The compatibility path still meters aggregate CLI usage. This is a
        # scripted transport contract, not a live external billing qualification.
        call = kernel.loop.dispatcher._dispatch_generation(provider=provider, request=request,
            allow_agent=True, pricing=None, pricing_for=None, pinned=True, on_chunk=None)
        if declared == "unknown":
            with pytest.raises(BudgetExceeded, match="no declared usage"):
                await call
            assert not provider.calls
        else:
            result = await call
            assert result.message.text() == "external output"
            assert budget_snapshot(tmp_path, kernel.session.id)["input_tokens"] == 11
    finally:
        kernel.session.close()


async def test_accounting_write_failure_holds_future_work_until_restart(tmp_path, monkeypatch):
    kernel = await make_kernel(tmp_path, ReportedFake([text_turn("ok", usage=Usage(2, 1))]))
    append = kernel.session.append

    def failed_write(event):
        if isinstance(event, UsageAttemptFinished):
            raise OSError("disk unavailable")
        return append(event)

    monkeypatch.setattr(kernel.session, "append", failed_write)
    try:
        with pytest.raises(OSError, match="disk unavailable"):
            await dispatch(kernel)
        monkeypatch.setattr(kernel.session, "append", append)
        with pytest.raises(BudgetExceeded, match="accounting write failed"):
            await dispatch(kernel)
        assert len(kernel.provider.calls) == 1
        assert budget_snapshot(tmp_path, kernel.session.id)["pending_attempts"] == 1
    finally:
        kernel.session.close()


async def test_budget_inspection_in_tui_keeps_draft_and_task_acceptance(tmp_path):
    from types import SimpleNamespace
    from textual.widgets import Input
    from harness.tui_support import SlashCommand
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    from tests.test_tui_tasks import command

    provider = ReportedFake([text_turn("done", usage=Usage(5, 2))])
    app = make_app(tmp_path, provider=provider, usage_limits=UsageLimits(max_input_tokens=5))
    async with app.run_test(size=(150, 45)) as pilot:
        await command(app, pilot, "/task new Keep this obligation")
        await command(app, pilot, "/task require User reviews the answer")
        await command(app, pilot, "Work on it")
        if app._turn_worker is not None:
            await app._turn_worker.wait()
        await command(app, pilot, "Continue")
        if app._turn_worker is not None:
            await app._turn_worker.wait()
        app._plugin_commands["budget"] = SimpleNamespace(body="run this model")
        composer = app.query_one("#prompt", Input)
        composer.value = "keep this draft"
        before = read_session(tmp_path, app.kernel.session.id)
        await app._run_command(SlashCommand("budget", ""))
        await pilot.pause(.1)
        visible = screen_text(app)
        assert "Input tokens: 5 reported; stop limit: 5" in visible
        assert "Last stop:" in visible and "unknown attempts:" in visible
        assert composer.value == "keep this draft" and len(provider.calls) == 1
        assert read_session(tmp_path, app.kernel.session.id) == before
        assert not app.kernel.tasks.selected().accepted
        await app._run_command(SlashCommand("help", ""))
        await pilot.pause(.1)
        assert "/budget" in screen_text(app)


async def test_export_keeps_usage_source_and_inspection_cli_is_read_only(tmp_path, capsys):
    from harness.budget_cli import main
    from harness.portable import task_package
    kernel = await make_kernel(tmp_path, ReportedFake([text_turn("answer", usage=Usage(3, 1))]),
                              usage_limits=UsageLimits(max_input_tokens=20))
    try:
        kernel.tasks.create("Continue in another interface")
        await dispatch(kernel)
        before = read_session(tmp_path, kernel.session.id)
        main([str(kernel.session.id), "--base-dir", str(tmp_path)])
        assert "Input tokens: 3 reported; stop limit: 20" in capsys.readouterr().out
        package, _ = task_package(tmp_path, kernel.session.id)
        assert package["usage_budget"]["root_session_id"] == kernel.session.id
        assert package["usage_budget"]["through_seq"] == before[-1].seq
        assert package["usage_budget"]["input_tokens"] == 3
        assert read_session(tmp_path, kernel.session.id) == before
    finally:
        kernel.session.close()


async def test_handoff_cannot_reset_failed_external_usage(tmp_path):
    from tests.test_handoff import permissions, source, specification
    kernel, provider = await source(tmp_path)
    record = kernel.handoffs.record(specification(kernel))
    sid = kernel.session.id
    kernel.session.close()
    resumed = build_kernel(base_dir=tmp_path / "sessions", model="local", provider=provider,
        resume_session_id=sid, native_tools=True, workspace_root=provider.root, permissions=permissions(),
        usage_limits=UsageLimits(max_input_tokens=1000))
    try:
        with pytest.raises(BudgetExceeded, match="incomplete"):
            await resumed.handoffs.run(record.id)
        assert not provider.requests and not (provider.root / "B.txt").exists()
        assert (provider.root / "A.txt").read_text() == "stage A\n"
    finally:
        resumed.session.close()


def test_catalog_accounting_requires_explicit_valid_declaration():
    from harness.catalog import Catalog
    from harness.provider_litellm import CatalogProvider
    provider = CatalogProvider(Catalog(entries={
        "declared": {"route": "openai/custom", "usage_accounting": "reported"},
        "unknown": {"route": "openai/custom"},
        "invalid": {"route": "openai/custom", "usage_accounting": True},
    }))
    assert provider.usage_accounting("declared") == "reported"
    assert provider.usage_accounting("unknown") == provider.usage_accounting("absent") == "unknown"
    with pytest.raises(ValueError, match="usage_accounting"):
        provider.usage_accounting("invalid")


def test_cli_budget_stop_is_readable_without_provider_traceback(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", "from harness.cli import main; main()", "--base-dir", str(tmp_path),
        "--catalog", str(tmp_path / "absent.toml"), "--workspace", str(tmp_path),
        "--no-plugins", "--no-mcp", "--budget-output-tokens", "0", "-p", "hello"],
        text=True, capture_output=True, timeout=20)
    assert result.returncode != 0
    assert "stopped: root usage budget:" in result.stderr and "stop limit (0)" in result.stderr
    assert "Traceback" not in result.stderr


async def test_export_usage_uses_the_tasks_frozen_log_prefix(tmp_path, monkeypatch):
    from harness.portable import task_package
    kernel = await make_kernel(tmp_path, ReportedFake([text_turn("old", usage=Usage(3, 1))]))
    try:
        kernel.tasks.create("Export this snapshot")
        await dispatch(kernel)
        prefix = read_session(tmp_path, kernel.session.id)
        ledger = kernel.loop.dispatcher.scope.budget.usage

        def capture_then_advance(*args, **kwargs):
            attempt = ledger.begin(kernel.session, call_id="later", model="fake", purpose="later",
                                   attempt=0, pricing={}, accounting="reported")
            ledger.finish(attempt, Usage(100, 50), status="completed")
            return prefix

        monkeypatch.setattr("harness.portable.read_session", capture_then_advance)
        package, _ = task_package(tmp_path, kernel.session.id)
        assert package["source"]["through_seq"] == prefix[-1].seq
        assert package["usage_budget"]["through_seq"] == prefix[-1].seq
        assert package["usage_budget"]["input_tokens"] == 3
        assert budget_snapshot(tmp_path, kernel.session.id)["input_tokens"] == 103
    finally:
        kernel.session.close()


async def test_resume_linked_child_refuses_before_tearing_down_current_tui(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from textual.widgets import Input
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text

    app = make_app(tmp_path, provider=ReportedFake([text_turn("child", usage=Usage(1, 1))]),
                   usage_limits=UsageLimits(max_input_tokens=10))
    async with app.run_test(size=(150, 45)) as pilot:
        original = app.kernel
        child = await original.runner.run_result(prompt="child", model=None, parent=original.session)
        assert child.status == "completed"
        monkeypatch.setattr(app, "push_screen_wait", AsyncMock(return_value=child.child_session_id))
        composer = app.query_one("#prompt", Input)
        composer.value = "keep my current work"
        before = read_session(tmp_path, original.session.id)
        await app._run_resume()
        await pilot.pause(.1)
        assert app.kernel is original and not original.session.closed
        assert composer.value == "keep my current work"
        assert "resume that root session" in screen_text(app)
        assert read_session(tmp_path, original.session.id) == before
        assert budget_snapshot(tmp_path, original.session.id)["input_tokens"] == 1
