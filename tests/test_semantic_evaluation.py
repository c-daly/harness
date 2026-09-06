"""The evaluator owns the oracle, fixed versions, and incomplete-run handling."""

import asyncio
import json

import pytest

from harness.events import EvaluationRunStarted
from harness.fold import fold
from harness.improvement import Candidate, Evidence, EvaluationPlan, adoption_decision, AdoptionPolicy, verdict
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.resume import resume_session
from harness.semantic_evaluation import (
    EvaluatorConfig, MessageEvaluationCase, MessageEvaluationSuite, REQUIRED_CASES,
    evaluator_version, run_evaluation,
)
from harness.semantics import MessagePrompt
from harness.types import ModelId
from tests.test_semantics import kernel_for


class PairedProvider(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.requests = []

    async def infer(self, request):
        self.requests.append(request)
        text = request.messages[-1].text()
        if text == REQUIRED_CASES[0].text:
            kind = "stop_request"
        elif text == REQUIRED_CASES[1].text:
            kind = "uncertain"
        else:
            kind = "question" if request.messages[0].text() == "candidate" else "uncertain"
        for chunk in text_turn(json.dumps({"kind": kind})):
            yield chunk


def prepare(kernel, provider, **config_changes):
    blobs, journal = kernel.session.blobs, kernel.improvements
    incumbent = blobs.put(MessagePrompt(instructions="incumbent").model_dump_json().encode())
    candidate_blob = blobs.put(MessagePrompt(instructions="candidate").model_dump_json().encode())
    evidence = Evidence(id="e", source_session=kernel.session.id, source_seq=1,
                        observation="Synthetic regression fixture", category="failure")
    candidate = Candidate(id="c", target="prompt", incumbent_version=incumbent.sha256,
                          artifact=candidate_blob, evidence_ids=("e",), hypothesis="Handle questions",
                          expected_benefit="Correct the held-out question")
    suite = MessageEvaluationSuite(cases=(*REQUIRED_CASES, MessageEvaluationCase(
        id="question", partition="held_out", text="How is this going?", expected="question")))
    config = EvaluatorConfig(model=ModelId("fake"), runtime_version="scripted-test", **config_changes)
    plan = EvaluationPlan(id="p", candidate_id="c", incumbent_version=incumbent.sha256,
        candidate_version=candidate_blob.sha256, evaluator_version=evaluator_version(provider, config),
        suite=blobs.put(suite.model_dump_json().encode()), cases=suite.plan_cases(),
        max_case_latency_ms=5000, max_latency_ratio=100)
    for record in (evidence, candidate, plan):
        journal.record(record)
    return incumbent, candidate, plan, config


async def test_paired_evaluation_records_real_scores_baseline_and_never_sends_labels(tmp_path):
    provider = PairedProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        incumbent, candidate, plan, config = prepare(kernel, provider)
        result = await run_evaluation(kernel, plan.id, incumbent=incumbent, config=config)
        assert verdict(plan, result) == "passed"
        assert adoption_decision(candidate, plan, result, AdoptionPolicy(version="a"*64)) == "review_required"
        assert len(provider.requests) == 6
        assert all(len(r.messages) == 2 and not r.tools for r in provider.requests)
        assert {r.messages[-1].text() for r in provider.requests} == {
            REQUIRED_CASES[0].text, REQUIRED_CASES[1].text, "How is this going?"}
        report = json.loads(kernel.session.blobs.get(result.artifact))
        assert report["metrics"]["candidate"]["correct"] == 3
        assert report["metrics"]["incumbent"]["correct"] == 2
        assert report["metrics"]["candidate"]["abstentions"] == 1
        assert report["metrics"]["rules"]["samples"] == 3
        assert report["metrics"]["candidate"]["held_out"]["samples"] == 1
        events = read_session(tmp_path, kernel.session.id)
        assert not fold(events).open_evaluations and not fold(events).messages
        assert result.run_id and result.completion == "completed"
        start = next(i for i, e in enumerate(events) if e.event.type == "evaluation_run_started")
        first_call = next(i for i, e in enumerate(events) if e.event.type == "model_call_proposed")
        assert start < first_call
    finally:
        kernel.session.close()


@pytest.mark.parametrize("change", ["incumbent", "runtime", "limits", "suite", "target", "provider"])
async def test_version_or_oracle_mismatch_rejected_before_any_inference(tmp_path, change):
    provider = PairedProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        incumbent, _, plan, config = prepare(kernel, provider)
        if change == "incumbent":
            incumbent = kernel.session.blobs.put(MessagePrompt(instructions="other").model_dump_json().encode())
        elif change == "runtime":
            config = config.model_copy(update={"runtime_version": "changed"})
        elif change == "limits":
            config = config.model_copy(update={"limits": config.limits.model_copy(update={"max_output_tokens": 32})})
        elif change == "provider":
            kernel.set_provider(FakeProvider([]))
        else:
            # The persisted journal is authoritative; mutate a new plan/candidate, not its cache.
            candidate = kernel.improvements.state.candidates["c"]
            if change == "target":
                candidate = candidate.model_copy(update={"id": "c2", "target": "code"})
                kernel.improvements.record(candidate)
                plan = plan.model_copy(update={"id": "p2", "candidate_id": candidate.id})
            else:
                suite = MessageEvaluationSuite(cases=(*REQUIRED_CASES, MessageEvaluationCase(
                    id="different", partition="held_out", text="Question?", expected="question")))
                plan = plan.model_copy(update={"id": "p2", "suite": kernel.session.blobs.put(
                    suite.model_dump_json().encode())})
            kernel.improvements.record(plan)
        with pytest.raises(ValueError):
            await run_evaluation(kernel, plan.id, incumbent=incumbent, config=config)
        assert not provider.requests
        assert not fold(read_session(tmp_path, kernel.session.id)).open_evaluations
    finally:
        kernel.session.close()


async def test_cancelled_evaluation_preserves_unknown_measurements_and_can_resume_without_rerun(tmp_path):
    entered = asyncio.Event()

    class Hanging(PairedProvider):
        async def infer(self, request):
            entered.set()
            await asyncio.Event().wait()
            yield

    provider = Hanging()
    kernel = await kernel_for(tmp_path, provider)
    incumbent, _, plan, config = prepare(kernel, provider)
    task = asyncio.create_task(run_evaluation(kernel, plan.id, incumbent=incumbent, config=config))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = next(iter(kernel.improvements.state.results.values()))
    assert result.completion == "cancelled" and verdict(plan, result) == "inconclusive"
    assert len(result.observations) == 3
    assert all(m.candidate_passed is None for m in result.observations)
    session_id = kernel.session.id
    kernel.session.close()
    resumed, messages = resume_session(tmp_path, session_id)
    try:
        assert not messages
        assert not fold(read_session(tmp_path, session_id)).open_evaluations
    finally:
        resumed.close()


async def test_crash_repair_aborts_an_open_evaluation_once(tmp_path):
    provider = PairedProvider()
    kernel = await kernel_for(tmp_path, provider)
    incumbent, _, plan, config = prepare(kernel, provider)
    kernel.session.append(EvaluationRunStarted(run_id="interrupted", plan_id=plan.id,
        incumbent=incumbent, configuration=kernel.session.blobs.put(config.model_dump_json().encode())))
    session_id = kernel.session.id
    kernel.session.close()
    for _ in range(2):
        resumed, _ = resume_session(tmp_path, session_id)
        resumed.close()
    terminals = [e.event for e in read_session(tmp_path, session_id)
                 if e.event.type == "evaluation_run_finished"]
    assert len(terminals) == 1 and terminals[0].status == "aborted"
    assert not provider.requests


async def test_durable_result_survives_crash_before_run_terminal(tmp_path, monkeypatch):
    from harness.events import EvaluationRunFinished
    provider = PairedProvider()
    kernel = await kernel_for(tmp_path, provider)
    incumbent, _, plan, config = prepare(kernel, provider)
    append = kernel.session.append

    def fail_terminal(event):
        if isinstance(event, EvaluationRunFinished):
            raise OSError("terminal write failed")
        return append(event)

    monkeypatch.setattr(kernel.session, "append", fail_terminal)
    with pytest.raises(OSError):
        await run_evaluation(kernel, plan.id, incumbent=incumbent, config=config)
    result = next(iter(kernel.improvements.state.results.values()))
    assert verdict(plan, result) == "passed"
    session_id = kernel.session.id
    kernel.session.close()
    resumed, _ = resume_session(tmp_path, session_id)
    resumed.close()
    terminal = [e.event for e in read_session(tmp_path, session_id)
                if isinstance(e.event, EvaluationRunFinished)][-1]
    assert terminal.status == "completed" and terminal.result_id == result.id
    assert len(provider.requests) == 6


async def test_whole_experiment_deadline_keeps_missing_cases_inconclusive(tmp_path):
    class Slow(PairedProvider):
        async def infer(self, request):
            await asyncio.Event().wait()
            yield

    provider = Slow()
    kernel = await kernel_for(tmp_path, provider)
    try:
        incumbent, _, plan, config = prepare(kernel, provider, timeout_seconds=0.02)
        result = await run_evaluation(kernel, plan.id, incumbent=incumbent, config=config)
        assert result.completion == "timed_out" and verdict(plan, result) == "inconclusive"
        assert not fold(read_session(tmp_path, kernel.session.id)).open_model_intents
        assert not fold(read_session(tmp_path, kernel.session.id)).open_evaluations
    finally:
        kernel.session.close()


async def test_candidate_cannot_grade_itself_or_erase_critical_failure(tmp_path):
    class SelfGrading(PairedProvider):
        async def infer(self, request):
            if request.messages[0].text() == "candidate":
                for chunk in text_turn('{"kind":"completed","score":1,"accepted":true}'):
                    yield chunk
            else:
                async for chunk in super().infer(request):
                    yield chunk

    provider = SelfGrading()
    kernel = await kernel_for(tmp_path, provider)
    try:
        incumbent, candidate, plan, config = prepare(kernel, provider)
        result = await run_evaluation(kernel, plan.id, incumbent=incumbent, config=config)
        assert verdict(plan, result) == "failed"
        assert verdict(plan, result.model_copy(update={"completion": "cancelled"})) == "failed"
        assert adoption_decision(candidate, plan, result,
            AdoptionPolicy(version="a"*64, automatic_targets={"prompt"})) == "refused"
    finally:
        kernel.session.close()


def test_fixed_critical_cases_cannot_be_relabelled():
    with pytest.raises(ValueError, match="critical"):
        MessageEvaluationSuite(cases=(REQUIRED_CASES[0].model_copy(update={"expected": "acknowledgement"}),
            REQUIRED_CASES[1], MessageEvaluationCase(id="held", partition="held_out", text="?", expected="question")))
