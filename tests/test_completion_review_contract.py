"""Independent review of checkpoint lineage and stop semantics."""

import hashlib

import pytest

from harness.agent import DelegationResult
from harness.cli import build_kernel
from harness.completion import (
    CheckObservation,
    CompletionCheck,
    CompletionObservation,
    CompletionPlan,
    CompletionService,
)
from harness.provider import FakeProvider
from harness.types import ModelId


class Work:
    def __init__(self):
        self.step = 0
        self.executions = 0

    async def identify(self):
        return hashlib.sha256(str(self.step).encode()).hexdigest()

    async def verify(self):
        return CompletionObservation(
            workspace_sha256=await self.identify(),
            checks=(
                CheckObservation(
                    id="result",
                    status="passed" if self.step == 3 else "failed",
                    summary=f"actual step {self.step}",
                ),
            ),
        )

    async def execute(self, prompt):
        self.step += 1
        self.executions += 1
        return DelegationResult(status="completed", text=f"partial {self.step}")


def kernel_and_plan(path, resume=None):
    k = build_kernel(
        provider=FakeProvider([]), base_dir=path, model=ModelId("fake"), resume_session_id=resume
    )
    p = CompletionPlan(
        task="review-contract",
        objective="finish",
        checks=(CompletionCheck(id="result", description="result meets requirements"),),
        configuration=k.session.blobs.put(b"operator review contract"),
    )
    return k, p


async def test_resumed_review_keeps_actual_before_and_after_observations(tmp_path):
    from harness.completion import CompletionReview

    work = Work()
    rows = []

    async def reviewer(before, after, outcome):
        rows.append((before.checks[0].summary, after.checks[0].summary, outcome.text))
        return CompletionReview(
            decision="pause" if len(rows) == 1 else "continue",
            reason="operator assessment of retained work",
        )

    args = dict(
        prompt="finish",
        execute=work.execute,
        verify=work.verify,
        identify=work.identify,
        reviewer=reviewer,
    )
    k, p = kernel_and_plan(tmp_path)
    await k.loop.start()
    try:
        state = await CompletionService(k.session).run(p, **args)
        assert state.status == "paused" and work.executions == 1
        root = k.session.id
    finally:
        k.session.close()
    k, p = kernel_and_plan(tmp_path, resume=root)
    try:
        state = await CompletionService(k.session).run(p, **args)
        assert state.status == "checks_passed" and work.executions == 3
        assert rows[0] == ("actual step 0", "actual step 1", "partial 1")
        assert rows[1] == rows[0], "fresh recheck must retain the before-attempt comparison"
        assert rows[2] == ("actual step 1", "actual step 2", "partial 2")
        assert all(r.reason for r in state.history if r.type == "completion_review_recorded")
    finally:
        k.session.close()


@pytest.mark.parametrize("failure", ["block", "invalid", "timeout"])
async def test_review_failure_cannot_launch_another_attempt(tmp_path, failure):
    from harness.completion import CompletionReview

    work = Work()

    async def reviewer(*args):
        if failure == "timeout":
            raise TimeoutError("progress reviewer unavailable")
        if failure == "invalid":
            return {"decision": "accept", "reason": "model claims done"}
        return CompletionReview(decision="block", reason="real unresolved blocker")

    k, p = kernel_and_plan(tmp_path)
    await k.loop.start()
    try:
        state = await CompletionService(k.session).run(
            p,
            prompt="finish",
            execute=work.execute,
            verify=work.verify,
            identify=work.identify,
            reviewer=reviewer,
        )
        assert state.status == "blocked" and work.executions == 1
        assert (
            await CompletionService(k.session).run(
                p,
                prompt="finish",
                execute=work.execute,
                verify=work.verify,
                identify=work.identify,
                reviewer=reviewer,
            )
        ).status == "blocked"
        assert work.executions == 1
    finally:
        k.session.close()


async def test_verifier_timeout_without_overall_deadline_is_not_a_deadline_stop(tmp_path):
    k, p = kernel_and_plan(tmp_path)
    await k.loop.start()
    work = Work()

    async def verify():
        raise TimeoutError("independent checker unavailable")

    try:
        try:
            await CompletionService(k.session).run(
                p, prompt="finish", execute=work.execute, verify=verify, identify=work.identify
            )
        except TimeoutError:
            pass
        state = CompletionService(k.session).state()
        assert state.status == "blocked"
        assert work.executions == 0
    finally:
        k.session.close()


async def test_explicit_attempt_limit_cannot_be_turned_into_a_resumable_pause(tmp_path):
    from harness.completion import CompletionReview

    k, p = kernel_and_plan(tmp_path)
    p = p.model_copy(update={"max_attempts": 1})
    await k.loop.start()
    work = Work()
    reviews = []

    async def reviewer(*args):
        reviews.append(args)
        return CompletionReview(decision="pause", reason="try again later")

    try:
        state = await CompletionService(k.session).run(
            p,
            prompt="finish",
            execute=work.execute,
            verify=work.verify,
            identify=work.identify,
            reviewer=reviewer,
        )
        assert state.status == "exhausted"
        assert work.executions == 1 and not reviews
    finally:
        k.session.close()
