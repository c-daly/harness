"""Persisted coordination facts; execution completion never accepts a task."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.agent import DelegationResult
from harness.blobs import BlobRef
from harness.tasks import MAX_REQUIREMENTS, RequirementEvidence, TaskRequirement
from harness.types import SessionId

MAX_REPORT_BYTES = 1024 * 1024


class CoordinationMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["expert", "judge", "proposer", "critic", "drafter", "refiner", "cheap", "premium", "verifier"]
    model: str = Field(min_length=1, max_length=128)
    agent: str | None = Field(default=None, min_length=1, max_length=128)
    result: DelegationResult
    evidence: tuple[RequirementEvidence, ...] | None = Field(default=None, max_length=MAX_REQUIREMENTS)


class CoordinationCheckSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session_id: SessionId
    task_id: str
    run_id: str
    basis_seq: int = Field(ge=1, strict=True)
    objective: str = Field(min_length=1, max_length=4096)


class CoordinationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    id: str
    source_session_id: SessionId | None
    strategy: str
    acceptance: Literal["unverified"] = "unverified"
    members: list[CoordinationMember] = Field(max_length=17)
    disagreement: bool
    gate: Literal["none", "synthesis", "text_vote", "advisory_review", "unreviewed", "execution_only", "recorded_checks"]
    unresolved: list[str]
    result: DelegationResult
    output: BlobRef | None
    admitted: bool | None = Field(default=None, strict=True)
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    requirements: tuple[TaskRequirement, ...] | None = Field(default=None, min_length=1, max_length=MAX_REQUIREMENTS)
    check_source: CoordinationCheckSource | None = None

    @model_validator(mode="after")
    def paired_admission(self):
        if (self.admitted is None) != (self.timeout_seconds is None):
            raise ValueError("coordination admission and deadline must be recorded together")
        if (self.requirements is None) != (self.check_source is None):
            raise ValueError("coordination requirements need their task source")
        ids = [r.id for r in self.requirements or ()]
        if len(set(ids)) != len(ids):
            raise ValueError("coordination requirement IDs must be unique")
        if self.gate == "recorded_checks" and not ids:
            raise ValueError("recorded checks require declared requirements")
        if self.check_source is not None and (self.strategy != "escalate" or
                                               self.check_source.session_id != self.source_session_id):
            raise ValueError("check source must belong to this escalation's calling session")
        for member in self.members:
            if member.evidence is not None and (not ids or [e.requirement_id for e in member.evidence] != ids):
                raise ValueError("participant evidence must cover every declared requirement in order")
        return self


def load_report(blobs, event, session_id) -> CoordinationReport:
    """Validate bytes and source association before inspection or export."""
    if event.report.size > MAX_REPORT_BYTES:
        raise ValueError("coordination report exceeds limit")
    report = CoordinationReport.model_validate_json(blobs.get(event.report))
    if (report.id != event.id or report.result.status != event.status
            or report.source_session_id != session_id or report.strategy != event.strategy):
        raise ValueError("coordination report does not match its event")
    return report
