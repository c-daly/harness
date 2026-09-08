"""Supervised assessment prompts remain function-scoped, advisory, and replayable."""

import asyncio
import json

import pytest

from harness.assessment_evaluation import AssessmentPromptExperiment
from harness.cli import build_kernel
from harness.events import AssessmentObserved
from harness.fold import fold
from harness.improvement import PromptChange, verdict
from harness.improvement_journal import read_improvements, render_improvements, inspect_improvement
from harness.log import read_session
from harness.prompt_improvement import default_prompt, prompt_selection
from harness.semantic_assessment import ProgressInput
from harness.semantics import read_semantics, render_semantics
from harness.types import ModelId
from harness.provider import text_turn
from tests.test_assessment_evaluation import AssessmentProvider, experiment
from tests.test_semantics import kernel_for

MODEL = ModelId("fake")
FUNCTIONS = ("context_selection", "progress_assessment")


class LearningAssessmentProvider(AssessmentProvider):
    def __init__(self, function):
        super().__init__(experiment(function))
        self.proposal = self.spec.candidate
        self.proposal_mode = "normal"
        self.hang_proposal = False

    async def infer(self, request):
        if request.purpose == "improvement:propose":
            self.requests.append(request)
            if self.hang_proposal:
                self.entered.set()
                await asyncio.Event().wait()
            prompt = self.proposal.model_dump()
            if self.proposal_mode == "wrong-function":
                prompt["function"] = next(f for f in FUNCTIONS if f != prompt["function"])
            elif self.proposal_mode == "unchanged":
                prompt = json.loads(request.messages[-1].text())["incumbent"]
            output = {"prompt": prompt, "hypothesis": "A concrete response shape might reduce invalid outputs",
                      "expected_benefit": "Fewer invalid responses on frozen cases"}
            if self.proposal_mode == "grant":
                output["adopt"] = True
            for chunk in text_turn(json.dumps(output)):
                yield chunk
            return
        # Live progress snapshots differ from evaluation fixtures. Supply an ordinary
        # valid assessment; the real evidence validator still decides whether it fits.
        data = json.loads(request.messages[-1].text())
        if (not self.seed and self.spec.suite.function == "progress_assessment"
                and data.get("session_id") != "synthetic-evaluation-fixture"):
            from harness.assessment_evaluation import rule_progress
            self.requests.append(request)
            for chunk in text_turn(rule_progress(ProgressInput.model_validate(data)).model_dump_json()):
                yield chunk
            return
        async for chunk in super().infer(request):
            yield chunk


def evaluation(provider):
    return AssessmentPromptExperiment.model_validate(provider.spec.model_dump(
        exclude={"candidate", "hypothesis", "expected_benefit"}))


async def observe(kernel, function, provider):
    if function == "context_selection":
        return await kernel.semantics.select_context(provider.spec.suite.cases[-1].input, model=MODEL)
    return await kernel.semantics.assess_progress(model=MODEL)


async def seed_failures(kernel, function, provider):
    if kernel.tasks.selected() is None:
        kernel.tasks.create("Private project title stays out of proposals")
        kernel.tasks.add_requirement({"id": "review", "description": "Operator inspects the result"})
    provider.seed = True
    for _ in range(2):
        value = await observe(kernel, function, provider)
        assert value.reason == "invalid_output" and value.evaluation_run_id is None
    provider.seed = False


async def passed(kernel, function, provider):
    await seed_failures(kernel, function, provider)
    candidate = await kernel.improvement_service.propose(model=MODEL, function=function)
    result = await kernel.improvement_service.evaluate(candidate.id, experiment=evaluation(provider))
    assert verdict(kernel.improvements.state.plans[result.plan_id], result) == "passed"
    return candidate, result


@pytest.mark.parametrize("function", FUNCTIONS)
async def test_proposal_evaluation_adoption_live_use_restart_and_exact_rollback(tmp_path, function):
    provider = LearningAssessmentProvider(function)
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passed(kernel, function, provider)
        generation = next(r for r in provider.requests if r.purpose == "improvement:propose")
        assert not generation.tools and "Private project title" not in str(generation.messages)
        assert provider.spec.suite.cases[-1].input.model_dump_json() not in str(generation.messages)
        assert "expected" not in json.loads(generation.messages[-1].text())
        assert len(candidate.evidence_ids) == 2
        state = read_improvements(tmp_path, kernel.session.id)
        events = read_session(tmp_path, kernel.session.id)
        assert all(isinstance(next(e.event for e in events if e.seq == state.evidence[key].source_seq), AssessmentObserved)
                   for key in candidate.evidence_ids)
        baseline = default_prompt(kernel.session, function=function)
        assert prompt_selection(kernel.session, provider, MODEL, function=function)[0] == baseline
        task_before, calls = kernel.tasks.state(), len(provider.requests)
        with pytest.raises(ValueError):
            kernel.improvement_service.adopt(result.id, model=MODEL)  # Message remains the legacy default.
        change = kernel.improvement_service.adopt(result.id, model=MODEL, function=function)
        assert change.policy == "supervised-assessment-prompt-v1" and change.function == function
        assert len(provider.requests) == calls
        assert function in render_improvements(read_improvements(tmp_path, kernel.session.id))
        assert "candidate" in inspect_improvement(read_improvements(tmp_path, kernel.session.id), kernel.session.blobs, change.id)
        live = await observe(kernel, function, provider)
        assert live.prompt == candidate.artifact and live.selection_id == change.id
        assert live.selection_status == "adopt" and live.evaluation_run_id is None
        assert live.limits == evaluation(provider).configuration.limits
        assert kernel.tasks.state() == task_before and not kernel.tasks.selected().accepted
        assert "Prompt selection: adopt" in render_semantics(read_semantics(tmp_path, kernel.session.id))
        for other in ("message_kind", *[f for f in FUNCTIONS if f != function]):
            assert prompt_selection(kernel.session, provider, MODEL, function=other)[1] == "builtin"
        session_id = kernel.session.id
    finally:
        kernel.session.close()
    resumed = build_kernel(base_dir=tmp_path, provider=LearningAssessmentProvider(function), model=MODEL,
                           resume_session_id=session_id)
    try:
        assert prompt_selection(resumed.session, resumed.provider, MODEL, function=function)[0] == candidate.artifact
        assert not resumed.provider.requests
        rollback = resumed.improvement_service.rollback(model=MODEL, function=function)
        assert rollback.prompt == baseline and not resumed.provider.requests
        assert prompt_selection(resumed.session, resumed.provider, MODEL, function=function)[0] == baseline
        assert resumed.tasks.state() == task_before
        state = fold(read_session(tmp_path, session_id))
        assert not state.messages and not state.open_model_intents and not state.open_evaluations
    finally:
        resumed.session.close()


@pytest.mark.parametrize("mode", ["wrong-function", "unchanged", "grant", "cancel", "no-live-failures"])
async def test_unproven_or_wrong_function_proposals_cannot_become_candidates(tmp_path, mode):
    function = "context_selection"
    provider = LearningAssessmentProvider(function)
    kernel = await kernel_for(tmp_path, provider)
    try:
        if mode != "no-live-failures":
            await seed_failures(kernel, function, provider)
        else:
            # Same current prompt and invalid output, but fixture provenance cannot seed a proposal.
            await seed_failures(kernel, function, provider)
            events = [e for e in read_session(tmp_path, kernel.session.id) if isinstance(e.event, AssessmentObserved)]
            other = default_prompt(kernel.session, function="progress_assessment")
            for e in events:
                kernel.session.append(AssessmentObserved(observation=e.event.observation.model_copy(update={
                    "id": e.event.observation.id + "-fixture", "prompt": other,
                    "function": "progress_assessment", "evaluation_run_id": "frozen-fixture"})))
            function = "progress_assessment"
        provider.proposal_mode = mode
        if mode == "cancel":
            provider.hang_proposal = True
            work = asyncio.create_task(kernel.improvement_service.propose(model=MODEL, function=function))
            await provider.entered.wait()
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        else:
            from harness.errors import MalformedStreamError
            with pytest.raises(MalformedStreamError if mode == "grant" else ValueError):
                await kernel.improvement_service.propose(model=MODEL, function=function)
        assert not read_improvements(tmp_path, kernel.session.id).candidates
        assert not fold(read_session(tmp_path, kernel.session.id)).open_model_intents
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["failed", "cancelled", "stale", "wrong-model", "wrong-function", "code-change", "busy"])
async def test_adoption_holds_without_current_matching_passing_evidence(tmp_path, mode, monkeypatch):
    function = "context_selection"
    provider = LearningAssessmentProvider(function)
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passed(kernel, function, provider)
        if mode in {"failed", "stale"}:
            provider.bad_candidate = True
            failed = await kernel.improvement_service.evaluate(candidate.id, experiment=evaluation(provider))
            assert verdict(kernel.improvements.state.plans[failed.plan_id], failed) == "failed"
            if mode == "failed":
                result = failed
        elif mode == "cancelled":
            provider.hang = True
            work = asyncio.create_task(kernel.improvement_service.evaluate(candidate.id, experiment=evaluation(provider)))
            await provider.entered.wait()
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            result = list(kernel.improvements.state.results.values())[-1]
        elif mode == "code-change":
            monkeypatch.setattr("harness.prompt_improvement.evaluator_version", lambda *args: "b" * 64)
        elif mode == "busy":
            kernel.controller.submit("Pending explicit request")
        with pytest.raises(ValueError):
            kernel.improvement_service.adopt(result.id, model=ModelId("other") if mode == "wrong-model" else MODEL,
                function="progress_assessment" if mode == "wrong-function" else function)
        assert not read_improvements(tmp_path, kernel.session.id).prompt_changes
    finally:
        kernel.session.close()


@pytest.mark.parametrize("function", FUNCTIONS)
async def test_changed_configuration_suspends_selected_assessment_without_inference(tmp_path, function, monkeypatch):
    provider = LearningAssessmentProvider(function)
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passed(kernel, function, provider)
        kernel.improvement_service.adopt(result.id, model=MODEL, function=function)
        calls = len(provider.requests)
        monkeypatch.setattr("harness.prompt_improvement.evaluator_version", lambda *args: "c" * 64)
        selected, status, _ = prompt_selection(kernel.session, provider, MODEL, function=function)
        assert status.startswith("suspended") and selected != candidate.artifact
        assert len(provider.requests) == calls
        with pytest.raises(ValueError, match="suspended"):
            await kernel.improvement_service.propose(model=MODEL, function=function)
        kernel.improvement_service.rollback(model=MODEL, function=function)
        assert prompt_selection(kernel.session, provider, MODEL, function=function)[1] == "rollback"
        assert len(provider.requests) == calls
    finally:
        kernel.session.close()


def test_legacy_message_changes_keep_their_function_and_policy():
    from harness.blobs import BlobRef
    ref = BlobRef(sha256="a" * 64, size=1)
    change = PromptChange(id="legacy", model=MODEL, action="rollback", previous=ref, prompt=ref)
    assert change.function == "message_kind" and change.policy == "supervised-message-prompt-v1"
    with pytest.raises(ValueError, match="policy"):
        PromptChange(**{**change.model_dump(), "function": "context_selection"})


async def test_context_and_progress_selections_and_rollback_are_independent(tmp_path):
    provider = LearningAssessmentProvider("context_selection")
    kernel = await kernel_for(tmp_path, provider)
    try:
        context, result = await passed(kernel, "context_selection", provider)
        kernel.improvement_service.adopt(result.id, model=MODEL, function="context_selection")
        provider.spec = experiment("progress_assessment")
        provider.proposal = provider.spec.candidate
        progress, result = await passed(kernel, "progress_assessment", provider)
        kernel.improvement_service.adopt(result.id, model=MODEL, function="progress_assessment")
        state = read_improvements(tmp_path, kernel.session.id)
        assert len(state.active_assessment_prompts) == 2 and not state.active_prompts
        assert prompt_selection(kernel.session, provider, MODEL, function="context_selection")[0] == context.artifact
        assert prompt_selection(kernel.session, provider, MODEL, function="progress_assessment")[0] == progress.artifact
        kernel.improvement_service.rollback(model=MODEL, function="context_selection")
        assert prompt_selection(kernel.session, provider, MODEL, function="progress_assessment")[0] == progress.artifact
        assert prompt_selection(kernel.session, provider, ModelId("other"), function="progress_assessment")[1] == "builtin"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("function", FUNCTIONS)
async def test_new_comparison_binds_selected_incumbent_and_evaluation_bypasses_selection(tmp_path, function):
    provider = LearningAssessmentProvider(function)
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passed(kernel, function, provider)
        kernel.improvement_service.adopt(result.id, model=MODEL, function=function)
        await observe(kernel, function, provider)  # Live evidence for this selected incumbent.
        replacement = provider.spec.model_copy(update={"candidate": provider.proposal.model_copy(
            update={"instructions": "candidate v2"})})
        compared = await kernel.improvement_service.compare_assessment(replacement)
        assert compared.incumbent_version == candidate.artifact.sha256
        assert prompt_selection(kernel.session, provider, MODEL, function=function)[0] == candidate.artifact
        observations = [o for o in read_semantics(tmp_path, kernel.session.id)
                        if o.evaluation_run_id == compared.run_id]
        assert all(o.selection_status == "explicit" and o.selection_id is None for o in observations)
        assert {o.prompt.sha256 for o in observations} == {compared.incumbent_version, compared.candidate_version}
        # Fixture invalid outputs cannot become live repeated-failure evidence for the candidate.
        from harness.prompt_improvement import repeated_failures
        assert not repeated_failures(kernel.session, MODEL, candidate.artifact, function)
    finally:
        kernel.session.close()


@pytest.mark.parametrize("function", FUNCTIONS)
async def test_explicit_limit_mismatch_uses_builtin_without_changing_selection(tmp_path, function):
    from harness.semantics import ASSESSMENT_LIMITS
    provider = LearningAssessmentProvider(function)
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passed(kernel, function, provider)
        change = kernel.improvement_service.adopt(result.id, model=MODEL, function=function)
        limits = ASSESSMENT_LIMITS.model_copy(update={"max_output_tokens": 128})
        kwargs = {"model": MODEL, "limits": limits}
        observed = (await kernel.semantics.select_context(provider.spec.suite.cases[-1].input, **kwargs)
                    if function == "context_selection" else await kernel.semantics.assess_progress(**kwargs))
        assert observed.prompt != candidate.artifact and observed.selection_status.startswith("suspended")
        assert observed.selection_id == change.id and observed.limits == limits
        assert prompt_selection(kernel.session, provider, MODEL, function=function)[0] == candidate.artifact
    finally:
        kernel.session.close()


@pytest.mark.parametrize("function", FUNCTIONS)
async def test_shared_cli_controls_select_explicit_function(tmp_path, function):
    from harness.improvement_cli import perform
    provider = LearningAssessmentProvider(function)
    kernel = await kernel_for(tmp_path, provider)
    label = "context" if function == "context_selection" else "progress"
    try:
        await seed_failures(kernel, function, provider)
        text = await perform(kernel, ["propose", label])
        candidate = list(kernel.improvements.state.candidates.values())[-1]
        assert label + " prompt" in text
        path = tmp_path / "experiment.json"
        path.write_text(evaluation(provider).model_dump_json())
        text = await perform(kernel, ["evaluate", candidate.id, str(path)])
        result = list(kernel.improvements.state.results.values())[-1]
        assert "Explicit adoption is available" in text
        with pytest.raises(ValueError):
            await perform(kernel, ["adopt", result.id])
        assert f"Adopted {label} prompt" in await perform(kernel, ["adopt", result.id, label])
        assert f"Restored {label} prompt" in await perform(kernel, ["rollback", label])
        before = len(provider.requests)
        with pytest.raises(ValueError, match="function"):
            await perform(kernel, ["propose", "shell"])
        assert len(provider.requests) == before
    finally:
        kernel.session.close()
