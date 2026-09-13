"""Operator checks for the declared continuation contract; no model calls."""

import hashlib

import pytest
from pydantic import ValidationError

from harness.agent import DelegationResult
from harness.cli import build_kernel
from harness.completion import (
    CheckObservation,
    CompletionCheck,
    CompletionObservation,
    CompletionPlan,
    CompletionService,
    project_completion,
)
from harness.events import CompletionConfigured, Envelope
from harness.provider import FakeProvider
from harness.types import ModelId


def plan_for(kernel, **kwargs):
    return CompletionPlan(
        task="operator-contract",
        objective="finish the useful task",
        checks=(CompletionCheck(id="ready", description="the required result is ready"),),
        configuration=kernel.session.blobs.put(b"operator contract v1"),
        **kwargs,
    )


async def test_default_supervisor_continues_beyond_four_attempts(tmp_path):
    kernel = build_kernel(provider=FakeProvider([]), base_dir=tmp_path, model=ModelId("fake"))
    await kernel.loop.start()
    step = 0

    def digest():
        return hashlib.sha256(str(step).encode()).hexdigest()

    async def identify():
        return digest()

    async def verify():
        return CompletionObservation(
            workspace_sha256=digest(),
            checks=(
                CheckObservation(
                    id="ready",
                    status="passed" if step == 6 else "failed",
                    summary=f"actual step {step}",
                ),
            ),
        )

    async def execute(prompt):
        nonlocal step
        step += 1
        return DelegationResult(status="completed", text="partial result retained")

    try:
        state = await CompletionService(kernel.session).run(
            plan_for(kernel), prompt="finish", execute=execute, verify=verify, identify=identify
        )
        assert state.status == "checks_passed"
        assert step == 6
        assert state.deadline is None
    finally:
        kernel.session.close()


def test_explicit_deadline_cannot_disappear_from_a_recorded_finite_plan(tmp_path):
    kernel = build_kernel(provider=FakeProvider([]), base_dir=tmp_path, model=ModelId("fake"))
    try:
        finite = plan_for(kernel, timeout_seconds=30)
        with pytest.raises((ValidationError, ValueError)):
            event = CompletionConfigured(plan=finite, deadline=None, workspace_sha256="0" * 64)
            # Projection is permitted to reject inconsistent records even if the
            # additive event parser accepts the new optional field.
            env = Envelope(session_id=kernel.session.id, seq=1, ts=1.0, event=event)
            project_completion([env])
    finally:
        kernel.session.close()


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf")])
def test_supplied_recorded_deadlines_stay_positive_and_finite(tmp_path, deadline):
    kernel = build_kernel(provider=FakeProvider([]), base_dir=tmp_path, model=ModelId("fake"))
    try:
        with pytest.raises((ValidationError, ValueError)):
            CompletionConfigured(
                plan=plan_for(kernel, timeout_seconds=30),
                deadline=deadline,
                workspace_sha256="0" * 64,
            )
    finally:
        kernel.session.close()
