"""Core improvement records and evaluation gates, independent of plugins.

Evidence and candidate claims are distinct from measured results. Plans are
fixed before experiments and bind exact incumbent/candidate content digests.
Eligibility does not activate behavior or edit files: runtime activation and
isolated source experiments consume these contracts at their safe boundary.
"""

from dataclasses import dataclass, field
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.blobs import BlobRef
from harness.types import SessionId

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Target = Literal["prompt", "routing", "context", "code"]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    id: Identifier


class Evidence(_Record):
    kind: Literal["evidence"] = "evidence"
    source_session: SessionId
    source_seq: int = Field(gt=0, strict=True)
    observation: str = Field(min_length=1, max_length=4096)
    category: Literal["correction", "failure", "latency", "task_outcome"]


class Candidate(_Record):
    kind: Literal["candidate"] = "candidate"
    target: Target
    incumbent_version: Digest
    artifact: BlobRef  # candidate version is its exact artifact SHA-256
    evidence_ids: tuple[Identifier, ...] = Field(min_length=1)
    hypothesis: str = Field(min_length=1, max_length=4096)
    expected_benefit: str = Field(min_length=1, max_length=4096)


class EvaluationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: Identifier
    partition: Literal["regression", "held_out"]
    critical: bool = False


class EvaluationPlan(_Record):
    kind: Literal["evaluation_plan"] = "evaluation_plan"
    candidate_id: Identifier
    incumbent_version: Digest
    candidate_version: Digest
    suite: BlobRef  # exact inputs/expected results, never just a mutable path
    evaluator_version: Digest
    cases: tuple[EvaluationCase, ...] = Field(min_length=2)
    min_improved_cases: int = Field(default=1, ge=0, strict=True)
    max_latency_ratio: float = Field(default=1.2, gt=0)
    max_case_latency_ms: float = Field(gt=0)

    @model_validator(mode="after")
    def fixed_suite(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("evaluation case IDs must be unique")
        if {c.partition for c in self.cases} != {"regression", "held_out"}:
            raise ValueError("evaluation requires regression and held-out cases")
        if self.min_improved_cases > len(self.cases):
            raise ValueError("improvement threshold exceeds case count")
        return self


class Measurement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    case_id: Identifier
    incumbent_passed: bool | None = Field(strict=True)
    candidate_passed: bool | None = Field(strict=True)
    incumbent_latency_ms: float | None = Field(ge=0)
    candidate_latency_ms: float | None = Field(ge=0)


class ExperimentResult(_Record):
    kind: Literal["experiment_result"] = "experiment_result"
    plan_id: Identifier
    incumbent_version: Digest
    candidate_version: Digest
    evaluator_version: Digest
    observations: tuple[Measurement, ...]
    artifact: BlobRef  # preserved evaluator output/provenance
    run_id: Identifier | None = None
    completion: Literal["completed", "cancelled", "timed_out", "failed", "aborted"] = "completed"


ImprovementRecord = Annotated[
    Union[Evidence, Candidate, EvaluationPlan, ExperimentResult], Field(discriminator="kind"),
]


def verdict(plan: EvaluationPlan, result: ExperimentResult) -> Literal["passed", "failed", "inconclusive"]:
    plan = EvaluationPlan.model_validate(plan.model_dump())
    result = ExperimentResult.model_validate(result.model_dump())
    if (result.plan_id != plan.id or result.incumbent_version != plan.incumbent_version
            or result.candidate_version != plan.candidate_version
            or result.evaluator_version != plan.evaluator_version):
        raise ValueError("experiment does not match its fixed evaluation plan")
    observations = {o.case_id: o for o in result.observations}
    if len(observations) != len(result.observations) or set(observations) != {c.id for c in plan.cases}:
        raise ValueError("experiment must report each planned case exactly once")
    improved = 0
    incumbent_ms = candidate_ms = 0.0
    incomplete = result.completion != "completed"
    failed = False
    for case in plan.cases:
        value = observations[case.id]
        # Known critical/regression failures remain failures even if another
        # measurement is absent. Unknown values can never qualify promotion.
        if (case.critical or value.incumbent_passed is True) and value.candidate_passed is False:
            failed = True
        fields = (value.incumbent_passed, value.candidate_passed,
                  value.incumbent_latency_ms, value.candidate_latency_ms)
        if any(v is None for v in fields):
            incomplete = True
            continue
        improved += int(value.incumbent_passed is False and value.candidate_passed is True)
        incumbent_ms += value.incumbent_latency_ms
        candidate_ms += value.candidate_latency_ms
        failed |= value.candidate_latency_ms > plan.max_case_latency_ms
    if failed:
        return "failed"
    if incomplete:
        return "inconclusive"
    if improved < plan.min_improved_cases or candidate_ms > incumbent_ms * plan.max_latency_ratio:
        return "failed"
    return "passed"


class AdoptionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Digest
    automatic_targets: frozenset[Target] = frozenset()


def adoption_decision(
    candidate: Candidate, plan: EvaluationPlan, result: ExperimentResult, policy: AdoptionPolicy,
) -> Literal["eligible", "review_required", "refused"]:
    """Eligibility only. No inference result grants activation or code authority."""
    candidate = Candidate.model_validate(candidate.model_dump())
    policy = AdoptionPolicy.model_validate(policy.model_dump())
    if (candidate.id != plan.candidate_id or candidate.artifact.sha256 != plan.candidate_version
            or candidate.incumbent_version != plan.incumbent_version):
        raise ValueError("evaluation plan targets a different candidate version")
    if verdict(plan, result) != "passed":
        return "refused"
    return "eligible" if candidate.target in policy.automatic_targets else "review_required"


@dataclass
class ImprovementState:
    evidence: dict[str, Evidence] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    plans: dict[str, EvaluationPlan] = field(default_factory=dict)
    results: dict[str, ExperimentResult] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)

    def apply(self, record: ImprovementRecord) -> None:
        # Snapshot mutable nested data and validate constructed/copied instances.
        from pydantic import TypeAdapter
        record = TypeAdapter(ImprovementRecord).validate_python(record.model_dump())
        if any(record.id in values for values in (self.evidence, self.candidates, self.plans, self.results)):
            raise ValueError("improvement record IDs are immutable and unique")
        if isinstance(record, Evidence):
            self.evidence[record.id] = record
        elif isinstance(record, Candidate):
            if any(key not in self.evidence for key in record.evidence_ids):
                raise ValueError("candidate references missing evidence")
            self.candidates[record.id] = record
        elif isinstance(record, EvaluationPlan):
            candidate = self.candidates.get(record.candidate_id)
            if (candidate is None or candidate.artifact.sha256 != record.candidate_version
                    or candidate.incumbent_version != record.incumbent_version):
                raise ValueError("plan must bind the recorded candidate and incumbent versions")
            self.plans[record.id] = record
        elif isinstance(record, ExperimentResult):
            plan = self.plans.get(record.plan_id)
            if plan is None:
                raise ValueError("experiment requires a previously recorded evaluation plan")
            verdict(plan, record)
            self.results[record.id] = record
