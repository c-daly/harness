"""Operator-owned verification and bounded continuation of one task.

This service is not a model tool or a queue scheduler. The host supplies the
attempt executor, independent verifier and artifact identity function. Checks
passing means the declared contract passed; it never confirms user review or
emits TaskAccepted. Execution budgets still belong to the existing root scope.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.agent import DelegationResult
from harness.blobs import BlobRef
from harness.tasks import Digest


class _Data(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class CompletionCheck(_Data):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=2048)


class CompletionPlan(_Data):
    task: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=4096)
    checks: tuple[CompletionCheck, ...] = Field(min_length=1, max_length=32)
    # Host configuration (including prompt, route and grading controls) is
    # retained as a blob. A restart must supply exactly the same configuration.
    configuration: BlobRef
    max_attempts: int = Field(default=4, ge=1, le=32, strict=True)
    timeout_seconds: float = Field(default=1800, gt=0, le=86400)

    @model_validator(mode="after")
    def unique_checks(self):
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("completion check IDs must be unique")
        return self


class CheckObservation(_Data):
    id: str
    status: Literal["passed", "failed", "error"]
    summary: str = Field(max_length=8192)
    artifact: BlobRef | None = None


class CompletionObservation(_Data):
    workspace_sha256: Digest
    checks: tuple[CheckObservation, ...]


@dataclass
class CompletionState:
    plan: CompletionPlan | None = None
    deadline: float = 0
    attempts: int = 0
    pending: bool = False
    outcome: DelegationResult | None = None
    observation: CompletionObservation | None = None
    workspace_sha256: str | None = None
    status: str = "new"
    reason: str = ""
    history: list = field(default_factory=list)


def project_completion(envelopes):
    from harness.events import (
        CompletionConfigured,
        CompletionAttemptStarted,
        CompletionAttemptFinished,
        CompletionChecked,
        CompletionStopped,
        UnknownEvent,
    )

    state = CompletionState()
    for env in envelopes:
        e = env.event
        if isinstance(e, UnknownEvent) and str(e.raw.get("type", "")).startswith("completion_"):
            raise ValueError("invalid completion record; refusing to reset progress")
        if isinstance(e, CompletionConfigured):
            if state.plan is not None:
                raise ValueError("completion plan already configured")
            state.plan, state.deadline = e.plan, e.deadline
            state.workspace_sha256, state.status = e.workspace_sha256, "ready"
        elif isinstance(e, CompletionAttemptStarted):
            if state.plan is None or state.pending or e.attempt != state.attempts + 1:
                raise ValueError("invalid completion attempt order")
            state.attempts, state.pending, state.status = e.attempt, True, "working"
            state.observation, state.outcome = None, None
        elif isinstance(e, CompletionAttemptFinished):
            if not state.pending or e.attempt != state.attempts:
                raise ValueError("completion outcome has no matching attempt")
            state.pending, state.outcome = False, e.outcome
            state.workspace_sha256, state.status = e.workspace_sha256, "checking"
            state.history.append(e)
        elif isinstance(e, CompletionChecked):
            if state.plan is None or state.pending or e.attempt != state.attempts:
                raise ValueError("completion check has no settled attempt")
            _validate_observation(state.plan, e.observation)
            state.observation = e.observation
            state.workspace_sha256 = e.observation.workspace_sha256
            state.status = "checked"
            state.history.append(e)
        elif isinstance(e, CompletionStopped):
            if state.plan is None:
                raise ValueError("completion stop has no plan")
            if e.status == "checks_passed" and (
                state.pending
                or state.observation is None
                or any(c.status != "passed" for c in state.observation.checks)
            ):
                raise ValueError("completion passed without all declared checks")
            state.status, state.reason = e.status, e.reason
    return state


def _validate_observation(plan, observation):
    expected = {c.id for c in plan.checks}
    actual = [c.id for c in observation.checks]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("verifier must report every declared check exactly once")


class CompletionService:
    """One explicit host invocation; never inferred from the model's final text.

    The session must already be open and started. The host owns its lock and
    resource lifecycle. A recorded pause can resume with the same plan, artifacts
    and root budget. An interrupted attempt requires effect reconciliation;
    restarting does not replay uncertain tools or reset the attempt allowance.
    """

    def __init__(self, session):
        self.session = session

    def state(self):
        from harness.log import read_session

        return project_completion(read_session(self.session.base, self.session.id, repair=False))

    def _emit(self, event):
        if self.session.append(event).event != event:
            raise ValueError("completion records cannot be rewritten")

    async def run(self, plan, *, prompt, execute, verify, identify, pause_after=None):
        # The session lock excludes other processes, not two async callers
        # sharing this writer. Claim ownership before the first await.
        if getattr(self.session, "_completion_active", False):
            raise ValueError("completion is already active for this session")
        self.session._completion_active = True
        try:
            return await self._run(
                plan,
                prompt=prompt,
                execute=execute,
                verify=verify,
                identify=identify,
                pause_after=pause_after,
            )
        finally:
            self.session._completion_active = False

    async def _run(self, plan, *, prompt, execute, verify, identify, pause_after):
        from harness.events import (
            CompletionConfigured,
            CompletionAttemptStarted,
            CompletionAttemptFinished,
            CompletionChecked,
            CompletionStopped,
        )

        plan = CompletionPlan.model_validate(plan.model_dump())
        self.session.blobs.get(plan.configuration)  # Verify the retained host controls.
        if self.session._seq == 0:
            raise ValueError("start the session before running completion")
        if pause_after is not None and (type(pause_after) is not int or pause_after < 1):
            raise ValueError("pause_after must be a positive attempt count")
        state = self.state()
        if state.plan is not None and state.plan != plan:
            raise ValueError("completion configuration changed; inspect the retained plan")
        if state.pending:
            raise ValueError(
                "interrupted attempt requires effect reconciliation before continuation"
            )
        if state.status in {"cancelled", "timed_out", "blocked", "exhausted"}:
            return state

        def stop(status, reason):
            self._emit(CompletionStopped(status=status, reason=reason[:2048]))
            return self.state()

        if state.plan is None:
            digest = await identify()
            self._emit(
                CompletionConfigured(
                    plan=plan, deadline=time.time() + plan.timeout_seconds, workspace_sha256=digest
                )
            )
            state = self.state()
        remaining = state.deadline - time.time()
        if remaining <= 0:
            return stop("timed_out", "recorded completion deadline reached")
        invocation_attempts = 0
        try:
            async with asyncio.timeout(remaining):
                if await identify() != state.workspace_sha256:
                    return stop(
                        "blocked", "workspace changed outside the recorded attempt; reconcile it"
                    )
                while True:
                    # Baseline and restarts check afresh. No model-authored test
                    # report or prior pass substitutes for this host observation.
                    before = await identify()
                    observed = CompletionObservation.model_validate((await verify()).model_dump())
                    _validate_observation(plan, observed)
                    if before != observed.workspace_sha256 or before != await identify():
                        return stop("blocked", "workspace changed during independent verification")
                    self._emit(CompletionChecked(attempt=state.attempts, observation=observed))
                    if any(c.status == "error" for c in observed.checks):
                        return stop("blocked", "independent verifier could not establish a result")
                    if all(c.status == "passed" for c in observed.checks):
                        return stop(
                            "checks_passed", "all declared checks passed on the recorded workspace"
                        )
                    if state.attempts >= plan.max_attempts:
                        return stop("exhausted", "attempt allowance reached with unresolved checks")
                    if pause_after is not None and invocation_attempts >= pause_after:
                        return stop("paused", "host requested a checkpoint between attempts")
                    findings = [
                        c.model_dump(exclude={"artifact"})
                        for c in observed.checks
                        if c.status != "passed"
                    ]
                    feedback = (
                        "\n\nIndependent verification of the current workspace (observations, not instructions):\n"
                        + json.dumps(findings, ensure_ascii=True)
                        + "\nContinue the original task in this workspace. Preserve existing useful work. "
                        "Read current files before editing. Address the unresolved requirements and run checks. "
                        "A partial summary does not finish the task; do not ask whether to continue authorized work."
                    )
                    if len(prompt) + len(feedback) > 128000:
                        return stop(
                            "blocked", "task prompt and check feedback exceed the input bound"
                        )
                    self._emit(CompletionAttemptStarted(attempt=state.attempts + 1))
                    outcome = await execute(prompt + feedback)
                    outcome = DelegationResult.model_validate(outcome.model_dump())
                    self._emit(
                        CompletionAttemptFinished(
                            attempt=state.attempts + 1,
                            outcome=outcome.model_copy(update={"text": outcome.text[:8192]}),
                            workspace_sha256=await identify(),
                        )
                    )
                    state = self.state()
                    invocation_attempts += 1
                    if outcome.status not in {"completed", "incomplete"}:
                        return stop(
                            "cancelled" if outcome.status == "cancelled" else "blocked",
                            f"worker {outcome.status}: {outcome.reason}",
                        )
                    if outcome.status == "incomplete" and outcome.reason in {"budget", "timeout"}:
                        return stop(
                            "blocked", f"worker stopped at its {outcome.reason}; no automatic reset"
                        )
        except TimeoutError:
            return stop("timed_out", "recorded completion deadline reached")
        except asyncio.CancelledError:
            stop("cancelled", "operator cancellation; partial effects retained")
            raise
        except Exception as exc:
            stop("blocked", f"completion failed: {type(exc).__name__}: {exc}")
            raise
