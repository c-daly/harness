"""Model comparisons must retain the existing public correctness and latency gates."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from harness.blobs import BlobRef
from harness.semantic_assessment import CONTEXT_PROMPT, ContextSelection, function_version
from harness.semantics import ASSESSMENT_LIMITS, AssessmentObservation
from scripts.measure_context_profile import IMAGE, MODEL_PROFILES, PROFILES, compare, profile_catalog
from scripts.qualify_assessment_evaluation import public_experiments


def ref(value):
    data = value.model_dump_json().encode()
    return BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def report(profile, *, wrong=(), latency=100):
    suite = public_experiments()[0].suite
    weights = MODEL_PROFILES[PROFILES[profile][0]]
    rows = []
    for case in suite.cases:
        result = ContextSelection(selected_ids=(), reason="no_match") if case.id in wrong else case.expected
        observed = AssessmentObservation(id=case.id, function="context_selection", model="local-small",
            function_version=function_version("context_selection"), limits=ASSESSMENT_LIMITS,
            prompt=ref(CONTEXT_PROMPT), input=ref(case.input), reason="assessed", result=result, duration_ms=latency)
        rows.append({"id": case.id, "correct": True, "observation": observed.model_dump(mode="json")})
    return {"profile": profile, "mechanics_passed": True, "weights_verified": True, "image": IMAGE,
        "weights": {k: v for k, v in weights.items() if k != "runtime_args"},
        "catalog": profile_catalog(profile, Path("/models/model.gguf")).entries,
        "source_sha256": {"fixture": "unchanged"}, "suite": suite.model_dump(mode="json"),
        "prompt": CONTEXT_PROMPT.model_dump(mode="json"), "cases": rows}


def baseline():
    return report("baseline-8b", wrong=("critical-ambiguous",))


def test_complete_gain_passes_development_without_qualifying_or_adopting():
    result = compare(baseline(), report("candidate-4b"))
    assert result["development_passed"]
    assert not result["quality_qualification"]
    assert result["improved_ids"] == ["critical-ambiguous"]
    assert result["candidate_correct"] == 6 and result["baseline_correct"] == 5


def test_regrades_outputs_and_rejects_new_regression_despite_fixed_critical_case():
    result = compare(baseline(), report("candidate-4b", wrong=("public-paraphrase",)))
    assert result["improved_ids"] == ["critical-ambiguous"]
    assert result["regressed_ids"] == ["public-paraphrase"]
    assert result["candidate_correct"] == 5
    assert not result["development_passed"]


@pytest.mark.parametrize("wrong,latency,gate", [
    (("critical-ambiguous",), 100, "critical_passed"),
    ((), 2001, "case_latency"),
    ((), 121, "latency_ratio"),
])
def test_quality_and_latency_gates_remain_required(wrong, latency, gate):
    result = compare(baseline(), report("candidate-4b", wrong=wrong, latency=latency))
    assert not result["gates"][gate]
    assert not result["development_passed"]


@pytest.mark.parametrize("change", ["incomplete", "unverified", "wrong_profile", "wrong_input", "changed_command", "changed_source"])
def test_mismatched_or_incomplete_evidence_is_rejected(change):
    candidate = report("candidate-4b")
    if change == "incomplete":
        candidate["cases"].pop()
    elif change == "unverified":
        candidate["weights_verified"] = False
    elif change == "wrong_profile":
        candidate["profile"] = "baseline-8b"
    elif change == "wrong_input":
        candidate["cases"][0]["observation"]["input"] = candidate["cases"][1]["observation"]["input"]
    elif change == "changed_command":
        candidate["catalog"]["local-small"]["local"]["command"][-1] = "1.5"
    else:
        candidate["source_sha256"]["fixture"] = "changed"
    with pytest.raises(ValueError):
        compare(baseline(), candidate)


def test_effective_commands_override_defaults_without_mutating_shared_profiles():
    original = deepcopy(MODEL_PROFILES)
    for profile, layers in (("baseline-8b", "28"), ("candidate-4b", "99")):
        command = profile_catalog(profile, Path("/models/model.gguf")).entries["local-small"]["local"]["command"]
        for flag, expected in (("--n-gpu-layers", layers), ("--ctx-size", "4096"), ("--presence-penalty", "0")):
            assert command[command.index(flag) + 1] == expected
    assert MODEL_PROFILES == original
