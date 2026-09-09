"""A gain on development cases alone cannot satisfy a declared holdout gate."""

import json

import pytest

from harness.improvement import EvaluationPlan, verdict
from harness.improvement_journal import read_improvements
from harness.provider import text_turn
from harness.session import Session
from harness.types import ModelId, SessionId
from tests.test_assessment_improvement import LearningAssessmentProvider, evaluation, seed_failures
from tests.test_improvement import records
from tests.test_prompt_improvement import (
    HELD_OUT, LearningProvider, experiment as message_experiment, seed_failures as seed_message_failures,
)
from tests.test_semantics import kernel_for


@pytest.mark.parametrize("gate", ["min_held_out_correct", "min_held_out_improved"])
def test_holdout_thresholds_are_strict_bounded_and_compatible_with_old_plans(tmp_path, gate):
    with Session(tmp_path, SessionId("holdout")) as session:
        session.start()
        _, _, plan, result = records(session)
        legacy = plan.model_dump(exclude={"min_held_out_correct", "min_held_out_improved"})
        assert verdict(EvaluationPlan.model_validate(legacy), result) == "passed"
        assert verdict(plan.model_copy(update={gate: 1}), result) == "passed"
        for invalid in (-1, 2, True, 1.0):
            with pytest.raises(ValueError):
                EvaluationPlan.model_validate({**legacy, gate: invalid})
        missing = result.observations[-1].model_copy(update={"candidate_passed": None})
        assert verdict(plan.model_copy(update={gate: 1}), result.model_copy(
            update={"observations": (*result.observations[:-1], missing)})) == "inconclusive"


class DevelopmentOnlyGain(LearningAssessmentProvider):
    """The candidate fixes a known failure but fails the unseen case too."""

    async def infer(self, request):
        if self.seed or request.purpose == "improvement:propose":
            async for chunk in super().infer(request):
                yield chunk
            return
        self.requests.append(request)
        case = next(c for c in self.spec.suite.cases if c.input.model_dump_json() == request.messages[-1].text())
        output = case.expected.model_dump(mode="json")
        candidate = request.messages[0].text() == "candidate"
        if case.partition == "held_out" or case.id == "critical-stale" and not candidate:
            output.update(selected_ids=[], reason="uncertain")
        for chunk in text_turn(json.dumps(output)):
            yield chunk


@pytest.mark.parametrize("path", ["compare", "propose_evaluate"])
@pytest.mark.parametrize("gate", ["min_held_out_correct", "min_held_out_improved"])
async def test_declared_holdout_gate_blocks_real_adoption_path(tmp_path, path, gate):
    provider = DevelopmentOnlyGain("context_selection")
    provider.spec = provider.spec.model_copy(update={gate: 1})
    kernel = await kernel_for(tmp_path, provider)
    try:
        await seed_failures(kernel, "context_selection", provider)
        if path == "compare":
            result = await kernel.improvement_service.compare_assessment(provider.spec)
        else:
            candidate = await kernel.improvement_service.propose(model=ModelId("fake"), function="context_selection")
            result = await kernel.improvement_service.evaluate(candidate.id, experiment=evaluation(provider))
        state = read_improvements(tmp_path, kernel.session.id)
        plan = state.plans[result.plan_id]
        assert getattr(plan, gate) == 1
        # The old overall gate passes on the development gain despite zero
        # correct unseen answers. The frozen additional gate must refuse it.
        assert verdict(plan.model_copy(update={gate: 0}), result) == "passed"
        assert verdict(plan, result) == "failed"
        with pytest.raises(ValueError, match="cannot be adopted"):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"), function="context_selection")
        assert not read_improvements(tmp_path, kernel.session.id).prompt_changes
        report = json.loads(kernel.session.blobs.get(result.artifact))
        assert report["gates"][gate] == 1 and not report["activation_qualified"]
    finally:
        kernel.session.close()


class MessageDevelopmentOnlyGain(LearningProvider):
    async def infer(self, request):
        if request.messages[-1].text() == HELD_OUT:
            self.requests.append(request)
            for chunk in text_turn('{"kind":"uncertain"}'):
                yield chunk
            return
        async for chunk in super().infer(request):
            yield chunk


@pytest.mark.parametrize("gate", ["min_held_out_correct", "min_held_out_improved"])
async def test_message_evaluation_cannot_drop_the_declared_holdout_gate(tmp_path, gate):
    from harness.semantic_evaluation import MessageEvaluationCase, MessageEvaluationSuite

    spec = message_experiment()
    known = MessageEvaluationCase(id="known-question", partition="regression",
                                  text="How is the known fix going?", expected="question")
    spec = spec.model_copy(update={gate: 1,
        "suite": MessageEvaluationSuite(cases=(*spec.suite.cases, known))})
    kernel = await kernel_for(tmp_path, MessageDevelopmentOnlyGain())
    try:
        await seed_message_failures(kernel)
        candidate = await kernel.improvement_service.propose(model=ModelId("fake"))
        result = await kernel.improvement_service.evaluate(candidate.id, experiment=spec)
        plan = read_improvements(tmp_path, kernel.session.id).plans[result.plan_id]
        assert getattr(plan, gate) == 1
        assert verdict(plan.model_copy(update={gate: 0}), result) == "passed"
        assert verdict(plan, result) == "failed"
        with pytest.raises(ValueError, match="cannot be adopted"):
            kernel.improvement_service.adopt(result.id, model=ModelId("fake"))
        assert not read_improvements(tmp_path, kernel.session.id).prompt_changes
    finally:
        kernel.session.close()
