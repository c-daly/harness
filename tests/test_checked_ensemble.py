"""Ensemble votes and synthesis cannot override frozen task requirements."""

import hashlib
import json
import threading
from contextlib import asynccontextmanager

import pytest

from harness.cli import build_kernel
from harness.coordination import load_report
from harness.events import CoordinationFinished, SubagentSpawned
from harness.frontmatter import AgentDef
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn


def requirement():
    return {"id": "answer", "description": "Return the expected output", "check": {
        "kind": "output", "sha256": hashlib.sha256(b"correct").hexdigest()}}


class RoutedProvider(FakeProvider):
    def __init__(self, scripts):
        super().__init__([])
        self.scripts = scripts
        self.models = []
        self.messages = {}

    async def complete(self, *, model, messages, tools=()):
        self.models.append(str(model))
        self.calls.append(tuple(messages))
        self.messages[str(model)] = tuple(messages)
        for chunk in self.scripts[str(model)].pop(0):
            yield chunk


@asynccontextmanager
async def case(tmp_path, answers, *, declared=None, judge=None, configure=None, configured=False, **args):
    scripts = {name: [text_turn(value)] if isinstance(value, str) else value for name, value in answers.items()}
    if judge is not None:
        scripts["judge"] = [text_turn(judge)]
    tool = "dispatch_agent" if configured else "ensemble"
    params = ({"agent": "team"} if configured else {"models": list(answers), **({"judge": "judge"} if judge is not None else {})})
    scripts["root"] = [tool_call_turn("", tool, {"prompt": "Solve", **params, **args}), text_turn("root summary")]
    provider = RoutedProvider(scripts)
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model="root")
    try:
        await kernel.loop.start()
        kernel.tasks.create("Solve the user's task")
        for item in [requirement()] if declared is None else declared:
            kernel.tasks.add_requirement(item)
        if configured:
            kernel.runner.agents["team"] = AgentDef(name="team", description="Checked ensemble",
                strategy="ensemble", experts=tuple(answers), require_checks=True)
        if configure is not None:
            configure(kernel)
        await kernel.loop.run_task(kernel.tasks.prepare("Solve"))
        yield kernel
    finally:
        kernel.session.close()


def report_for(kernel):
    terminal = next(e.event for e in reversed(read_session(kernel.session.base, kernel.session.id))
                    if isinstance(e.event, CoordinationFinished))
    return load_report(kernel.session.blobs, terminal, kernel.session.id)


async def test_failed_majority_cannot_outvote_passing_evidence(tmp_path):
    async with case(tmp_path, {"a": "wrong", "b": "wrong", "c": "correct"},
                    require_checks=False, requirements=[], acceptance="passed") as kernel:
        report = report_for(kernel)
        assert kernel.session.blobs.get(report.output) == b"correct"
        assert report.result.status == "completed" and report.gate == "recorded_checks"
        assert report.disagreement and report.acceptance == "unverified"
        assert [m.evidence[0].status for m in report.members] == ["failed", "failed", "passed"]
        assert not kernel.tasks.selected().accepted
        assert report.check_source.task_id == kernel.tasks.selected().definition.id
        assert all(any("Task acceptance criteria" in m.text() for m in kernel.provider.messages[name])
                   for name in ("a", "b", "c"))


@pytest.mark.parametrize("judge", [None, "correct"])
async def test_no_passing_candidate_is_incomplete_without_judge_call(tmp_path, judge):
    async with case(tmp_path, {"a": "wrong", "b": "wrong"}, judge=judge) as kernel:
        report = report_for(kernel)
        assert report.result.status == "incomplete" and report.gate == "recorded_checks"
        assert "no expert passed" in report.result.reason
        assert len(report.members) == 2 and "judge" not in kernel.provider.models
        assert all(m.result.status == "completed" and m.evidence[0].status == "failed" for m in report.members)


@pytest.mark.parametrize("judge,status", [("wrong", "incomplete"), ("correct", "completed")])
async def test_judge_sees_only_passing_answers_and_must_pass_its_own_checks(tmp_path, judge, status):
    async with case(tmp_path, {"a": "REJECTED_CANDIDATE", "b": "correct"}, judge=judge) as kernel:
        report = report_for(kernel)
        assert report.result.status == status and report.gate == "recorded_checks"
        assert [m.role for m in report.members] == ["expert", "expert", "judge"]
        assert report.members[-1].evidence[0].status == ("passed" if judge == "correct" else "failed")
        prompt = "\n".join(m.text() for m in kernel.provider.messages["judge"])
        assert "REJECTED_CANDIDATE" not in prompt and "correct" in prompt
        assert "Task acceptance criteria" in prompt


async def test_review_requirement_cannot_be_voted_into_a_pass(tmp_path):
    async with case(tmp_path, {"a": "correct", "b": "correct"}, declared=[requirement(), {
        "id": "review", "description": "User must review the work"}]) as kernel:
        report = report_for(kernel)
        assert report.result.status == "incomplete"
        assert all([e.status for e in m.evidence] == ["passed", "unverified"] for m in report.members)


@pytest.mark.parametrize("value", [True, "false", 1])
async def test_required_or_invalid_checks_block_before_children(tmp_path, value):
    async with case(tmp_path, {"a": "correct"}, declared=[], require_checks=value) as kernel:
        events = read_session(tmp_path, kernel.session.id)
        if value is True:
            report = report_for(kernel)
            assert report.result.status == "blocked" and not report.members
        else:
            # Model-response validation rejects the malformed argument before
            # tool dispatch; the direct caller is checked independently below.
            assert not any(isinstance(e.event, CoordinationFinished) for e in events)
        assert not any(isinstance(e.event, SubagentSpawned) for e in events)
        assert kernel.provider.models == ["root", "root"]


async def test_configured_ensemble_uses_same_checks(tmp_path):
    async with case(tmp_path, {"a": "wrong", "b": "correct"}, configured=True) as kernel:
        report = report_for(kernel)
        assert report.gate == "recorded_checks" and report.result.status == "completed"
        assert [m.evidence[0].status for m in report.members] == ["failed", "passed"]


async def test_without_requirements_legacy_vote_remains_available(tmp_path):
    async with case(tmp_path, {"a": "wrong", "b": "wrong", "c": "correct"}, declared=[]) as kernel:
        report = report_for(kernel)
        assert kernel.session.blobs.get(report.output) == b"wrong"
        assert report.gate == "text_vote" and report.requirements is None
        assert all(m.evidence is None for m in report.members)


@pytest.mark.parametrize("value", ["false", 0, 1, None])
async def test_direct_tool_and_agent_definition_reject_nonboolean_checks(tmp_path, value):
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([]), model="root")
    try:
        await kernel.loop.start()
        await kernel.registry.get("ensemble")({"prompt": "Solve", "models": ["a"], "require_checks": value})
        report = report_for(kernel)
        assert report.result.status == "blocked" and not report.members
        assert not kernel.provider.calls
        with pytest.raises(ValueError):
            AgentDef(name="team", description="Invalid gate", strategy="ensemble", experts=("a",), require_checks=value)
    finally:
        kernel.session.close()


async def test_out_of_order_checks_remain_attached_to_their_own_participant(tmp_path, monkeypatch):
    from harness.coordination_checks import check_child_result

    checked = threading.Event()
    completed = []

    def reversed_checks(parent, result, *args, **kwargs):
        if result.text == "wrong":
            assert checked.wait(5)
        evidence = check_child_result(parent, result, *args, **kwargs)
        completed.append(result.text)
        if result.text == "correct":
            checked.set()
        return evidence

    monkeypatch.setattr("harness.coordination_checks.check_child_result", reversed_checks)
    async with case(tmp_path, {"a": "wrong", "b": "correct"}) as kernel:
        report = report_for(kernel)
        assert completed == ["correct", "wrong"]
        assert [m.evidence[0].status for m in report.members] == ["failed", "passed"]
        assert kernel.session.blobs.get(report.output) == b"correct"


async def test_matching_tool_evidence_is_required_not_a_pass_answer(tmp_path):
    from harness.tools import ToolSpec

    class Verify:
        spec = ToolSpec(name="verify", description="Public fixture check", parameters={})
        calls = 0

        async def __call__(self, args):
            self.calls += 1
            return "PASS"

    verifier = Verify()
    answers = {
        "a": [tool_call_turn("", "verify", {"target": "wrong"}), text_turn("PASS")],
        "b": [tool_call_turn("", "verify", {"target": "trusted"}), text_turn("verified answer")],
    }
    declared = [{"id": "verify", "description": "Check the declared fixture", "check": {
        "kind": "tool_result", "tool": "verify", "args": {"target": "trusted"},
        "sha256": hashlib.sha256(b"PASS").hexdigest()}}]
    async with case(tmp_path, answers, declared=declared,
                    configure=lambda k: k.registry.register(verifier)) as kernel:
        report = report_for(kernel)
        assert report.result.status == "completed" and verifier.calls == 2
        assert report.members[0].evidence[0].status != "passed"
        assert report.members[1].evidence[0].status == "passed"
        assert report.members[1].evidence[0].call_id is not None
        assert kernel.session.blobs.get(report.output) == b"verified answer"


async def test_export_inspection_and_restart_preserve_both_grades_without_execution(tmp_path):
    from harness.coordination_cli import render_coordination
    from harness.portable import task_package

    async with case(tmp_path, {"a": "wrong", "b": "correct"}) as kernel:
        sid = kernel.session.id
        before = read_session(tmp_path, sid)
        visible = render_coordination(tmp_path, sid)
        assert "answer: failed" in visible and "answer: passed" in visible
        package, artifacts = task_package(tmp_path, sid)
        saved = json.loads(artifacts[package["coordination"][0]["report"]["path"]])
        assert [m["evidence"][0]["status"] for m in saved["members"]] == ["failed", "passed"]
        assert saved["check_source"]["task_id"] == package["task"]["id"]
        assert not package["task"]["accepted"]
        assert read_session(tmp_path, sid) == before
    resumed = build_kernel(base_dir=tmp_path, provider=FakeProvider([]), model="root", resume_session_id=sid)
    try:
        assert "answer: failed" in render_coordination(tmp_path, sid)
        assert not resumed.tasks.selected().accepted and not resumed.provider.calls
    finally:
        resumed.session.close()


async def test_partial_participant_remains_incomplete_even_with_a_passing_sibling(tmp_path):
    from harness.provider import StreamStop, TextDelta

    async with case(tmp_path, {"a": [[TextDelta("correct"), StreamStop("max_tokens")]], "b": "correct"}) as kernel:
        report = report_for(kernel)
        assert report.result.status == "incomplete"
        assert report.members[0].result.status == "incomplete"
        assert [m.evidence[0].status for m in report.members] == ["unverified", "passed"]
        assert kernel.session.blobs.get(report.output) == b"correct"
