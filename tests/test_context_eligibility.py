"""Eligibility is deterministic; only scoped, eligible text reaches inference."""

import json

import pytest

from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.permissions import PermissionEngine, RuleSet
from harness.provider import FakeProvider, text_turn
from harness.semantic_assessment import ContextSelectionInput
from harness.semantics import AssessmentObservation, SemanticLimits, read_semantics, render_semantics
from harness.types import ModelId
from tests.test_assessment_evaluation import experiment
from tests.test_semantic_assessment import candidates, selection
from tests.test_semantics import kernel_for


async def test_filtered_request_and_original_input_have_distinct_replayable_provenance(tmp_path):
    provider = FakeProvider([text_turn(json.dumps(selection()))])
    kernel = await kernel_for(tmp_path, provider)
    try:
        data = candidates()
        result = await kernel.semantics.select_context(data, model=ModelId("fake"))
        expected = data.model_copy(update={"candidates": (data.candidates[0],)})
        sent = provider.calls[0][-1].text()
        assert ContextSelectionInput.model_validate_json(sent) == expected
        assert kernel.session.blobs.get(result.inference_input).decode() == sent
        assert ContextSelectionInput.model_validate_json(kernel.session.blobs.get(result.input)) == data
        assert result.input_sha256 == result.input.sha256 != result.inference_input.sha256
        assert result.decision_source == "model" and result.call_id is not None
        assert read_semantics(tmp_path, kernel.session.id) == [result]
        legacy = result.model_dump(exclude={"decision_source", "inference_input"})
        assert AssessmentObservation.model_validate(legacy).decision_source == "model"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("empty", [True, False])
async def test_no_eligible_context_works_without_model_authority_or_an_available_model(tmp_path, empty):
    provider = FakeProvider([])
    kernel = await kernel_for(tmp_path, provider,
        permissions=PermissionEngine([RuleSet(rules=[], default="deny")]))
    try:
        data = candidates(candidates=[] if empty else [
            {"id": "absent", "summary": "private unavailable text", "freshness": "current", "available": False},
            {"id": "old", "summary": "private old text", "freshness": "stale"},
            {"id": "unknown", "summary": "private unverified text"}])
        kernel.tasks.create("Keep this task")
        before = kernel.tasks.state()
        pending = kernel.controller.submit("Keep this queued request")
        async with kernel.semantics._lock:  # A busy model does not block a deterministic answer.
            result = await kernel.semantics.select_context(data, model=ModelId("unconfigured"))
        assert result.reason == "no_match" and result.result.selected_ids == ()
        assert result.decision_source == "eligibility" and result.status == "ok"
        assert result.call_id is result.effective_model is result.inference_input is None
        assert not provider.calls
        assert kernel.tasks.state() == before and kernel.controller.pending == (pending,)
        assert not any(e.event.type.startswith(("model_call_", "local_runtime_", "tool_call_"))
                       for e in read_session(tmp_path, kernel.session.id))
        assert "Core eligibility:" in render_semantics(read_semantics(tmp_path, kernel.session.id))
        assert "no model call" in render_semantics([result])
    finally:
        kernel.session.close()


@pytest.mark.parametrize("option", ["disabled", "input_limit"])
async def test_filtering_does_not_bypass_original_input_limit_or_disabled_assessments(tmp_path, option):
    provider = FakeProvider([])
    kernel = await kernel_for(tmp_path, provider)
    try:
        result = await kernel.semantics.select_context(candidates(candidates=[]), model=ModelId("fake"),
            enabled=option != "disabled", limits=SemanticLimits(max_message_bytes=1) if option == "input_limit" else None)
        assert result.reason == option and result.result is None
        assert result.inference_input is None and not provider.calls
        if option == "input_limit":
            assert result.input is None and result.input_sha256 is None
    finally:
        kernel.session.close()


async def test_deterministic_observations_cannot_be_used_as_prompt_improvement_evidence(tmp_path):
    provider = FakeProvider([])
    kernel = await kernel_for(tmp_path, provider)
    try:
        await kernel.semantics.select_context(candidates(candidates=[]), model=ModelId("fake"))
        with pytest.raises(ValueError, match="live assessment"):
            await kernel.improvement_service.compare_assessment(experiment())
        state = read_improvements(tmp_path, kernel.session.id)
        assert not state.candidates and not state.evidence and not provider.calls
    finally:
        kernel.session.close()
