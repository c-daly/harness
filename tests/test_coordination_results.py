"""Coordination uses execution facts, retains provenance, and settles siblings."""

import asyncio
import json

import pytest

from harness.agent import DelegationResult
from harness.blobs import BlobStore
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.coordination import load_report
from harness.coordination_cli import main, render_coordination
from harness.events import CoordinationFinished, SubagentFinished
from harness.execution import ExecutionBudget, ExecutionLimits, ExecutionScope, current_scope
from harness.fold import fold
from harness.frontmatter import AgentDef
from harness.log import TornLogError, read_session
from harness.mixture import Expert, run_strategy_result
from harness.portable import task_package
from harness.provider import FakeProvider, StreamStop, TextDelta, text_turn, tool_call_turn
from harness.provider_litellm import CatalogProvider
from harness.session import Session
from harness.tui_panel import fold_agents
from harness.types import ModelId, SessionId, ToolName
from tests.test_external_agent_runtime import ScriptedCodex
from tests.test_mixture import FakeRunner
from tests.test_subagent import _runner


@pytest.fixture
def parent(tmp_path):
    with Session(tmp_path, SessionId("parent")) as session:
        session.start()
        yield session


def saved_report(parent):
    events = read_session(parent.base, parent.id)
    event = next(e.event for e in reversed(events) if isinstance(e.event, CoordinationFinished))
    return load_report(parent.blobs, event, parent.id)


async def test_literal_error_text_survives_real_children_with_provenance(parent, tmp_path):
    text = "[subagent error] is a literal example"
    runner = _runner(tmp_path, FakeProvider([text_turn(text), text_turn(text), text_turn("different")]))
    result = await run_strategy_result("ensemble", runner, parent, "Compare", [Expert("fake")] * 3)
    assert result.status == "completed" and result.text == text and result.acceptance == "unverified"
    report = saved_report(parent)
    assert report.disagreement and report.unresolved
    assert parent.blobs.get(report.output).decode() == text
    assert result.report_session_id == parent.id
    terminals = {e.event.child_session_id: e.event for e in read_session(tmp_path, parent.id)
                 if isinstance(e.event, SubagentFinished)}
    for member in report.members:
        child = member.result
        assert child.status == "completed" and child.run_id and child.output
        assert terminals[child.child_session_id].run_id == child.run_id
        assert terminals[child.child_session_id].output == child.output
        blobs = BlobStore(tmp_path / "sessions" / child.child_session_id / "blobs", create=False)
        assert blobs.get(child.output).decode() in {text, "different"}
        assert not fold(read_session(tmp_path, child.child_session_id)).open_agent_runs


@pytest.mark.parametrize("verdict", [
    DelegationResult(status="incomplete", text="PASS"),
    DelegationResult(status="failed", text="PASS"),
    DelegationResult(status="blocked", text="PASS"),
    DelegationResult(status="completed", text="PASS", truncated=True),
    DelegationResult(status="completed", text="PASSENGER"),
    DelegationResult(status="completed", text="PASS but I did not check"),
])
async def test_verifier_cannot_pass_on_partial_execution_or_prefix(parent, verdict):
    runner = FakeRunner({"cheap": "draft", "verify": verdict, "premium": "recovered"})
    result = await run_strategy_result("escalate", runner, parent, "Q",
        [Expert("cheap"), Expert("premium"), Expert("verify")])
    assert result.status == "completed" and result.text == "recovered"
    report = saved_report(parent)
    assert [m.role for m in report.members] == ["cheap", "verifier", "premium"]
    assert report.gate == "advisory_review" and report.acceptance == "unverified"
    assert report.members[1].result.status == verdict.status


async def test_truncated_child_is_not_a_complete_vote_and_keeps_full_artifact(parent, tmp_path):
    runner = _runner(tmp_path, FakeProvider([text_turn("PASS with more detail"), text_turn("complete sibling")]))
    runner.agents["terse"] = AgentDef(name="terse", description="d", body="x", max_output_chars=4)
    result = await run_strategy_result("ensemble", runner, parent, "Q",
        [Expert("fake", "terse"), Expert("fake")])
    assert result.status == "incomplete" and result.text == "complete sibling"
    partial = saved_report(parent).members[0].result
    assert partial.status == "completed" and partial.truncated
    blobs = BlobStore(tmp_path / "sessions" / partial.child_session_id / "blobs", create=False)
    assert blobs.get(partial.output) == b"PASS with more detail"


async def test_failed_critic_is_missing_review_not_disagreement(parent):
    runner = FakeRunner({"proposer": "answer", "critic": DelegationResult(status="failed")})
    result = await run_strategy_result("panel", runner, parent, "Q", [Expert("proposer"), Expert("critic")])
    report = saved_report(parent)
    assert result.status == "incomplete" and not report.disagreement
    assert report.unresolved == ["one or more participants did not deliver a complete result"]


async def test_negative_verifier_retains_disagreement_after_recovery(parent):
    runner = FakeRunner({"cheap": "draft", "verify": "FAIL\nIncorrect calculation", "premium": "recovered"})
    result = await run_strategy_result("escalate", runner, parent, "Q",
        [Expert("cheap"), Expert("premium"), Expert("verify")])
    report = saved_report(parent)
    assert result.status == "completed" and result.text == "recovered"
    assert report.disagreement and report.acceptance == "unverified"
    assert report.unresolved == ["participants disagreed; inspect the alternatives"]


async def test_failed_child_retains_failed_run_and_successful_sibling(parent, tmp_path):
    runner = _runner(tmp_path, FakeProvider([text_turn("useful partial work")]))
    result = await run_strategy_result("ensemble", runner, parent, "Q", [Expert("fake")] * 2)
    assert result.status == "incomplete" and result.text == "useful partial work"
    good, bad = saved_report(parent).members
    assert good.result.output and bad.result.status == "failed" and bad.result.run_id
    state = fold(read_session(tmp_path, bad.result.child_session_id))
    assert state.agent_runs[bad.result.run_id].status == "failed" and not state.open_agent_runs


async def test_model_budget_exhaustion_remains_incomplete_with_its_run_id(parent, tmp_path):
    runner = _runner(tmp_path, FakeProvider([]))
    budget = ExecutionBudget(ExecutionLimits(max_model_calls=0))
    token = current_scope.set(ExecutionScope(parent, runner.registry, budget))
    try:
        result = await runner.run_result(prompt="Q", model=None, parent=parent)
    finally:
        current_scope.reset(token)
    assert result.status == "incomplete" and result.reason == "budget" and result.run_id
    assert budget.active_children == 0 and not runner.provider.calls


async def test_unreadable_child_terminal_does_not_hide_parent_failure(parent, tmp_path, monkeypatch):
    runner = _runner(tmp_path, FakeProvider([]))

    def unreadable(*args, **kwargs):
        raise TornLogError("unreadable child")

    monkeypatch.setattr("harness.log.read_session", unreadable)
    result = await runner.run_result(prompt="Q", model=None, parent=parent)
    assert result.status == "failed" and result.run_id is None
    events = read_session(tmp_path, parent.id)
    assert events[-1].event.type == "subagent_finished" and events[-1].event.status == "error"
    assert any(e.event.type == "error_raised" and e.event.where == "subagent:outcome" for e in events)


async def test_cancellation_settles_children_before_coordination_terminal(parent, tmp_path):
    entered, release_cleanup = asyncio.Event(), asyncio.Event()
    started, closed = [], []

    class Waiting:
        async def complete(self, *, model, **kwargs):
            started.append(str(model))
            if len(started) == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
                yield  # pragma: no cover
            finally:
                await release_cleanup.wait()
                closed.append(str(model))

    runner = _runner(tmp_path, Waiting())
    task = asyncio.create_task(run_strategy_result("ensemble", runner, parent, "Q", [Expert("a"), Expert("b")]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not any(e.event.type == "coordination_finished" for e in read_session(tmp_path, parent.id))
    finally:
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert set(closed) == {"a", "b"}
    report = saved_report(parent)
    assert report.result.status == "cancelled"
    assert all(m.result.status == "cancelled" and m.result.run_id for m in report.members)
    assert runner._root_scopes[str(parent.id)].budget.active_children == 0
    events = read_session(tmp_path, parent.id)
    assert events[-1].event.type == "coordination_finished"
    for member in report.members:
        state = fold(read_session(tmp_path, member.result.child_session_id))
        assert not state.open_agent_runs and not state.open_model_intents


async def test_unexpected_runner_failure_cancels_and_awaits_sibling(parent):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class Broken:
        async def run_result(self, *, model, **kwargs):
            if model == "bad":
                await entered.wait()
                raise RuntimeError("internal detail")
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

    with pytest.raises(RuntimeError):
        await run_strategy_result("ensemble", Broken(), parent, "Q", [Expert("bad"), Expert("waiting")])
    assert cleaned.is_set()
    report = saved_report(parent)
    assert report.result.status == "failed" and report.result.reason == "RuntimeError"
    assert [m.result.status for m in report.members] == ["failed", "cancelled"]


@pytest.mark.parametrize("strategy,experts", [
    ("ensemble", []), ("ensemble", [Expert("fake")] * 17),
    ("draft_refine", [Expert("fake")] * 3), ("escalate", [Expert("fake")] * 4),
    ("unknown", [Expert("fake")]), ("ensemble", [Expert("")]),
])
async def test_invalid_fanout_is_blocked_before_any_child(parent, strategy, experts):
    runner = FakeRunner({})
    result = await run_strategy_result(strategy, runner, parent, "Q", experts)
    assert result.status == "blocked" and not runner.calls
    assert saved_report(parent).result.status == "blocked"


async def test_aggregate_output_is_bounded_without_utf8_damage(parent, monkeypatch):
    monkeypatch.setattr("harness.mixture.MAX_COORDINATION_OUTPUT", 5)
    result = await run_strategy_result("ensemble", FakeRunner({"fake": "ééé"}), parent, "Q", [Expert("fake")])
    assert result.status == "incomplete" and result.truncated and result.text == "éé"
    assert parent.blobs.get(saved_report(parent).output) == "éé".encode()


async def test_controlled_inference_and_external_runtime_share_report(parent, monkeypatch, tmp_path):
    # Exercise real routing/binding; scripted transports do not qualify live providers.
    monkeypatch.setattr("harness.catalog._cost_map_lookup", lambda _: {})

    async def inference(**kwargs):
        assert kwargs["model"] == "openai/fixture"
        yield TextDelta("inference answer")
        yield StreamStop("end_turn")

    monkeypatch.setattr("harness.provider_litellm._acomplete", inference)
    backend = ScriptedCodex([TextDelta("external answer"), StreamStop("end_turn")])
    catalog = Catalog({"model": {"route": "openai/fixture"},
                       "agent": {"route": "codex/default", "backend": "codex"}})
    runner = _runner(tmp_path, CatalogProvider(catalog, codex=backend))
    result = await run_strategy_result("ensemble", runner, parent, "Q", [Expert("model"), Expert("agent")])
    assert result.status == "completed" and backend.closed
    report = saved_report(parent)
    runtimes = []
    for member in report.members:
        events = read_session(tmp_path, member.result.child_session_id)
        runtimes.append([e.event.runtime for e in events if e.event.type == "agent_run_started"])
        assert member.result.status == "completed" and not fold(events).open_agent_runs
    assert runtimes == [["harness"], ["harness", "codex"]]
    assert report.disagreement and report.acceptance == "unverified"


async def test_task_export_and_activity_keep_configured_coordination_partial_status(tmp_path):
    provider = FakeProvider([
        tool_call_turn("", ToolName("dispatch_agent"), {"prompt": "Compare", "agent": "team"}),
        text_turn("useful answer"), text_turn("root summary"),
    ])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("fake"),
                          execution_limits=ExecutionLimits(max_children=2))
    runner = kernel.registry.get(ToolName("dispatch_agent")).runner
    runner.agents["team"] = AgentDef(name="team", description="d", body="", strategy="ensemble", experts=("fake", "fake"))
    try:
        await kernel.loop.start()
        kernel.tasks.create("Keep all outcomes")
        kernel.tasks.add_requirement({"id": "review", "description": "Operator reviews alternatives"})
        result = await kernel.loop.run_task(kernel.tasks.prepare("Work"))
        assert result.status == "completed" and not kernel.tasks.selected().accepted
        report = saved_report(kernel.session)
        assert report.result.status == "incomplete"
        assert [m.result.status for m in report.members] == ["completed", "blocked"]
        rows = fold_agents(read_session(tmp_path, kernel.session.id))
        assert rows[0].status == "incomplete"
        package, artifacts = task_package(tmp_path, kernel.session.id)
        coordination, = package["coordination"]
        assert coordination["status"] == "incomplete"
        assert json.loads(artifacts[coordination["report"]["path"]])["id"] == report.id
        assert artifacts[coordination["output"]["path"]] == b"useful answer"
        assert package["task"]["unresolved"] == ["review"] and not package["task"]["accepted"]
        assert package["child_sessions"][0]["contents"] == "not_exported"
    finally:
        kernel.session.close()


async def test_inspection_is_read_only_and_rejects_mismatched_report(parent, capsys):
    await run_strategy_result("ensemble", FakeRunner({"a": "A", "b": "B"}), parent, "Q", [Expert("a"), Expert("b")])
    before = read_session(parent.base, parent.id)
    main([parent.id, "--base-dir", str(parent.base)])
    assert "acceptance unverified" in capsys.readouterr().out
    assert read_session(parent.base, parent.id) == before
    terminal = before[-1].event
    parent.append(terminal.model_copy(update={"id": "another-report"}))
    with pytest.raises(ValueError, match="does not match"):
        render_coordination(parent.base, parent.id)


async def test_coordination_command_does_not_dispatch_plugin_or_model(tmp_path):
    from types import SimpleNamespace
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    from tests.test_tui_tasks import command

    app = make_app(tmp_path, provider=FakeProvider([]))
    app._plugin_commands["coordination"] = SimpleNamespace(body="must not run")
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        before = read_session(tmp_path, app.kernel.session.id)
        await command(app, pilot, "/coordination")
        await pilot.pause()
        assert "No recorded coordination outcomes." in screen_text(app)
        assert read_session(tmp_path, app.kernel.session.id) == before
        assert not app.kernel.provider.calls
