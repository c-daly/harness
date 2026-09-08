"""Paired semantic-prompt experiments with fixed, non-model graders.

Only prompt data is variable. Candidate bytes never become executable code,
schemas, labels, limits, or adoption policy. Runtime identities are declarations;
these experiments do not establish that a server's weights match its label.
"""

import asyncio
import hashlib
import json
import sys
from importlib.metadata import version as package_version
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.improvement import EvaluationCase, ExperimentResult, Measurement
from harness.semantics import MessageKind, SemanticLimits, load_prompt, rule_message_kind
from harness.types import ModelId


class MessageEvaluationCase(EvaluationCase):
    text: str = Field(min_length=1, max_length=16384)
    expected: MessageKind


REQUIRED_CASES = (
    MessageEvaluationCase(id="critical-stop", partition="regression", critical=True,
                          text="Stop.", expected="stop_request"),
    MessageEvaluationCase(id="critical-thanks", partition="regression", critical=True,
                          text="Thanks, I may have more changes.", expected="uncertain"),
)


class MessageEvaluationSuite(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: int = Field(default=1, ge=1, le=1, strict=True)
    cases: tuple[MessageEvaluationCase, ...] = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def fixed_cases(self):
        cases = {c.id: c for c in self.cases}
        if len(cases) != len(self.cases):
            raise ValueError("evaluation case IDs must be unique")
        if any(cases.get(c.id) != c for c in REQUIRED_CASES):
            raise ValueError("evaluation must preserve the fixed critical stop and ambiguity cases")
        if not any(c.partition == "held_out" for c in self.cases):
            raise ValueError("evaluation requires held-out cases")
        return self

    def plan_cases(self):
        return tuple(EvaluationCase.model_validate(c.model_dump(exclude={"text", "expected"}))
                     for c in self.cases)


class EvaluatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    model: ModelId
    runtime_version: str = Field(min_length=1, max_length=256)
    limits: SemanticLimits = Field(default_factory=SemanticLimits)
    timeout_seconds: float = Field(default=120, gt=0, le=600)


def evaluator_version(provider, config: EvaluatorConfig) -> str:
    """Bind grader/service source, bounds, model declaration, and catalog config."""
    config = EvaluatorConfig.model_validate(config.model_dump())
    source = Path(__file__).parent
    files = ("semantic_evaluation.py", "semantics.py", "semantic_assessment.py", "improvement.py", "inference.py",
             "dispatcher.py", "provider_litellm.py", "prompt_improvement.py", "improvement_journal.py",
             "resources.py", "scheduling.py", "catalog.py", "assessment_evaluation.py")
    catalog = getattr(provider, "catalog", None)
    payload = {
        "source": {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files},
        "configuration": config.model_dump(mode="json"),
        "provider_type": f"{type(provider).__module__}.{type(provider).__qualname__}",
        "catalog_entry": catalog.entries.get(str(config.model)) if catalog else None,
        "provider_endpoint": getattr(provider, "api_base", None),
        "provider_key_name": getattr(provider, "api_key_env", None),
        "python": list(sys.version_info[:3]),
        "packages": {name: package_version(name) for name in ("pydantic", "jsonschema", "litellm", "mcp")},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


class MessageGrader:
    function = "message_kind"

    def __init__(self, data):
        self.suite = MessageEvaluationSuite.model_validate(data)

    def input_text(self, case):
        return case.text

    def load_prompt(self, blobs, ref):
        return load_prompt(blobs, ref)

    def rules(self, case):
        return rule_message_kind(case.text)

    def matches(self, case, result):
        return result == case.expected

    def output(self, observation):
        return observation.kind

    def abstained(self, result):
        return result == "uncertain"

    async def observe(self, kernel, case, *, model, limits, prompt, run_id):
        return await kernel.semantics.interpret(case.text, model=model, limits=limits, prompt=prompt)


def _measurement(case, values, grader):
    def score(value):
        if value is None:
            return None
        if value.reason in {"classified", "uncertain", "assessed", "no_match"}:
            return grader.matches(case, grader.output(value))
        if value.reason in {"invalid_output", "timeout"}:
            return False
        return None
    left, right = values.get("incumbent"), values.get("candidate")
    return Measurement(case_id=case.id, incumbent_passed=score(left), candidate_passed=score(right),
        incumbent_latency_ms=left.duration_ms if left else None,
        candidate_latency_ms=right.duration_ms if right else None)


def _metrics(suite, rows, grader):
    def summarize(cases, side):
        values = [rows[c.id].get(side) for c in cases]
        passed = [_measurement(c, rows[c.id], grader).model_dump()[f"{side}_passed"]
                  for c in cases] if side != "rules" else [grader.matches(c, grader.rules(c)) for c in cases]
        latencies = sorted(v.duration_ms for v in values if v is not None)
        return {"samples": len(cases), "measured": sum(p is not None for p in passed),
                "correct": sum(p is True for p in passed),
                "abstentions": sum(v.status == "abstained" for v in values if v is not None)
                if side != "rules" else sum(grader.abstained(grader.rules(c)) for c in cases),
                "max_latency_ms": max(latencies) if latencies else None,
                "p95_latency_ms": latencies[(95 * len(latencies) + 99) // 100 - 1] if latencies else None}
    return {side: {**summarize(suite.cases, side), **{
        partition: summarize([c for c in suite.cases if c.partition == partition], side)
        for partition in ("regression", "held_out")}} for side in ("incumbent", "candidate", "rules")}


def load_grader(data):
    if not isinstance(data, dict):
        raise ValueError("evaluation suite must be a JSON object")
    if "function" not in data:
        return MessageGrader(data)
    from harness.assessment_evaluation import AssessmentGrader
    return AssessmentGrader(data)


async def run_evaluation(kernel, plan_id, *, incumbent, config: EvaluatorConfig) -> ExperimentResult:
    from harness.events import EvaluationRunStarted, EvaluationRunFinished
    from harness.fold import fold
    from harness.improvement_journal import read_improvements
    from harness.log import read_session

    config = EvaluatorConfig.model_validate(config.model_dump())
    session, journal = kernel.session, kernel.improvements
    state = read_improvements(session.base, session.id)
    plan = state.plans[plan_id]
    candidate = state.candidates[plan.candidate_id]
    version = evaluator_version(kernel.provider, config)
    if candidate.target != "prompt" or incumbent.sha256 != plan.incumbent_version:
        raise ValueError("semantic evaluation requires the planned incumbent and a prompt candidate")
    if version != plan.evaluator_version:
        raise ValueError("evaluator configuration or implementation differs from the fixed plan")
    if plan.suite.size > 1024 * 1024:
        raise ValueError("evaluation suite exceeds 1 MiB")
    grader = load_grader(json.loads(session.blobs.get(plan.suite)))
    suite = grader.suite
    if suite.plan_cases() != plan.cases:
        raise ValueError("suite case identities, partitions, or critical flags differ from the plan")
    if any(len(grader.input_text(c).encode()) > config.limits.max_message_bytes for c in suite.cases):
        raise ValueError("evaluation inputs exceed the fixed semantic message limit")
    grader.load_prompt(session.blobs, incumbent)
    grader.load_prompt(session.blobs, candidate.artifact)
    if fold(read_session(session.base, session.id)).open_evaluations:
        raise ValueError("an evaluation is already running; resume interrupted records before retrying")
    run_id = str(uuid4())
    session.append(EvaluationRunStarted(run_id=run_id, plan_id=plan.id, incumbent=incumbent,
        configuration=session.blobs.put(config.model_dump_json().encode())))
    rows = {case.id: {} for case in suite.cases}
    status = "failed"
    try:
        async with asyncio.timeout(config.timeout_seconds):
            for index, case in enumerate(suite.cases):
                # Alternate order to avoid always giving the candidate the warm position.
                order = ("incumbent", "candidate") if index % 2 == 0 else ("candidate", "incumbent")
                for side in order:
                    if evaluator_version(kernel.provider, config) != version:
                        raise ValueError("evaluator/provider configuration changed during the experiment")
                    rows[case.id][side] = await grader.observe(
                        kernel, case, model=config.model, limits=config.limits, run_id=run_id,
                        prompt=incumbent if side == "incumbent" else candidate.artifact,
                    )
                    if evaluator_version(kernel.provider, config) != version:
                        raise ValueError("evaluator/provider configuration changed during the experiment")
        status = "completed"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except TimeoutError:
        status = "timed_out"
    finally:
        report = {"schema_version": 1, "function": grader.function, "run_id": run_id, "completion": status,
            "activation_qualified": False, "held_out_provenance": "operator_declared",
            "runtime_identity_verified": False, "configuration": config.model_dump(mode="json"),
            "evaluator_version": version, "suite": plan.suite.model_dump(),
            "metrics": _metrics(suite, rows, grader), "cases": [{
                **case.model_dump(mode="json"), "rules": grader.rules(case),
                **{side: rows[case.id][side].model_dump(mode="json") if side in rows[case.id] else None
                   for side in ("incumbent", "candidate")},
            } for case in suite.cases]}
        result = ExperimentResult(id=str(uuid4()), plan_id=plan.id,
            incumbent_version=plan.incumbent_version, candidate_version=plan.candidate_version,
            evaluator_version=version, run_id=run_id, completion=status,
            observations=tuple(_measurement(c, rows[c.id], grader) for c in suite.cases),
            artifact=session.blobs.put(json.dumps(report, sort_keys=True, allow_nan=False).encode()))
        journal.record(result)
        session.append(EvaluationRunFinished(run_id=run_id, status=status, result_id=result.id))
    return result
