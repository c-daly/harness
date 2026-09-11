"""Explicit run stops preserve siblings, evidence and cleanup ownership."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import threading
from itertools import count

import pytest

from harness.agent import AgentTask, current_agent_run
from harness.cli import build_kernel
from harness.events import AgentRunCancelRequested, AgentRunFinished, AgentRunStarted
from harness.execution import current_scope
from harness.fold import fold
from harness.log import read_session
from harness.mixture import Expert, run_strategy_result
from harness.portable import task_package
from harness.provider import FakeProvider, StreamStop, TextDelta, text_turn, tool_call_turn
from harness.run_budgets import extend_execution
from harness.run_controls import cancel_run, render_run_controls
from harness.tui_support import SlashCommand
from tests.test_external_agent_runtime import ScriptedCodex


class HeldProvider:
    def __init__(self, *aliases, cleanup=False, swallow=False):
        self.entered = {a: asyncio.Event() for a in aliases}
        self.release = {a: asyncio.Event() for a in aliases}
        self.cleaning = {a: asyncio.Event() for a in aliases}
        self.cleaned = set()
        self.cleanup = asyncio.Event()
        if not cleanup:
            self.cleanup.set()
        self.swallow = swallow
        self.calls = []

    async def infer(self, request):
        async for chunk in self.generate(request.model):
            yield chunk

    async def generate(self, alias):
        self.calls.append(alias)
        try:
            yield TextDelta(f"partial {alias}")
            self.entered[alias].set()
            try:
                await self.release[alias].wait()
            except asyncio.CancelledError:
                if not self.swallow:
                    raise
            yield StreamStop("end_turn")
        finally:
            self.cleaning[alias].set()
            await self.cleanup.wait()
            self.cleaned.add(alias)

    def unblock(self):
        self.cleanup.set()
        for event in self.release.values():
            event.set()


@pytest.fixture
async def make(tmp_path):
    cases = []

    async def create(provider=None, **kwargs):
        provider = provider or HeldProvider("root")
        kernel = build_kernel(base_dir=tmp_path / str(len(cases)), model="root", provider=provider, **kwargs)
        await kernel.loop.start()
        work = []

        def start(coroutine):
            task = asyncio.create_task(coroutine)
            work.append(task)
            return task

        case = SimpleNamespace(kernel=kernel, provider=provider, start=start, work=work)
        cases.append(case)
        return case

    yield create
    for case in cases:
        if hasattr(case.provider, "unblock"):
            case.provider.unblock()
        for work in case.work:
            if not work.done():
                work.cancel()
        await asyncio.gather(*case.work, return_exceptions=True)
        case.kernel.session.close()


def controls(kernel):
    return kernel.loop.dispatcher.scope.budget.controls


def events(kernel):
    return read_session(kernel.session.base, kernel.session.id)


def requests(kernel):
    return [e for e in events(kernel) if isinstance(e.event, AgentRunCancelRequested)]


def row(kernel, session_id=None, runtime=None):
    return next(r for r in controls(kernel).snapshot()
                if (session_id is None or r.session_id == session_id) and (runtime is None or r.runtime == runtime))


async def test_root_stop_is_durable_waits_for_cleanup_and_keeps_budgets(make):
    case = await make(HeldProvider("root", cleanup=True))
    kernel, provider = case.kernel, case.provider
    kernel.tasks.create("Preserve the obligation")
    kernel.tasks.add_requirement({"id": "review", "description": "Inspect the work", "check": {"kind": "review"}})
    work = case.start(kernel.loop.run_task(kernel.tasks.prepare("secret prompt")))
    await asyncio.wait_for(provider.entered["root"].wait(), 3)
    live = row(kernel)
    budget = kernel.loop.dispatcher.scope.budget
    signal = budget.activity.snapshot()[0].last_signal
    grant = cancel_run(kernel, live.run_id[:8])
    assert grant.cancellation_requested and "cancelling" in grant.cancellation_blocked
    assert len(requests(kernel)) == 1 and "secret prompt" not in requests(kernel)[0].event.model_dump_json()
    assert budget.activity.snapshot()[0].last_signal == signal
    await asyncio.wait_for(provider.cleaning["root"].wait(), 3)
    assert not work.done() and controls(kernel).snapshot() and budget.runs.snapshot(kernel.session.id)
    assert not any(isinstance(e.event, AgentRunFinished) for e in events(kernel))
    with pytest.raises(ValueError, match="cancelling"):
        cancel_run(kernel, live.run_id)
    with pytest.raises(ValueError, match="cancelling"):
        extend_execution(kernel, live.run_id, 100)
    provider.cleanup.set()
    result = await asyncio.wait_for(work, 3)
    assert result.status == "cancelled" and result.reason == "operator_cancelled"
    assert result.acceptance == "unverified" and result.remaining_criteria
    assert provider.cleaned == {"root"} and budget.model_calls == 1
    assert not controls(kernel).snapshot() and not budget.activity.snapshot()
    assert requests(kernel)[0].seq < next(e.seq for e in events(kernel) if isinstance(e.event, AgentRunFinished))
    with pytest.raises(ValueError, match="one live run"):
        cancel_run(kernel, live.run_id)
    package, _ = task_package(kernel.session.base, kernel.session.id)
    assert package["cancellation_requests"] == [{"source_seq": requests(kernel)[0].seq,
        "run_id": result.run_id, "task_id": result.task_id, "target_session_id": kernel.session.id, "actor": "operator"}]
    assert not kernel.tasks.selected().accepted


async def test_stopped_expert_preserves_sibling_output_and_root_continuation(make):
    class EnsembleProvider(HeldProvider):
        async def infer(self, request):
            if request.model == "root":
                self.calls.append("root")
                chunks = (tool_call_turn("ensemble-call", "ensemble", {"prompt": "work", "models": ["a", "b"]})
                          if self.calls.count("root") == 1 else text_turn("root retained useful work"))
                for chunk in chunks:
                    yield chunk
            else:
                async for chunk in super().infer(request):
                    yield chunk

    provider = EnsembleProvider("a", "b")
    case = await make(provider)
    kernel = case.kernel
    kernel.tasks.create("Keep independent work")
    work = case.start(kernel.loop.run_task(kernel.tasks.prepare("ensemble please")))
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in provider.entered.values())), 3)
    child_a = next(r for r in controls(kernel).snapshot() if r.session_id != kernel.session.id and
                   any(e.event.type == "model_call_started" and e.event.model == "a"
                       for e in read_session(kernel.session.base, r.session_id)))
    root = row(kernel, kernel.session.id)
    assert child_a.parent_run_id == root.run_id
    cancel_run(kernel, child_a.run_id)
    await asyncio.wait_for(provider.cleaning["a"].wait(), 3)
    assert not provider.cleaning["b"].is_set() and not work.done()
    provider.release["b"].set()
    result = await asyncio.wait_for(work, 3)
    assert result.status == "completed" and result.read_text(kernel.session.blobs) == "root retained useful work"
    from tests.test_coordination_results import saved_report
    report = saved_report(kernel.session)
    assert report.result.status == "incomplete"
    assert [m.result.status for m in report.members] == ["cancelled", "completed"]
    assert report.members[0].result.reason == "operator_cancelled"
    assert report.members[1].result.output is not None
    assert kernel.session.blobs.get(report.output).decode() == "partial b"
    assert report.acceptance == "unverified"
    budget = kernel.loop.dispatcher.scope.budget
    assert budget.children == 3 and budget.model_calls == 4 and not budget.busy
    child_events = read_session(kernel.session.base, child_a.session_id)
    assert not any(isinstance(e.event, AgentRunCancelRequested) for e in child_events)
    assert next(e.event.result.status for e in child_events if isinstance(e.event, AgentRunFinished)) == "cancelled"
    package, _ = task_package(kernel.session.base, kernel.session.id)
    assert package["cancellation_requests"][0]["target_session_id"] == child_a.session_id


async def test_sequential_child_stop_returns_a_result_without_cancelling_its_caller(make):
    case = await make(HeldProvider("a", "b"))
    work = case.start(run_strategy_result("draft_refine", case.kernel.runner, case.kernel.session,
                                         "work", [Expert("a"), Expert("b")]))
    await asyncio.wait_for(case.provider.entered["a"].wait(), 3)
    cancel_run(case.kernel, row(case.kernel).run_id)
    result = await asyncio.wait_for(work, 3)
    assert result.status == "cancelled" and result.reason == "operator_cancelled"
    assert not work.cancelled() and not case.provider.entered["b"].is_set()


@pytest.mark.parametrize("target", ["harness", "codex"])
async def test_typed_external_and_outer_run_have_separate_stop_boundaries(make, target):
    class External(HeldProvider, ScriptedCodex):
        async def complete(self, **kwargs):
            async for chunk in self.generate("external"):
                yield chunk

    provider = External("external")
    case = await make(provider)
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="external work")))
    await asyncio.wait_for(provider.entered["external"].wait(), 3)
    live = controls(case.kernel).snapshot()
    assert {r.runtime for r in live} == {"harness", "codex"}
    cancel_run(case.kernel, row(case.kernel, runtime=target).run_id)
    result = await asyncio.wait_for(work, 3)
    assert result.status == "cancelled" and provider.cleaned == {"external"}
    terminals = [e.event.result for e in events(case.kernel) if isinstance(e.event, AgentRunFinished)]
    assert len(terminals) == 2 and all(r.status == "cancelled" for r in terminals)
    assert not fold(events(case.kernel)).open_agent_runs
    assert not controls(case.kernel).snapshot()


@pytest.mark.parametrize("written", [False, True])
async def test_failed_request_write_does_not_cancel_or_replay_after_restart(make, monkeypatch, written):
    case = await make()
    kernel = case.kernel
    work = case.start(kernel.loop.run_task(AgentTask(prompt="keep working")))
    await asyncio.wait_for(case.provider.entered["root"].wait(), 3)
    target = row(kernel).run_id
    original = kernel.session.append

    def fail(event):
        if isinstance(event, AgentRunCancelRequested):
            if written:
                original(event)
            raise OSError("request write failed")
        return original(event)

    with monkeypatch.context() as scoped:
        scoped.setattr(kernel.session, "append", fail)
        with pytest.raises(OSError):
            cancel_run(kernel, target)
    assert not row(kernel).cancellation_requested and not case.provider.cleaning["root"].is_set()
    case.provider.release["root"].set()
    assert (await asyncio.wait_for(work, 3)).status == "completed"
    sid, base = kernel.session.id, kernel.session.base
    kernel.session.close()
    resumed = build_kernel(base_dir=base, model="root", provider=FakeProvider([text_turn("new work")]), resume_session_id=sid)
    try:
        assert not controls(resumed).snapshot()
        with pytest.raises(ValueError, match="one live run"):
            cancel_run(resumed, target)
        assert (await resumed.loop.run_task(AgentTask(prompt="new work"))).status == "completed"
        assert len(requests(resumed)) == int(written)
    finally:
        resumed.session.close()


async def test_rewritten_request_cannot_cancel_a_run(make):
    case = await make()
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="work")))
    await asyncio.wait_for(case.provider.entered["root"].wait(), 3)
    case.kernel.session._redactors.append(lambda e: e.model_copy(update={"task_id": "changed"})
        if isinstance(e, AgentRunCancelRequested) else e)
    with pytest.raises(ValueError, match="cannot be rewritten"):
        cancel_run(case.kernel, row(case.kernel).run_id)
    assert not row(case.kernel).cancellation_requested
    case.provider.release["root"].set()
    assert (await work).status == "completed"


async def test_ids_and_operator_authority_refuse_without_writes(make):
    case = await make()
    other = await make()
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="work")))
    await asyncio.wait_for(case.provider.entered["root"].wait(), 3)
    target, before = row(case.kernel).run_id, events(case.kernel)
    for invalid in ("", target[:7], "g" * 32, "f" * 33, 12, "b" * 32):
        with pytest.raises(ValueError):
            cancel_run(case.kernel, invalid)
    with pytest.raises(ValueError, match="owning root"):
        controls(case.kernel).cancel(other.kernel.session, target)
    with pytest.raises(ValueError, match="one live run"):
        cancel_run(other.kernel, target)
    scope = case.kernel.loop.dispatcher.scope
    for context, value in ((current_scope, scope), (current_agent_run, object())):
        token = context.set(value)
        try:
            with pytest.raises(ValueError, match="root operator"):
                cancel_run(case.kernel, target)
        finally:
            context.reset(token)
    case.kernel.loop.dispatcher.scope = replace(scope, depth=1)
    with pytest.raises(ValueError, match="root operator"):
        cancel_run(case.kernel, target)
    case.kernel.loop.dispatcher.scope = scope
    assert events(case.kernel) == before
    case.provider.release["root"].set()
    await work


async def test_ambiguous_prefix_does_not_stop_either_concurrent_child(make, monkeypatch):
    case = await make(HeldProvider("a", "b"))
    identifiers = count()
    monkeypatch.setattr("harness.agent.uuid4", lambda: SimpleNamespace(hex=f"12345678{next(identifiers):024x}"))
    work = case.start(run_strategy_result("ensemble", case.kernel.runner, case.kernel.session,
                                         "work", [Expert("a"), Expert("b")]))
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in case.provider.entered.values())), 3)
    with pytest.raises(ValueError, match="one live run"):
        cancel_run(case.kernel, "12345678")
    assert not requests(case.kernel) and not any(r.cancellation_requested for r in controls(case.kernel).snapshot())
    cancel_run(case.kernel, controls(case.kernel).snapshot()[0].run_id)
    case.provider.release["b"].set()
    assert (await work).status == "incomplete"


@pytest.mark.parametrize("interruption", ["parent", "deadline"])
async def test_enclosing_cancellation_wins_and_repeated_cancel_preserves_cleanup(make, interruption):
    case = await make(HeldProvider("root", cleanup=True), execution_overrides={
        "task_timeout_seconds": .15 if interruption == "deadline" else 10})
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="work")))
    await asyncio.wait_for(case.provider.entered["root"].wait(), 3)
    cancel_run(case.kernel, row(case.kernel).run_id)
    await asyncio.wait_for(case.provider.cleaning["root"].wait(), 3)
    if interruption == "deadline":
        await asyncio.sleep(.2)
    else:
        for _ in range(2):
            work.cancel()
            await asyncio.sleep(.02)
    assert not work.done() and not case.provider.cleaned
    assert not any(isinstance(e.event, AgentRunFinished) for e in events(case.kernel))
    assert controls(case.kernel).snapshot()
    case.provider.cleanup.set()
    with pytest.raises(asyncio.CancelledError if interruption == "parent" else TimeoutError):
        await work
    terminal, = [e.event.result for e in events(case.kernel) if isinstance(e.event, AgentRunFinished)]
    assert terminal.reason == ("cancelled" if interruption == "parent" else "deadline")
    assert case.provider.cleaned == {"root"} and not controls(case.kernel).snapshot()


async def test_provider_swallowing_cancel_cannot_turn_stop_into_success(make):
    case = await make(HeldProvider("root", swallow=True))
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="work")))
    await asyncio.wait_for(case.provider.entered["root"].wait(), 3)
    cancel_run(case.kernel, row(case.kernel).run_id)
    assert (await work).status == "cancelled"


async def test_request_before_body_starts_records_one_terminal_without_provider_work(make, monkeypatch):
    case = await make()
    original = case.kernel.session.append

    def stop_at_start(event):
        result = original(event)
        if isinstance(event, AgentRunStarted):
            asyncio.get_running_loop().call_soon(cancel_run, case.kernel, event.run_id)
        return result

    monkeypatch.setattr(case.kernel.session, "append", stop_at_start)
    result = await case.kernel.loop.run_task(AgentTask(prompt="never execute"))
    assert result.status == "cancelled" and not case.provider.calls
    assert len([e for e in events(case.kernel) if isinstance(e.event, AgentRunFinished)]) == 1
    assert not any(e.event.type == "user_message" for e in events(case.kernel))


async def test_native_file_mutation_settles_before_targeted_run_terminal(make, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    from tests.test_handoff import permissions
    case = await make(FakeProvider([tool_call_turn("write-call", "write_file", {
        "file_path": "result.txt", "content": "completed mutation"})]), native_tools=True, workspace_root=root, permissions=permissions())
    tool = case.kernel.registry.get("write_file")
    entered, release, settled = (threading.Event() for _ in range(3))
    original = tool._write

    def hold(*args):
        entered.set()
        try:
            assert release.wait(5)
            return original(*args)
        finally:
            settled.set()

    monkeypatch.setattr(tool, "_write", hold)
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="write file")))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        cancel_run(case.kernel, row(case.kernel).run_id)
        await asyncio.sleep(.05)
        assert not work.done() and not settled.is_set()
        assert not any(isinstance(e.event, AgentRunFinished) for e in events(case.kernel))
        release.set()
        assert (await asyncio.wait_for(work, 3)).status == "cancelled"
        assert settled.is_set() and (root / "result.txt").read_text() == "completed mutation"
        observed = events(case.kernel)
        assert next(e.seq for e in observed if e.event.type == "tool_call_cancelled") < next(
            e.seq for e in observed if isinstance(e.event, AgentRunFinished))
    finally:
        release.set()


@pytest.mark.parametrize("width", [60, 120])
async def test_terminal_cancel_preserves_draft_queue_and_partial_response(tmp_path, width):
    from textual.widgets import Input
    from tests.test_tui import make_app
    from tests.test_tui_tasks import command
    from tests.test_tui_queue import screen_text
    provider = HeldProvider("test-model", cleanup=True)
    app = make_app(tmp_path, provider=provider)
    async with app.run_test(size=(width, 45)) as pilot:
        try:
            # make_app's default alias is authoritative for the controlled provider.
            alias = app.kernel.loop.model
            if alias not in provider.entered:
                provider.entered[alias] = asyncio.Event()
                provider.release[alias] = asyncio.Event()
                provider.cleaning[alias] = asyncio.Event()
            await command(app, pilot, "work")
            await asyncio.wait_for(provider.entered[alias].wait(), 3)
            await command(app, pilot, "next")
            prompt = app.query_one("#prompt", Input)
            prompt.value = "keep unsent draft"
            live = row(app.kernel)
            before = events(app.kernel)
            app._plugin_commands["activity"] = SimpleNamespace(body="must never run")
            await app._run_command(SlashCommand("activity", ""))
            assert events(app.kernel) == before
            assert f"/activity cancel {live.run_id}" in render_run_controls(controls(app.kernel))
            await app._run_command(SlashCommand("activity", f"cancel {live.run_id[:8]}"))
            await asyncio.wait_for(provider.cleaning[alias].wait(), 3)
            await pilot.pause(.1)
            assert "waiting for cleanup" in " ".join(screen_text(app).split())
            assert prompt.value == "keep unsent draft" and len(app.controller.pending) == 1
            provider.cleanup.set()
            async with asyncio.timeout(3):
                while app.controller.active is not None:
                    await pilot.pause(.02)
            assert app.controller.paused and app.controller.last_result.status == "cancelled"
            assert len(provider.calls) == 1 and len(app.controller.pending) == 1
            assert "partial" in screen_text(app) and prompt.value == "keep unsent draft"
            assert not controls(app.kernel).snapshot()
        finally:
            provider.unblock()


async def test_swallowed_cancellation_cannot_admit_more_model_tool_or_child_work(make):
    from harness.dispatcher import current_dispatch_tool
    from harness.hooks import ProposedToolCall
    from harness.tools import ToolSpec
    blocked, ran = [], []
    other_provider = FakeProvider([text_turn("must not run")])

    class Probe:
        spec = ToolSpec(name="probe", description="Observable side effect", parameters={})

        async def __call__(self, args):
            ran.append("tool")
            return "done"

    class Swallowing(HeldProvider):
        async def infer(self, request):
            self.entered["root"].set()
            try:
                await self.release["root"].wait()
            except asyncio.CancelledError:
                for label in ("tool", "model", "child"):
                    try:
                        if label == "tool":
                            await current_dispatch_tool.get()(ProposedToolCall(call_id="blocked-tool", tool="probe", args={}))
                        elif label == "model":
                            await case.kernel.loop.dispatcher.dispatch_model(provider=other_provider,
                                model="other", messages=[], tools=())
                        else:
                            await case.kernel.runner.run_result(prompt="more work", model=None, parent=case.kernel.session)
                    except asyncio.CancelledError:
                        blocked.append(label)
            for chunk in text_turn("must not become a completed response"):
                yield chunk

    case = await make(Swallowing("root"))
    case.kernel.registry.register(Probe())
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="work")))
    await asyncio.wait_for(case.provider.entered["root"].wait(), 3)
    cancel_run(case.kernel, row(case.kernel).run_id)
    assert (await work).status == "cancelled"
    assert blocked == ["tool", "model", "child"] and not ran and not other_provider.calls
    budget = case.kernel.loop.dispatcher.scope.budget
    assert (budget.model_calls, budget.tool_calls, budget.children) == (1, 0, 0)
    assert not any(e.event.type == "model_call_completed" for e in events(case.kernel))


async def test_request_stop_of_root_settles_all_parallel_children(make):
    class EnsembleProvider(HeldProvider):
        async def infer(self, request):
            if request.model == "root":
                for chunk in tool_call_turn("ensemble", "ensemble", {"prompt": "work", "models": ["a", "b"]}):
                    yield chunk
            else:
                async for chunk in super().infer(request):
                    yield chunk

    case = await make(EnsembleProvider("a", "b", cleanup=True))
    work = case.start(case.kernel.loop.run_task(AgentTask(prompt="work")))
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in case.provider.entered.values())), 3)
    cancel_run(case.kernel, row(case.kernel, case.kernel.session.id).run_id)
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in case.provider.cleaning.values())), 3)
    budget = case.kernel.loop.dispatcher.scope.budget
    assert budget.active_children == 2 and budget.active_coordinators == 1 and not work.done()
    case.provider.cleanup.set()
    assert (await work).status == "cancelled"
    assert case.provider.cleaned == {"a", "b"} and not budget.busy
    assert not controls(case.kernel).snapshot() and not budget.activity.snapshot()
    assert len(requests(case.kernel)) == 1
    children = [e.event for e in events(case.kernel) if e.event.type == "subagent_finished"]
    assert len(children) == 2 and all(c.status == "cancelled" for c in children)


async def test_provider_close_settles_once_despite_repeated_cancel_and_request_timeout():
    from harness.inference import InferenceRequest, collect_bounded
    entered, release = asyncio.Event(), asyncio.Event()

    class Source:
        def __init__(self):
            self.chunks = iter(text_turn("done"))
            self.closed = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                raise StopAsyncIteration from None

        async def aclose(self):
            entered.set()
            await release.wait()
            self.closed += 1

    source = Source()
    work = asyncio.create_task(collect_bounded(source, InferenceRequest(model="fake", messages=(), purpose="test", timeout_seconds=.1)))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        for _ in range(2):
            work.cancel()
            await asyncio.sleep(.01)
        await asyncio.sleep(.12)
        assert not work.done() and source.closed == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert source.closed == 1
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
