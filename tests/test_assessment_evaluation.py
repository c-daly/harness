"""Assessment candidates cannot supply grades, rewrite evidence, or activate themselves."""

import asyncio
import json

import pytest

from harness.assessment_evaluation import (
    CONTEXT_REQUIRED_CASES, PROGRESS_REQUIRED_CASES, AssessmentEvaluationSuite,
    AssessmentEvaluatorConfig, AssessmentExperiment, AssessmentGrader, ContextEvaluationCase,
    ProgressEvaluationCase, prepare_assessment_experiment,
)
from harness.fold import fold
from harness.improvement import verdict
from harness.improvement_journal import inspect_improvement, read_improvements
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.resume import resume_session
from harness.semantic_assessment import (
    CONTEXT_PROMPT, PROGRESS_PROMPT, AssessmentPrompt, ContextCandidate, ContextSelection,
    ContextSelectionInput, ProgressAssessment, eligible_context, validate_progress, validate_selection,
)
from harness.semantic_evaluation import evaluator_version, run_evaluation
from harness.semantics import read_semantics, render_semantics
from harness.types import ModelId
from tests.test_semantics import kernel_for


def experiment(function="context_selection", **config_changes):
    if function == "context_selection":
        cases = (*CONTEXT_REQUIRED_CASES, ContextEvaluationCase(id="held-context", partition="held_out",
            input=ContextSelectionInput(query="How are recollections kept between visits?", candidates=(
                ContextCandidate(id="storage", summary="Persistent memory uses SQLite", freshness="current"),)),
            expected=ContextSelection(selected_ids=("storage",), reason="selected")))
    else:
        held = PROGRESS_REQUIRED_CASES[0]
        cases = (*PROGRESS_REQUIRED_CASES, ProgressEvaluationCase(id="held-work", partition="held_out",
            input=held.input.model_copy(update={"execution": "not started"}),
            expected=ProgressAssessment(remaining_ids=("output", "review"), focus_ids=("output",), next_action="work")))
    return AssessmentExperiment(candidate=AssessmentPrompt(function=function, instructions="candidate"),
        hypothesis="Operator hypothesis sentinel", expected_benefit="Operator benefit sentinel",
        configuration=AssessmentEvaluatorConfig(model=ModelId("fake"), runtime_version="scripted", **config_changes),
        suite=AssessmentEvaluationSuite(function=function, cases=cases), max_latency_ratio=100,
        max_case_latency_ms=5000)


class AssessmentProvider(FakeProvider):
    def __init__(self, spec):
        super().__init__([])
        self.spec, self.requests = spec, []
        self.seed = True
        self.bad_candidate = False
        self.hang = False
        self.entered = asyncio.Event()

    async def infer(self, request):
        self.requests.append(request)
        if self.hang:
            self.entered.set()
            await asyncio.Event().wait()
        candidate = request.messages[0].text() == "candidate"
        if self.seed or candidate and self.bad_candidate:
            output = {"score": 1, "accepted": True}
        else:
            case = requested_case(self.spec, request)
            output = case.expected.model_dump(mode="json")
            if case.partition == "held_out" and not candidate:
                if case.function == "context_selection":
                    output.update(selected_ids=[], reason="uncertain")
                else:
                    output.update(focus_ids=[], next_action="uncertain")
        for chunk in text_turn(json.dumps(output)):
            yield chunk


def requested_case(spec, request):
    data = json.loads(request.messages[-1].text())
    return next(c for c in spec.suite.cases if (
        c.input.query == data.get("query") if isinstance(c.input, ContextSelectionInput)
        else c.input.model_dump(mode="json") == data))


async def seed(kernel, provider):
    kernel.tasks.create("Actual task, separate from evaluation fixtures")
    if provider.spec.suite.function == "context_selection":
        observation = await kernel.semantics.select_context(provider.spec.suite.cases[0].input, model=ModelId("fake"))
    else:
        observation = await kernel.semantics.assess_progress(model=ModelId("fake"))
    assert observation.reason == "invalid_output" and observation.evaluation_run_id is None
    provider.seed = False
    provider.requests.clear()


@pytest.mark.parametrize("function", ["context_selection", "progress_assessment"])
async def test_paired_assessments_grade_fixed_oracles_and_preserve_live_task(tmp_path, function):
    spec = experiment(function)
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider)
    try:
        await seed(kernel, provider)
        before = kernel.tasks.state()
        result = await kernel.improvement_service.compare_assessment(spec)
        state = read_improvements(tmp_path, kernel.session.id)
        plan = state.plans[result.plan_id]
        assert result.completion == "completed" and verdict(plan, result) == "passed"
        assert kernel.tasks.state() == before and not kernel.tasks.selected().accepted
        assert not state.prompt_changes
        assert "candidate" in inspect_improvement(state, kernel.session.blobs, result.id)
        with pytest.raises(ValueError):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        assert not read_improvements(tmp_path, kernel.session.id).prompt_changes
        report = json.loads(kernel.session.blobs.get(result.artifact))
        count = len(spec.suite.cases)
        assert report["metrics"]["candidate"]["correct"] == count
        assert report["metrics"]["incumbent"]["correct"] == count - 1
        assert report["metrics"]["rules"]["samples"] == count
        assert report["metrics"]["candidate"]["held_out"]["samples"] == 1
        assert not report["activation_qualified"] and report["held_out_provenance"] == "operator_declared"
        called = iter(provider.requests)
        expected_calls = 0
        for index, case in enumerate(spec.suite.cases):
            data = eligible_context(case.input) if function == "context_selection" else case.input
            if function == "context_selection" and not data.candidates:
                continue
            expected_calls += 2
            left, right = next(called), next(called)
            assert (left.messages[0].text() == "candidate") == bool(index % 2)
            for request in (left, right):
                assert request.messages[-1].text() == data.model_dump_json()
                assert len(request.messages) == 2 and not request.tools
                assert "sentinel" not in request.messages[0].text()
        assert len(provider.requests) == expected_calls
        observations = read_semantics(tmp_path, kernel.session.id)[1:]
        assert all(o.evaluation_run_id == result.run_id for o in observations)
        if function == "context_selection":
            automatic = [o for o in observations if o.decision_source == "eligibility"]
            assert len(automatic) == 2 and all(o.reason == "no_match" and o.call_id is None for o in automatic)
        assert "Evaluation fixture" in render_semantics(observations)
        assert "Recorded evidence as of event" not in render_semantics(observations)
        assert not fold(read_session(tmp_path, kernel.session.id)).messages
        # Reusing live evidence is valid; experiment observations must not seed new claims.
        await kernel.improvement_service.compare_assessment(spec)
        state = read_improvements(tmp_path, kernel.session.id)
        assert len(state.evidence) == 1 and len(state.candidates) == 2
    finally:
        kernel.session.close()


@pytest.mark.parametrize("function", ["context_selection", "progress_assessment"])
async def test_invalid_candidate_cannot_self_grade_or_erase_critical_failure(tmp_path, function):
    spec = experiment(function)
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider)
    try:
        await seed(kernel, provider)
        provider.bad_candidate = True
        result = await kernel.improvement_service.compare_assessment(spec)
        plan = kernel.improvements.state.plans[result.plan_id]
        assert verdict(plan, result) == "failed"
        for row in result.observations:
            # The unavailable-context case is answered by core in both arms;
            # every actual candidate inference still fails the validator.
            assert row.candidate_passed is (function == "context_selection" and row.case_id == "critical-unavailable")
        assert verdict(plan, result.model_copy(update={"completion": "cancelled"})) == "failed"
        assert not any(e.event.type == "tool_call_proposed" for e in read_session(tmp_path, kernel.session.id))
    finally:
        kernel.session.close()


@pytest.mark.parametrize("function", ["context_selection", "progress_assessment"])
@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
async def test_interrupted_assessment_retains_unknown_grades_and_replays_without_inference(tmp_path, function, interrupt):
    spec = experiment(function, **({"timeout_seconds": .02} if interrupt == "deadline" else {}))
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider)
    await seed(kernel, provider)
    provider.hang = True
    task = asyncio.create_task(kernel.improvement_service.compare_assessment(spec))
    await provider.entered.wait()
    if interrupt == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    result = next(iter(kernel.improvements.state.results.values()))
    assert result.completion == ("cancelled" if interrupt == "cancel" else "timed_out")
    assert verdict(kernel.improvements.state.plans[result.plan_id], result) == "inconclusive"
    assert all(row.candidate_passed is None and row.incumbent_passed is None for row in result.observations)
    session_id = kernel.session.id
    kernel.session.close()
    resumed, _ = resume_session(tmp_path, session_id)
    try:
        state = fold(read_session(tmp_path, session_id))
        assert not state.open_evaluations and not state.open_model_intents
        assert len(provider.requests) == 1
        assert read_improvements(tmp_path, session_id).results[result.id] == result
    finally:
        resumed.close()


@pytest.mark.parametrize("function", ["context_selection", "progress_assessment"])
@pytest.mark.parametrize("change", ["critical", "partition", "duplicate", "wrong-function", "invalid-oracle"])
def test_assessment_suite_refuses_changed_or_impossible_oracles(function, change):
    data = experiment(function).suite.model_dump(mode="json")
    if change == "critical":
        data["cases"][0]["critical"] = False
    elif change == "partition":
        data["cases"][-1]["partition"] = "regression"
    elif change == "duplicate":
        data["cases"].append(data["cases"][-1])
    elif change == "wrong-function":
        data["function"] = "context_selection" if function == "progress_assessment" else "progress_assessment"
    else:
        key = "selected_ids" if function == "context_selection" else "remaining_ids"
        data["cases"][-1]["expected"][key] = ["invented"]
    with pytest.raises(ValueError):
        AssessmentEvaluationSuite.model_validate(data)


@pytest.mark.parametrize("function", ["context_selection", "progress_assessment"])
async def test_wrong_function_or_changed_oracle_refused_before_inference(tmp_path, function):
    spec = experiment(function)
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider)
    try:
        await seed(kernel, provider)
        incumbent, plan = prepare_assessment_experiment(kernel, spec)
        from harness.improvement import Candidate
        other = PROGRESS_PROMPT if function == "context_selection" else CONTEXT_PROMPT
        old = kernel.improvements.state.candidates[plan.candidate_id]
        wrong = Candidate.model_validate({**old.model_dump(), "id": "wrong",
            "artifact": kernel.session.blobs.put(other.model_dump_json().encode())})
        kernel.improvements.record(wrong)
        changed = plan.model_copy(update={"id": "wrong-plan", "candidate_id": wrong.id,
                                         "candidate_version": wrong.artifact.sha256})
        kernel.improvements.record(changed)
        with pytest.raises(ValueError, match="different function"):
            await run_evaluation(kernel, changed.id, incumbent=incumbent, config=spec.configuration)
        altered = spec.suite.model_copy(update={"cases": (*spec.suite.cases[:-1],
            spec.suite.cases[-1].model_copy(update={"id": "different"}))})
        changed = plan.model_copy(update={"id": "changed-plan",
            "suite": kernel.session.blobs.put(altered.model_dump_json().encode())})
        kernel.improvements.record(changed)
        with pytest.raises(ValueError, match="identities"):
            await run_evaluation(kernel, changed.id, incumbent=incumbent, config=spec.configuration)
        assert not provider.requests and not fold(read_session(tmp_path, kernel.session.id)).open_evaluations
    finally:
        kernel.session.close()


@pytest.mark.parametrize("function", ["context_selection", "progress_assessment"])
def test_baselines_obey_evidence_and_grading_is_order_independent(function):
    spec = experiment(function)
    grader = AssessmentGrader(spec.suite.model_dump())
    for case in spec.suite.cases:
        result = grader.rules(case)
        if function == "context_selection":
            validate_selection(case.input, ContextSelection.model_validate(result))
        else:
            validate_progress(case.input, ProgressAssessment.model_validate(result))
        expected = case.expected.model_dump(mode="json")
        for key, value in expected.items():
            if isinstance(value, list):
                expected[key] = list(reversed(value))
        assert grader.matches(case, expected)


async def test_comparison_requires_live_evidence_idle_session_and_input_bounds(tmp_path):
    spec = experiment()
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider)
    try:
        with pytest.raises(ValueError, match="live assessment"):
            await kernel.improvement_service.compare_assessment(spec)
        await seed(kernel, provider)
        kernel.controller.submit("new work")
        with pytest.raises(ValueError, match="idle session"):
            await kernel.improvement_service.compare_assessment(spec)
        kernel.controller.clear()
        bounded = experiment(limits={"max_message_bytes": 1})
        with pytest.raises(ValueError, match="inputs exceed"):
            await kernel.improvement_service.compare_assessment(bounded)
        assert not provider.requests and not kernel.improvements.state.candidates
    finally:
        kernel.session.close()


def test_assessment_evaluator_source_is_part_of_version(tmp_path, monkeypatch):
    from pathlib import Path
    spec = experiment()
    provider = AssessmentProvider(spec)
    before = evaluator_version(provider, spec.configuration)
    read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read(path) + b"\n# new grader"
                       if path.name == "assessment_evaluation.py" else read(path))
    assert evaluator_version(provider, spec.configuration) != before


@pytest.mark.parametrize("reason", ["budget", "denied", "provider_error"])
async def test_unavailable_inference_cannot_count_as_correct_uncertainty(tmp_path, reason):
    from harness.cli import build_kernel
    from harness.errors import AuthFailed
    from harness.execution import ExecutionLimits
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    spec = experiment()
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider,
        **({"execution_limits": ExecutionLimits(max_model_calls=1)} if reason == "budget" else {}))
    try:
        await seed(kernel, provider)
        if reason == "denied":
            session_id = kernel.session.id
            kernel.session.close()
            kernel = build_kernel(base_dir=tmp_path, model=ModelId("fake"), provider=provider,
                resume_session_id=session_id, permissions=PermissionEngine([
                    RuleSet(rules=[PermissionRule("deny", "model:*")], default="deny")]))
        elif reason == "provider_error":
            async def unavailable(request):
                raise AuthFailed("private-provider-sentinel")
                yield
            provider.infer = unavailable
        result = await kernel.improvement_service.compare_assessment(spec)
        assert verdict(kernel.improvements.state.plans[result.plan_id], result) == "inconclusive"
        for row in result.observations:
            expected = True if row.case_id == "critical-unavailable" else None
            assert row.incumbent_passed is expected and row.candidate_passed is expected
        artifact = kernel.session.blobs.get(result.artifact)
        assert b"private-provider-sentinel" not in artifact
        report = json.loads(artifact)
        assert report["metrics"]["candidate"]["measured"] == 1
        assert report["metrics"]["candidate"]["abstentions"] == len(spec.suite.cases) - 1
        assert not provider.requests
    finally:
        kernel.session.close()


async def test_evaluation_observations_cannot_seed_proposals_as_live_failures(tmp_path):
    from harness.events import AssessmentObserved
    from harness.semantics import ASSESSMENT_LIMITS, AssessmentObservation
    spec = experiment()
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider)
    try:
        kernel.session.append(AssessmentObserved(observation=AssessmentObservation(
            id="synthetic-observation", evaluation_run_id="historical-run", function="context_selection",
            function_version="a" * 64, model=ModelId("fake"), limits=ASSESSMENT_LIMITS, reason="invalid_output",
            prompt=kernel.session.blobs.put(CONTEXT_PROMPT.model_dump_json().encode()), duration_ms=1)))
        with pytest.raises(ValueError, match="live assessment"):
            await kernel.improvement_service.compare_assessment(spec)
        assert not provider.requests and not kernel.improvements.state.evidence
    finally:
        kernel.session.close()


async def test_provider_change_during_assessment_fails_the_pinned_run(tmp_path):
    spec = experiment()
    provider = AssessmentProvider(spec)
    kernel = await kernel_for(tmp_path, provider)
    try:
        await seed(kernel, provider)
        original = provider.infer

        async def changed(request):
            async for chunk in original(request):
                yield chunk
            kernel.set_provider(FakeProvider([]))
        provider.infer = changed
        with pytest.raises(ValueError, match="changed during"):
            await kernel.improvement_service.compare_assessment(spec)
        result = next(iter(kernel.improvements.state.results.values()))
        assert result.completion == "failed"
        assert verdict(kernel.improvements.state.plans[result.plan_id], result) != "passed"
        assert not fold(read_session(tmp_path, kernel.session.id)).open_evaluations
    finally:
        kernel.session.close()


def test_shipped_comparison_examples_are_exact_public_smoke_inputs():
    from pathlib import Path
    from scripts.qualify_assessment_evaluation import public_experiments
    root = Path(__file__).resolve().parents[1]
    for spec in public_experiments():
        path = root / "docs/examples" / (spec.suite.function.replace("_", "-") + "-comparison.json")
        assert AssessmentExperiment.model_validate_json(path.read_bytes()) == spec
