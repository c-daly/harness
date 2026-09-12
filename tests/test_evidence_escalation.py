"""Escalation uses declared task checks and fresh, attributable child records."""

import asyncio
import hashlib
import json
import threading

import pytest

from harness.cli import build_kernel
from harness.coordination import load_report
from harness.events import CoordinationFinished, SubagentSpawned
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def output_requirement():
    return {"id": "answer", "description": "Return the required answer", "check": {
        "kind": "output", "sha256": digest("correct")}}


def report_for(kernel):
    event = next(e.event for e in reversed(read_session(kernel.session.base, kernel.session.id))
                 if isinstance(e.event, CoordinationFinished))
    return load_report(kernel.session.blobs, event, kernel.session.id)


async def run_case(tmp_path, answers, declared, *, configure=None, root_tool="escalate", **args):
    provider = FakeProvider([
        tool_call_turn("", ToolName(root_tool), {
            "prompt": "Solve the task", "cheap": "cheap", "premium": "premium", **args}),
        *(text_turn(text) if isinstance(text, str) else text for text in answers), text_turn("root summary"),
    ])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("root"))
    await kernel.loop.start()
    kernel.tasks.create("Solve the task")
    for requirement in declared:
        kernel.tasks.add_requirement(requirement)
    try:
        if configure is not None:
            configure(kernel)
        await kernel.loop.run_task(kernel.tasks.prepare("Solve the task"))
        return kernel
    except BaseException:
        kernel.session.close()
        raise


@pytest.mark.parametrize("cheap,verdict,roles", [
    ("wrong", None, ["cheap", "premium"]),
    ("correct", "FAIL\nPrefer the premium response", ["cheap", "verifier", "premium"]),
    ("correct", "PASS", ["cheap", "verifier"]),
])
async def test_advisory_verifier_cannot_override_checks(tmp_path, cheap, verdict, roles):
    answers = [cheap, *([verdict] if verdict is not None else []), *(["correct"] if "premium" in roles else [])]
    kernel = await run_case(tmp_path, answers, [output_requirement()], verify="judge")
    try:
        report = report_for(kernel)
        assert report.gate == "recorded_checks" and [m.role for m in report.members] == roles
        assert report.result.status == "completed"
        for m in report.members:
            assert (m.evidence is None) == (m.role == "verifier")
    finally:
        kernel.session.close()


async def test_model_arguments_cannot_remove_existing_requirements(tmp_path):
    kernel = await run_case(tmp_path, ["wrong", "correct"], [output_requirement()],
                            require_checks=False, requirements=[], acceptance="passed")
    try:
        report = report_for(kernel)
        assert report.gate == "recorded_checks" and len(report.members) == 2
        assert len(report.requirements) == 1 and report.requirements[0].check.sha256 == digest("correct")
    finally:
        kernel.session.close()


async def test_all_declared_requirements_must_pass(tmp_path):
    kernel = await run_case(tmp_path, ["correct", "correct"], [output_requirement(), {
        "id": "review", "description": "User review remains necessary"}])
    try:
        report = report_for(kernel)
        assert report.result.status == "incomplete" and len(report.members) == 2
        for member in report.members:
            assert [e.status for e in member.evidence] == ["passed", "unverified"]
    finally:
        kernel.session.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_run", "wrong_text", "duplicate_delivery"])
@pytest.mark.parametrize("strategy", ["escalate", "ensemble"])
async def test_fresh_child_provenance_is_required_even_after_a_passing_check(tmp_path, monkeypatch, damage, strategy):
    from harness.events import SubagentFinished

    def configure(kernel):
        original = kernel.runner.run_result

        async def damaged(**kwargs):
            result = await original(**kwargs)
            if str(kwargs["model"]) == "cheap":
                if damage in ("missing", "corrupt"):
                    path = tmp_path / "sessions" / result.child_session_id / "blobs" / result.output.sha256
                    if damage == "missing":
                        path.unlink()
                    else:
                        path.write_bytes(b"corrupt")
                elif damage == "wrong_run":
                    result = result.model_copy(update={"run_id": "unrelated"})
                elif damage == "wrong_text":
                    result = result.model_copy(update={"text": "fabricated"})
                else:
                    terminal = next(e.event for e in reversed(read_session(tmp_path, kernel.session.id))
                                    if isinstance(e.event, SubagentFinished) and e.event.child_session_id == result.child_session_id)
                    kernel.session.append(terminal)
            return result

        monkeypatch.setattr(kernel.runner, "run_result", damaged)

    kernel = await run_case(tmp_path, ["correct", "correct"], [output_requirement()], configure=configure,
                            root_tool=strategy, models=["cheap", "premium"])
    try:
        report = report_for(kernel)
        assert [e.evidence[0].status for e in report.members] == ["unverified", "passed"]
        assert report.result.status == "completed"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["wrong_args", "error", "later_failure", "blocked", "absent"])
async def test_tool_checks_require_matching_successful_current_child_calls(tmp_path, mode):
    from harness.hooks import Allow, Block, ProposedToolCall
    from harness.native_tools import ToolError
    from harness.tools import ToolSpec

    class Verify:
        spec = ToolSpec(name=ToolName("verify"), description="Return fixture check output", parameters={})
        count = 0

        async def __call__(self, args):
            self.count += 1
            if mode == "error" and self.count == 1 or mode == "later_failure" and self.count == 2:
                raise ToolError("FAIL")
            return "PASS"

    def configure(kernel):
        kernel.registry.register(Verify())
        if mode == "blocked":
            blocked = False

            async def once(action):
                nonlocal blocked
                if isinstance(action, ProposedToolCall) and action.tool == "verify" and not blocked:
                    blocked = True
                    return Block("fixture permission denial")
                return Allow()

            kernel.hooks.register_dispatch("deny-first-check", once)

    check = tool_call_turn("", ToolName("verify"), {"target": "trusted"})
    initial = tool_call_turn("", ToolName("verify"), {"target": "wrong" if mode == "wrong_args" else "trusted"})
    answers = [*([initial] if mode != "absent" else []), *([check] if mode == "later_failure" else []),
               "PASS", check, "verified"]
    kernel = await run_case(tmp_path, answers, [{"id": "verify", "description": "Run the fixture check", "check": {
        "kind": "tool_result", "tool": "verify", "args": {"target": "trusted"}, "sha256": digest("PASS")}}], configure=configure)
    try:
        report = report_for(kernel)
        assert len(report.members) == 2 and report.result.status == "completed"
        assert report.members[0].evidence[0].status != "passed"
        evidence = report.members[1].evidence[0]
        assert evidence.status == "passed" and evidence.call_id and evidence.source_seq
        assert evidence.actual_sha256 == digest("PASS")
    finally:
        kernel.session.close()


async def test_matching_partial_output_is_not_a_pass(tmp_path):
    from harness.provider import StreamStop, TextDelta
    kernel = await run_case(tmp_path, [[TextDelta("correct"), StreamStop("max_tokens")], "correct"], [output_requirement()])
    try:
        report = report_for(kernel)
        assert report.result.status == "completed" and len(report.members) == 2
        assert report.members[0].result.status == "incomplete"
        assert report.members[0].evidence[0].status == "unverified"
        assert report.members[1].evidence[0].status == "passed"
    finally:
        kernel.session.close()


async def test_configured_escalation_uses_the_same_core_checks(tmp_path):
    from harness.frontmatter import AgentDef

    def configure(kernel):
        kernel.runner.agents["checked"] = AgentDef(name="checked", description="Checked fallback",
            strategy="escalate", experts=("cheap", "premium"), require_checks=True)

    kernel = await run_case(tmp_path, ["wrong", "correct"], [output_requirement()], configure=configure,
                            root_tool="dispatch_agent", agent="checked")
    try:
        report = report_for(kernel)
        assert report.gate == "recorded_checks" and len(report.members) == 2
        assert report.result.status == "completed" and report.check_source.task_id == kernel.tasks.selected().definition.id
    finally:
        kernel.session.close()


def test_require_checks_definition_is_strict_and_requires_a_supported_strategy():
    from harness.frontmatter import AgentDef
    with pytest.raises(ValueError, match="only supported"):
        AgentDef(name="plain", description="d", require_checks=True)
    with pytest.raises(ValueError):
        AgentDef(name="checked", description="d", strategy="escalate", experts=("a", "b"), require_checks="false")


async def test_inspection_export_and_replay_keep_checks_without_reexecuting(tmp_path):
    from harness.coordination_cli import render_coordination
    from harness.portable import task_package
    kernel = await run_case(tmp_path, ["wrong", "correct"], [output_requirement()])
    sid = kernel.session.id
    try:
        before = read_session(tmp_path, sid)
        report = report_for(kernel)
        visible = render_coordination(tmp_path, sid)
        assert "recorded_checks" in visible and "answer: failed" in visible and "answer: passed" in visible
        assert "child event" in visible and "child blob" in visible
        package, artifacts = task_package(tmp_path, sid)
        stored = json.loads(artifacts[package["coordination"][0]["report"]["path"]])
        assert stored["requirements"][0]["check"]["sha256"] == digest("correct")
        assert stored["check_source"]["task_id"] == kernel.tasks.selected().definition.id
        assert [m["evidence"][0]["status"] for m in stored["members"]] == ["failed", "passed"]
        assert report.check_source.session_id == sid and not package["task"]["accepted"]
        assert read_session(tmp_path, sid) == before
    finally:
        kernel.session.close()
    resumed = build_kernel(base_dir=tmp_path, provider=FakeProvider([]), model=ModelId("root"), resume_session_id=sid)
    try:
        assert "answer: passed" in render_coordination(tmp_path, sid)
        assert not resumed.provider.calls and not resumed.tasks.selected().accepted
    finally:
        resumed.session.close()


@pytest.mark.parametrize("cheap,premium,expected", [
    ("correct", None, "completed"), ("wrong", "correct", "completed"),
    ("PASS", "also wrong", "incomplete"),
])
async def test_declared_output_check_gates_both_cheap_and_premium(tmp_path, cheap, premium, expected):
    answers = [cheap, *([premium] if premium is not None else [])]
    kernel = await run_case(tmp_path, answers, [output_requirement()])
    try:
        report = report_for(kernel)
        assert report.gate == "recorded_checks"
        assert [m.role for m in report.members] == (["cheap"] if premium is None else ["cheap", "premium"])
        assert report.result.status == expected and report.acceptance == "unverified"
        assert report.members[0].evidence[0].status == ("passed" if cheap == "correct" else "failed")
        if premium is not None:
            assert report.members[1].evidence[0].status == ("passed" if premium == "correct" else "failed")
        assert not kernel.tasks.selected().accepted
        for member in report.members:
            assert member.evidence[0].artifact == member.result.output
            assert member.evidence[0].source_seq is not None
    finally:
        kernel.session.close()


async def test_user_review_requirement_cannot_be_satisfied_by_pass_prose(tmp_path):
    kernel = await run_case(tmp_path, ["PASS", "PASS"], [{"id": "review", "description": "User reviews the answer"}])
    try:
        report = report_for(kernel)
        assert report.result.status == "incomplete"
        assert [m.evidence[0].status for m in report.members] == ["unverified", "unverified"]
        assert not kernel.tasks.selected().accepted
    finally:
        kernel.session.close()


async def test_required_checks_without_declared_requirements_block_before_children(tmp_path):
    kernel = await run_case(tmp_path, [], [], require_checks=True)
    try:
        report = report_for(kernel)
        assert report.result.status == "blocked" and "requirements" in report.result.reason
        assert not report.members
        assert not any(isinstance(e.event, SubagentSpawned)
                       for e in read_session(kernel.session.base, kernel.session.id))
    finally:
        kernel.session.close()


@pytest.mark.parametrize("strategy", ["escalate", "ensemble"])
async def test_gate_follows_the_running_task_not_a_different_selected_task(tmp_path, strategy):
    from harness.tasks import TaskService, project_tasks
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([
        tool_call_turn("", ToolName(strategy), {"prompt": "Solve", "cheap": "cheap", "premium": "premium", "models": ["cheap"]}),
        text_turn("correct"), text_turn("root summary"),
    ]), model=ModelId("root"))
    try:
        await kernel.loop.start()
        owner = kernel.tasks.create("Actual task")
        kernel.tasks.add_requirement(output_requirement())
        task = kernel.tasks.prepare("Solve")
        other = kernel.tasks.create("Unrelated selected task")
        kernel.tasks.add_requirement({"id": "review", "description": "Review unrelated work"})
        await kernel.loop.run_task(task)
        report = report_for(kernel)
        assert report.check_source.task_id == owner.id and len(report.members) == 1
        assert report.requirements[0].id == "answer"
        child = project_tasks(read_session(tmp_path, report.members[0].result.child_session_id))
        assert child.items[child.selected_id].definition.title == owner.title
        assert TaskService(kernel.session).selected().definition.id == other.id
    finally:
        kernel.session.close()


@pytest.mark.parametrize("cause", ["cancel", "deadline"])
async def test_interrupted_premium_keeps_cheap_evidence_and_settles_children(tmp_path, cause):
    from dataclasses import replace
    from harness.events import SubagentFinished
    from harness.fold import fold

    entered, settled = asyncio.Event(), asyncio.Event()

    class Waiting(FakeProvider):
        async def complete(self, *, model, **kwargs):
            if str(model) == "premium":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    settled.set()
            else:
                async for chunk in super().complete(model=model, **kwargs):
                    yield chunk

    kernel = build_kernel(base_dir=tmp_path, provider=Waiting([
        tool_call_turn("", ToolName("escalate"), {"prompt": "Solve", "cheap": "cheap", "premium": "premium"}),
        text_turn("wrong"), text_turn("root summary"),
    ]), model=ModelId("root"))
    budget = kernel.loop.dispatcher.scope.budget
    # Setup includes the cheap child's persisted evidence checks. Expire the
    # actual coordinator only after premium entry, independently of CI speed.
    budget.limits = replace(budget.limits, coordination_timeout_seconds=10)
    work = None
    try:
        await kernel.loop.start()
        kernel.tasks.create("Interruptible checked fallback")
        kernel.tasks.add_requirement(output_requirement())
        work = asyncio.create_task(kernel.loop.run_task(kernel.tasks.prepare("Solve")))
        await asyncio.wait_for(entered.wait(), 3)
        if cause == "cancel":
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        else:
            coordinator, = budget.coordinations._coordinations.values()
            assert coordinator.strategy == "escalate" and coordinator.session is kernel.session
            coordinator.timer.reschedule(asyncio.get_running_loop().time())
            await asyncio.wait_for(work, 3)
        report = report_for(kernel)
        assert report.result.status == ("cancelled" if cause == "cancel" else "incomplete")
        if cause == "deadline":
            assert coordinator.timer.expired() and report.result.reason == "coordination deadline"
        assert report.members[0].evidence[0].status == "failed"
        assert report.members[1].result.status == "cancelled" and report.members[1].evidence is None
        events = read_session(tmp_path, kernel.session.id)
        terminal = next(e for e in events if isinstance(e.event, CoordinationFinished))
        assert all(e.seq < terminal.seq for e in events if isinstance(e.event, SubagentFinished))
        assert settled.is_set() and not budget.busy
        assert not fold(events).open_agent_runs
        for member in report.members:
            assert not fold(read_session(tmp_path, member.result.child_session_id)).open_agent_runs
    finally:
        if work is not None and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        kernel.session.close()


@pytest.mark.parametrize("strategy", ["escalate", "ensemble"])
async def test_runtime_contract_checks_native_then_external_output(tmp_path, monkeypatch, strategy):
    from harness.catalog import Catalog
    from harness.provider import StreamStop, TextDelta
    from harness.provider_litellm import CatalogProvider
    from tests.test_external_agent_runtime import ScriptedCodex

    monkeypatch.setattr("harness.catalog._cost_map_lookup", lambda _: {})
    steps = [tool_call_turn("", ToolName(strategy), {
        "prompt": "Solve", "cheap": "native", "premium": "external", "models": ["native", "external"], "require_checks": True}),
        text_turn("wrong"), text_turn("root summary")]

    async def inference(**kwargs):
        for chunk in steps.pop(0):
            yield chunk

    monkeypatch.setattr("harness.provider_litellm._acomplete", inference)
    backend = ScriptedCodex([TextDelta("correct"), StreamStop("end_turn")])
    catalog = Catalog({"native": {"route": "openai/fixture"},
                       "external": {"route": "codex/default", "backend": "codex"}})
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(catalog, codex=backend), model=ModelId("native"))
    try:
        await kernel.loop.start()
        kernel.tasks.create("Mixed contracts")
        kernel.tasks.add_requirement(output_requirement())
        await kernel.loop.run_task(kernel.tasks.prepare("Solve"))
        report = report_for(kernel)
        assert report.result.status == "completed" and backend.closed
        assert [m.evidence[0].status for m in report.members] == ["failed", "passed"]
        events = read_session(tmp_path, report.members[1].result.child_session_id)
        assert [e.event.runtime for e in events if e.event.type == "agent_run_started"] == ["harness", "codex"]
        assert any("Task acceptance criteria" in m.text() for m in backend.received["messages"])
    finally:
        kernel.session.close()


@pytest.mark.parametrize("strategy", ["escalate", "ensemble"])
async def test_terminal_exposes_failed_and_passing_checks_without_accepting_task(tmp_path, strategy):
    from textual.widgets import Input
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    from tests.test_tui_tasks import command

    provider = FakeProvider([
        tool_call_turn("", ToolName(strategy), {"prompt": "Solve", "cheap": "cheap", "premium": "premium",
                                              "models": ["cheap", "premium"], "require_checks": True}),
        text_turn("wrong"), text_turn("correct"), text_turn("root summary"),
    ])
    app = make_app(tmp_path, provider=provider, model=ModelId("root"))
    async with app.run_test(size=(180, 55)) as pilot:
        await command(app, pilot, "/task new Solve with checked fallback")
        await command(app, pilot, "/task require-json " + json.dumps(output_requirement()))
        await command(app, pilot, "Solve")
        if app._turn_worker is not None:
            await asyncio.wait_for(app._turn_worker.wait(), 3)
        before = read_session(tmp_path, app.kernel.session.id)
        await command(app, pilot, "/coordination")
        app.query_one("#prompt", Input).value = "keep this draft"
        await pilot.pause(.1)
        visible = " ".join(screen_text(app).split())
        assert "recorded_checks" in visible
        assert "answer: failed: recorded bytes differ" in visible and "answer: passed: recorded bytes match" in visible
        assert "acceptance unverified" in visible and "keep this draft" in visible
        assert read_session(tmp_path, app.kernel.session.id) == before
        assert not app.kernel.tasks.selected().accepted


@pytest.mark.parametrize("cause", ["cancel", "deadline"])
@pytest.mark.parametrize("strategy", ["escalate", "ensemble"])
async def test_late_read_only_verification_cannot_revive_an_interrupted_coordinator(tmp_path, monkeypatch, cause, strategy):
    from dataclasses import replace
    from harness.coordination_checks import check_child_result
    from harness.coordination_cli import render_coordination

    entered, release, settled = (threading.Event() for _ in range(3))

    def delayed(*args, **kwargs):
        try:
            result = check_child_result(*args, **kwargs)
            entered.set()
            assert release.wait(5)
            return result
        finally:
            settled.set()

    monkeypatch.setattr("harness.coordination_checks.check_child_result", delayed)
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([
        tool_call_turn("", ToolName(strategy), {"prompt": "Solve", "cheap": "cheap", "premium": "premium", "models": ["cheap"]}),
        text_turn("correct"), text_turn("root summary"),
    ]), model=ModelId("root"))
    budget = kernel.loop.dispatcher.scope.budget
    budget.limits = replace(budget.limits, coordination_timeout_seconds=10)
    work = None
    try:
        await kernel.loop.start()
        kernel.tasks.create("Interruptible verification")
        kernel.tasks.add_requirement(output_requirement())
        work = asyncio.create_task(kernel.loop.run_task(kernel.tasks.prepare("Solve")))
        assert await asyncio.to_thread(entered.wait, 3)
        if cause == "cancel":
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        else:
            coordinator, = budget.coordinations._coordinations.values()
            assert coordinator.strategy == strategy
            coordinator.timer.reschedule(asyncio.get_running_loop().time())
            await asyncio.wait_for(work, 3)
        report = report_for(kernel)
        assert report.result.status == ("cancelled" if cause == "cancel" else "incomplete")
        assert len(report.members) == 1 and report.members[0].result.status == "completed"
        assert report.members[0].evidence is None and not budget.busy
        assert "Checks unconfirmed" in render_coordination(tmp_path, kernel.session.id)
        before = read_session(tmp_path, kernel.session.id)
        release.set()
        assert await asyncio.to_thread(settled.wait, 3)
        await asyncio.sleep(.02)
        assert read_session(tmp_path, kernel.session.id) == before
    finally:
        release.set()
        if work is not None and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        if entered.is_set():
            assert await asyncio.to_thread(settled.wait, 3)
        kernel.session.close()


async def test_report_requires_a_complete_evidence_set_and_source(tmp_path):
    from harness.coordination import CoordinationReport
    kernel = await run_case(tmp_path, ["correct"], [output_requirement()])
    try:
        raw = report_for(kernel).model_dump(mode="json")
        raw["members"][0]["evidence"] = []
        with pytest.raises(ValueError, match="every declared requirement"):
            CoordinationReport.model_validate(raw)
        raw = report_for(kernel).model_dump(mode="json")
        raw["check_source"] = None
        with pytest.raises(ValueError, match="task source"):
            CoordinationReport.model_validate(raw)
        raw = report_for(kernel).model_dump(mode="json")
        raw["check_source"]["session_id"] = "unrelated"
        with pytest.raises(ValueError, match="calling session"):
            CoordinationReport.model_validate(raw)
    finally:
        kernel.session.close()


async def test_older_advisory_reports_remain_readable_without_invented_evidence(tmp_path):
    from harness.coordination import CoordinationReport
    kernel = await run_case(tmp_path, ["answer"], [])
    try:
        raw = report_for(kernel).model_dump(mode="json")
        raw.pop("requirements")
        raw.pop("check_source")
        for member in raw["members"]:
            member.pop("evidence")
        legacy = CoordinationReport.model_validate(raw)
        assert legacy.gate == "execution_only" and legacy.requirements is None and legacy.check_source is None
        assert all(m.evidence is None for m in legacy.members)
    finally:
        kernel.session.close()
