"""The supervised prompt loop changes bounded data only after measured evidence."""

import asyncio
import json

import pytest

from harness.cli import build_kernel
from harness.events import EvaluationRunFinished, SemanticObserved
from harness.fold import fold
from harness.improvement import PromptChange, verdict
from harness.improvement_journal import ImprovementJournal, read_improvements
from harness.log import read_session
from harness.prompt_improvement import PromptExperiment, default_prompt, prompt_selection
from harness.provider import FakeProvider, text_turn
from harness.semantic_evaluation import EvaluatorConfig, MessageEvaluationCase, MessageEvaluationSuite, REQUIRED_CASES
from harness.semantics import MessagePrompt, SemanticLimits
from harness.types import ModelId
from tests.test_semantics import kernel_for

PROPOSAL = MessagePrompt(instructions="candidate: preserve stop and uncertainty; classify questions precisely")
HELD_OUT = "Which branch has the fix?"


class LearningProvider(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.requests = []
        self.bad = False
        self.hang = None
        self.entered = asyncio.Event()

    async def infer(self, request):
        self.requests.append(request)
        if self.hang and request.purpose.startswith(self.hang):
            self.entered.set()
            await asyncio.Event().wait()
        text = request.messages[-1].text()
        candidate = request.messages[0].text() == PROPOSAL.instructions
        if request.purpose == "improvement:propose":
            output = json.dumps({"prompt": PROPOSAL.model_dump(), "hypothesis": "Clarify the required labels",
                                 "expected_benefit": "Fewer invalid responses"})
        elif text.startswith("broken"):
            output = "not JSON"
        else:
            if text == REQUIRED_CASES[0].text:
                kind = "acknowledgement" if self.bad and candidate else "stop_request"
            elif text == REQUIRED_CASES[1].text:
                kind = "uncertain"
            else:
                kind = "question" if candidate else "uncertain"
            output = json.dumps({"kind": kind})
        for chunk in text_turn(output):
            yield chunk


def experiment():
    return PromptExperiment(configuration=EvaluatorConfig(model=ModelId("fake"), runtime_version="fixture-v1",
        limits=SemanticLimits(timeout_seconds=2)), suite=MessageEvaluationSuite(cases=(*REQUIRED_CASES,
            MessageEvaluationCase(id="held-out-question", partition="held_out", text=HELD_OUT, expected="question"))),
        max_case_latency_ms=5000, max_latency_ratio=100)


async def seed_failures(kernel):
    for text in ("broken one", "broken two"):
        observed = await kernel.semantics.interpret(text, model=ModelId("fake"))
        assert observed.reason == "invalid_output"


async def passing_candidate(kernel):
    await seed_failures(kernel)
    candidate = await kernel.improvement_service.propose(model=ModelId("fake"))
    result = await kernel.improvement_service.evaluate(candidate.id, experiment=experiment())
    assert verdict(kernel.improvements.state.plans[result.plan_id], result) == "passed"
    return candidate, result


async def test_complete_supervised_loop_preserves_task_and_replay_without_inference(tmp_path):
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    task = kernel.tasks.create("Review the project")
    baseline = default_prompt(kernel.session)
    try:
        candidate, result = await passing_candidate(kernel)
        assert prompt_selection(kernel.session, provider, ModelId("fake"))[0] == baseline
        generation = next(r for r in provider.requests if r.purpose == "improvement:propose")
        assert not generation.tools and HELD_OUT not in str(generation.messages)
        assert "broken one" not in str(generation.messages)  # proposal receives metadata, not source inputs
        assert len(candidate.evidence_ids) == 2
        evidence = kernel.improvements.state.evidence
        events = read_session(tmp_path, kernel.session.id)
        for identity in candidate.evidence_ids:
            source = next(e for e in events if e.seq == evidence[identity].source_seq)
            assert isinstance(source.event, SemanticObserved)
        before = len(provider.requests)
        adopted = kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        assert len(provider.requests) == before
        interpreted = await kernel.semantics.interpret(HELD_OUT, model=ModelId("fake"))
        assert interpreted.prompt == candidate.artifact and interpreted.kind == "question"
        assert interpreted.selection_id == adopted.id and interpreted.selection_status == "adopt"
        assert interpreted.limits == experiment().configuration.limits
        assert kernel.tasks.selected().definition.id == task.id and not kernel.tasks.selected().accepted
        assert not fold(read_session(tmp_path, kernel.session.id)).messages
        session_id = kernel.session.id
    finally:
        kernel.session.close()
    resumed_provider = LearningProvider()
    resumed = build_kernel(base_dir=tmp_path, model=ModelId("fake"), provider=resumed_provider, resume_session_id=session_id)
    try:
        selected, _, _ = prompt_selection(resumed.session, resumed_provider, ModelId("fake"))
        assert selected == candidate.artifact and not resumed_provider.requests
        assert read_improvements(tmp_path, session_id).active_prompts[ModelId("fake")] == adopted.id
        rollback = resumed.improvement_service.rollback(model=ModelId("fake"))
        assert rollback.prompt == baseline and not resumed_provider.requests
        assert prompt_selection(resumed.session, resumed_provider, ModelId("fake"))[0] == baseline
        interpreted = await resumed.semantics.interpret(HELD_OUT, model=ModelId("fake"))
        assert interpreted.kind == "uncertain" and interpreted.prompt == baseline
        assert interpreted.selection_status == "rollback"
        assert resumed.tasks.selected().definition.id == task.id and not resumed.tasks.selected().accepted
    finally:
        resumed.session.close()


@pytest.mark.parametrize("mode", ["critical", "cancel", "timeout", "missing-finish", "repaired-finish", "manual", "stale-result"])
async def test_failed_incomplete_or_unproven_results_cannot_activate(tmp_path, mode):
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, passing = await passing_candidate(kernel)
        result = passing
        if mode in ("critical", "stale-result"):
            provider.bad = True
            result = await kernel.improvement_service.evaluate(candidate.id, experiment=experiment())
            assert verdict(kernel.improvements.state.plans[result.plan_id], result) == "failed"
            if mode == "stale-result":
                result = passing
        elif mode in ("cancel", "timeout"):
            provider.hang = "semantic:"
            spec = experiment()
            if mode == "timeout":
                spec = spec.model_copy(update={"configuration": spec.configuration.model_copy(update={"timeout_seconds": 0.01})})
            work = asyncio.create_task(kernel.improvement_service.evaluate(candidate.id, experiment=spec))
            await provider.entered.wait()
            if mode == "cancel":
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await work
                result = list(kernel.improvements.state.results.values())[-1]
            else:
                result = await work
        elif mode == "manual":
            result = passing.model_copy(update={"id": "manual", "run_id": None})
            kernel.improvements.record(result)
        else:
            # An injected disk failure leaves a genuine result without its terminal run event.
            original = kernel.session.append
            def broken(event):
                if isinstance(event, EvaluationRunFinished):
                    raise OSError("failed final run write")
                return original(event)
            kernel.session.append = broken
            with pytest.raises(OSError):
                await kernel.improvement_service.evaluate(candidate.id, experiment=experiment())
            kernel.session.append = original
            result = list(kernel.improvements.state.results.values())[-1]
            if mode == "repaired-finish":
                session_id = kernel.session.id
                kernel.session.close()
                kernel = build_kernel(base_dir=tmp_path, model=ModelId("fake"), provider=provider,
                                      resume_session_id=session_id)
                assert not fold(read_session(tmp_path, session_id)).open_evaluations
                # Recovery preserves the already committed completed result;
                # it repairs the terminal event without repeating inference.
                calls = len(provider.requests)
                change = kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
                assert change.result_id == result.id and len(provider.requests) == calls
                return
        with pytest.raises(ValueError):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        assert not read_improvements(tmp_path, kernel.session.id).prompt_changes
        assert prompt_selection(kernel.session, provider, ModelId("fake"))[1] == "builtin"
    finally:
        kernel.session.close()


async def test_changed_configuration_suspends_selection_and_rollback_needs_no_provider(tmp_path):
    provider = LearningProvider()
    provider.api_base = "http://127.0.0.1:8000/v1"
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passing_candidate(kernel)
        kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        provider.api_base = "http://127.0.0.1:8001/v1"
        selected, status, _ = prompt_selection(kernel.session, provider, ModelId("fake"))
        assert selected == default_prompt(kernel.session) and status.startswith("suspended")
        with pytest.raises(ValueError, match="configuration"):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        with pytest.raises(ValueError, match="suspended"):
            await kernel.improvement_service.propose(model=ModelId("fake"))
        before = len(provider.requests)
        kernel.improvement_service.rollback(model=ModelId("fake"))
        assert len(provider.requests) == before
    finally:
        kernel.session.close()


async def test_explicit_prompt_or_different_limits_do_not_use_adopted_configuration(tmp_path):
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passing_candidate(kernel)
        kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        observed = await kernel.semantics.interpret(HELD_OUT, model=ModelId("fake"), limits=SemanticLimits())
        assert observed.prompt != candidate.artifact and observed.selection_status.startswith("suspended")
        override = kernel.session.blobs.put(MessagePrompt(instructions="explicit override").model_dump_json().encode())
        observed = await kernel.semantics.interpret(HELD_OUT, model=ModelId("fake"), prompt=override)
        assert observed.prompt == override and observed.selection_status == "explicit"
    finally:
        kernel.session.close()


async def test_proposal_requires_distinct_current_failures_and_status_is_read_only(tmp_path):
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        before = set(kernel.session.blobs._root.iterdir())
        log = read_session(tmp_path, kernel.session.id)
        assert "need at least two" in kernel.improvement_service.status(ModelId("fake"))
        assert set(kernel.session.blobs._root.iterdir()) == before
        assert read_session(tmp_path, kernel.session.id) == log
        with pytest.raises(ValueError, match="two distinct"):
            await kernel.improvement_service.propose(model=ModelId("fake"))
        observed = await kernel.semantics.interpret("broken once", model=ModelId("fake"))
        kernel.session.append(SemanticObserved(observation=observed))
        with pytest.raises(ValueError, match="two distinct"):
            await kernel.improvement_service.propose(model=ModelId("fake"))
        assert not any(r.purpose == "improvement:propose" for r in provider.requests)
    finally:
        kernel.session.close()


async def test_generation_cancellation_does_not_leave_candidate_or_open_inference(tmp_path):
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        await seed_failures(kernel)
        provider.hang = "improvement:"
        work = asyncio.create_task(kernel.improvement_service.propose(model=ModelId("fake")))
        await provider.entered.wait()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        state = read_improvements(tmp_path, kernel.session.id)
        assert len(state.evidence) == 2 and not state.candidates and not state.prompt_changes
        assert not fold(read_session(tmp_path, kernel.session.id)).open_model_intents
    finally:
        kernel.session.close()


async def test_busy_session_and_corrupt_artifacts_do_not_change_selected_version(tmp_path):
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passing_candidate(kernel)
        kernel.controller.submit("queued user work")
        with pytest.raises(ValueError, match="idle"):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        kernel.controller.clear()
        path = kernel.session.blobs._root / candidate.artifact.sha256
        path.unlink()
        from harness.blobs import MissingBlobError
        with pytest.raises(MissingBlobError):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        assert not read_improvements(tmp_path, kernel.session.id).prompt_changes
    finally:
        kernel.session.close()


async def test_journal_rejects_stale_prompt_changes_and_invalid_rollback_bytes(tmp_path):
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passing_candidate(kernel)
        change = kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        with pytest.raises(ValueError, match="current selected"):
            ImprovementJournal(kernel.session).record(change.model_copy(update={"id": "stale"}))
        with pytest.raises(ValueError, match="exact preceding"):
            kernel.improvements.record(PromptChange(id="bad-rollback", model=ModelId("fake"), action="rollback",
                previous_id=change.id, previous=change.prompt, prompt=change.prompt))
        assert read_improvements(tmp_path, kernel.session.id).active_prompts[ModelId("fake")] == change.id
    finally:
        kernel.session.close()


@pytest.mark.parametrize("reason", ["budget", "denied"])
async def test_proposal_requires_dispatch_authority_and_shared_budget(tmp_path, reason):
    from harness.dispatcher import ModelDispatchBlocked
    from harness.execution import BudgetExceeded, ExecutionLimits
    from harness.permissions import PermissionEngine, PermissionRule, RuleSet
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider, execution_limits=ExecutionLimits(max_model_calls=2))
    try:
        await seed_failures(kernel)
        if reason == "denied":
            session_id = kernel.session.id
            kernel.session.close()
            kernel = build_kernel(base_dir=tmp_path, model=ModelId("fake"), provider=provider,
                resume_session_id=session_id, permissions=PermissionEngine([
                    RuleSet(rules=[PermissionRule("deny", "model:*")], default="deny")]))
        with pytest.raises((BudgetExceeded, ModelDispatchBlocked)):
            await kernel.improvement_service.propose(model=ModelId("fake"))
        assert len(provider.requests) == 2
        assert not read_improvements(tmp_path, kernel.session.id).candidates
        assert not fold(read_session(tmp_path, kernel.session.id)).open_model_intents
    finally:
        kernel.session.close()


async def test_zero_improvement_gate_cannot_qualify_supervised_adoption(tmp_path):
    from harness.semantic_evaluation import run_evaluation
    kernel = await kernel_for(tmp_path, LearningProvider())
    try:
        _, result = await passing_candidate(kernel)
        plan = kernel.improvements.state.plans[result.plan_id].model_copy(update={"id": "zero", "min_improved_cases": 0})
        kernel.improvements.record(plan)
        result = await run_evaluation(kernel, plan.id, incumbent=default_prompt(kernel.session),
                                      config=experiment().configuration)
        assert verdict(plan, result) == "passed"
        with pytest.raises(ValueError, match="latest passing"):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        assert not read_improvements(tmp_path, kernel.session.id).prompt_changes
    finally:
        kernel.session.close()


def test_cli_inspection_and_adoption_are_explicit_and_need_no_inference(tmp_path, monkeypatch, capsys):
    from harness.cli import main
    from harness.improvement_cli import main as improve
    provider = LearningProvider()

    async def seed():
        kernel = await kernel_for(tmp_path, provider)
        try:
            candidate, result = await passing_candidate(kernel)
            return kernel.session.id, candidate, result
        finally:
            kernel.session.close()
    session_id, candidate, result = asyncio.run(seed())
    before = read_session(tmp_path, session_id)
    calls = len(provider.requests)
    monkeypatch.setattr("sys.argv", ["harness", "improvements", str(session_id), "--base-dir", str(tmp_path),
                                     "--show", result.id])
    main()
    output = capsys.readouterr().out
    assert candidate.artifact.sha256 in output and PROPOSAL.instructions in output
    assert '"candidate_passed": true' in output and "Frozen evaluation plan" in output
    assert read_session(tmp_path, session_id) == before
    catalog = tmp_path / "models.toml"
    catalog.write_text('[models.fake]\nroute = "openai/fake"\n')
    monkeypatch.setattr("harness.provider_litellm.CatalogProvider", lambda _: provider)
    common = ["--base-dir", str(tmp_path), "--catalog", str(catalog), "--model", "fake", str(session_id)]
    improve([*common, "adopt", result.id])
    assert "Adopted message prompt" in capsys.readouterr().out
    state = read_improvements(tmp_path, session_id)
    assert state.prompt_changes[state.active_prompts[ModelId("fake")]].prompt == candidate.artifact
    improve([*common, "rollback"])
    assert "Restored message prompt" in capsys.readouterr().out
    assert len(provider.requests) == calls


async def test_selection_publication_failure_and_rollback_chain_preserve_exact_state(tmp_path):
    from harness.events import ImprovementRecorded
    provider = LearningProvider()
    kernel = await kernel_for(tmp_path, provider)
    try:
        candidate, result = await passing_candidate(kernel)
        original = kernel.session.append

        def failed(event):
            if isinstance(event, ImprovementRecorded) and isinstance(event.record, PromptChange):
                raise OSError("injected selection write failure")
            return original(event)
        kernel.session.append = failed
        with pytest.raises(OSError):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        kernel.session.append = original
        assert prompt_selection(kernel.session, provider, ModelId("fake"))[1] == "builtin"
        change = kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        kernel.improvement_service.rollback(model=ModelId("fake"))
        restored = kernel.improvement_service.rollback(model=ModelId("fake"))
        assert restored.prompt == candidate.artifact and restored.configuration == change.configuration
        assert prompt_selection(kernel.session, provider, ModelId("fake"))[0] == candidate.artifact
    finally:
        kernel.session.close()
