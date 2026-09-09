"""Persisted coordination facts; execution completion never accepts a task."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.agent import DelegationResult
from harness.blobs import BlobRef
from harness.types import SessionId

MAX_REPORT_BYTES = 1024 * 1024


class CoordinationMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["expert", "judge", "proposer", "critic", "drafter", "refiner", "cheap", "premium", "verifier"]
    model: str = Field(min_length=1, max_length=128)
    agent: str | None = Field(default=None, min_length=1, max_length=128)
    result: DelegationResult


class CoordinationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    id: str
    source_session_id: SessionId | None
    strategy: str
    acceptance: Literal["unverified"] = "unverified"
    members: list[CoordinationMember] = Field(max_length=17)
    disagreement: bool
    gate: Literal["none", "synthesis", "text_vote", "advisory_review", "unreviewed", "execution_only"]
    unresolved: list[str]
    result: DelegationResult
    output: BlobRef | None


def load_report(blobs, event, session_id) -> CoordinationReport:
    """Validate bytes and source association before inspection or export."""
    if event.report.size > MAX_REPORT_BYTES:
        raise ValueError("coordination report exceeds limit")
    report = CoordinationReport.model_validate_json(blobs.get(event.report))
    if (report.id != event.id or report.result.status != event.status
            or report.source_session_id != session_id or report.strategy != event.strategy):
        raise ValueError("coordination report does not match its event")
    return report
