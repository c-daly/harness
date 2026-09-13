"""Completion is established by host checks across real native child attempts."""

import asyncio
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
from harness.events import CompletionAttemptStarted, CompletionChecked, TaskAccepted
from harness.execution import ExecutionLimits
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId


def setup(tmp_path, turns=(), *, max_calls=20, resume=None):
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    provider = FakeProvider(list(turns))
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path / "journal",
        model=ModelId("fake"),
        resume_session_id=resume,
        native_tools=True,
        workspace_root=workspace,
        permissions=PermissionEngine([RuleSet(rules=[PermissionRule("allow", tool="*")])]),
        execution_limits=None if resume else ExecutionLimits(max_model_calls=max_calls),
    )
    return kernel, provider, workspace


def binding(kernel, workspace, **overrides):
    plan = CompletionPlan(
        task="actual-work",
        objective="Create the useful result",
        configuration=kernel.session.blobs.put(b"operator-owned grading controls"),
        checks=(CompletionCheck(id="result", description="result.txt contains ready"),),
        **overrides,
    )

    async def identify():
        path = workspace / "result.txt"
        return hashlib.sha256(path.read_bytes() if path.exists() else b"missing").hexdigest()

    async def verify():
        path = workspace / "result.txt"
        passed = path.exists() and path.read_text() == "ready"
        return CompletionObservation(
            workspace_sha256=await identify(),
            checks=(
                CheckObservation(
                    id="result",
                    status="passed" if passed else "failed",
                    summary="result is ready" if passed else "result is still unfinished",
                ),
            ),
        )

    async def execute(prompt):
        return await kernel.runner.run_result(
            prompt=prompt, model=ModelId("fake"), parent=kernel.session
        )

    return plan, dict(
        prompt="Complete the useful result", execute=execute, verify=verify, identify=identify
    )


async def test_partial_final_is_checked_and_continued_without_operator_restart(tmp_path):
    kernel, provider, workspace = setup(
        tmp_path,
        [
            text_turn("I'm done; ask me to continue if you want more"),
            tool_call_turn("", "write_file", {"file_path": "result.txt", "content": "ready"}),
            text_turn("implemented"),
        ],
    )
    await kernel.loop.start()
    try:
        plan, callbacks = binding(kernel, workspace)
        state = await CompletionService(kernel.session).run(plan, **callbacks)
        assert state.status == "checks_passed" and state.attempts == 2
        assert "result is still unfinished" in "\n".join(m.text() for m in provider.calls[1])
        events = read_session(kernel.session.base, kernel.session.id)
        grades = [
            e.event.observation.checks[0].status
            for e in events
            if isinstance(e.event, CompletionChecked)
        ]
        assert grades == ["failed", "failed", "passed"]
        assert not any(isinstance(e.event, TaskAccepted) for e in events)
        assert kernel.runner.scope_for(kernel.session).budget.model_calls == 3
    finally:
        kernel.session.close()


async def test_resume_retains_work_attempts_and_cumulative_budget(tmp_path):
    kernel, _, workspace = setup(
        tmp_path,
        [
            tool_call_turn("", "write_file", {"file_path": "result.txt", "content": "partial"}),
            text_turn("partial work"),
        ],
        max_calls=3,
    )
    await kernel.loop.start()
    plan, callbacks = binding(kernel, workspace)
    state = await CompletionService(kernel.session).run(plan, **callbacks, pause_after=1)
    assert state.status == "paused" and workspace.joinpath("result.txt").read_text() == "partial"
    root_id = kernel.session.id
    kernel.session.close()
    kernel, provider, workspace = setup(tmp_path, [text_turn("still partial")], resume=root_id)
    try:
        new_plan, callbacks = binding(kernel, workspace)
        assert new_plan == plan
        state = await CompletionService(kernel.session).run(plan, **callbacks)
        assert state.status == "blocked" and "budget" in state.reason
        assert state.attempts == 3 and len(provider.calls) == 1
        assert kernel.runner.scope_for(kernel.session).budget.model_calls == 3
        assert workspace.joinpath("result.txt").read_text() == "partial"
    finally:
        kernel.session.close()


async def test_exhaustion_is_retained_and_false_success_does_not_accept(tmp_path):
    kernel, provider, workspace = setup(tmp_path, [text_turn("passed all tests")])
    await kernel.loop.start()
    try:
        plan, callbacks = binding(kernel, workspace, max_attempts=1)
        service = CompletionService(kernel.session)
        state = await service.run(plan, **callbacks)
        assert state.status == "exhausted" and state.attempts == 1
        assert (await service.run(plan, **callbacks)).status == "exhausted"
        assert len(provider.calls) == 1
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mutation", ["workspace", "plan", "inflight"])
async def test_resume_refuses_unreconciled_work_or_changed_contract(tmp_path, mutation):
    kernel, provider, workspace = setup(tmp_path)
    await kernel.loop.start()
    plan, callbacks = binding(kernel, workspace)
    service = CompletionService(kernel.session)
    try:

        async def partial(prompt):
            return DelegationResult(status="completed", text="partial")

        await service.run(plan, **(callbacks | {"execute": partial}), pause_after=1)
        if mutation == "workspace":
            workspace.joinpath("result.txt").write_text("unrecorded external edit")
            state = await service.run(plan, **callbacks)
            assert state.status == "blocked" and "workspace changed" in state.reason
        elif mutation == "plan":
            with pytest.raises(ValueError, match="configuration changed"):
                await service.run(plan.model_copy(update={"max_attempts": 20}), **callbacks)
        else:
            kernel.session.append(CompletionAttemptStarted(attempt=2))
            with pytest.raises(ValueError, match="effect reconciliation"):
                await service.run(plan, **callbacks)
        assert not provider.calls
    finally:
        kernel.session.close()


@pytest.mark.parametrize("failure", ["omitted", "duplicate", "error", "mutation"])
async def test_verifier_failure_or_artifact_change_never_becomes_success(tmp_path, failure):
    kernel, provider, workspace = setup(tmp_path)
    await kernel.loop.start()
    plan, callbacks = binding(kernel, workspace)
    try:

        async def verify():
            digest = await callbacks["identify"]()
            check = CheckObservation(
                id="result", status="error" if failure == "error" else "passed", summary="x"
            )
            checks = (
                ()
                if failure == "omitted"
                else (check, check)
                if failure == "duplicate"
                else (check,)
            )
            if failure == "mutation":
                workspace.joinpath("result.txt").write_text("changed during check")
            return CompletionObservation(workspace_sha256=digest, checks=checks)

        service = CompletionService(kernel.session)
        if failure in {"omitted", "duplicate"}:
            with pytest.raises(ValueError, match="every declared check"):
                await service.run(plan, **(callbacks | {"verify": verify}))
        else:
            await service.run(plan, **(callbacks | {"verify": verify}))
        assert service.state().status == "blocked"
        assert not provider.calls
    finally:
        kernel.session.close()


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_interrupt_verification_records_stop_without_retry(tmp_path, interrupt):
    kernel, provider, workspace = setup(tmp_path)
    await kernel.loop.start()
    plan, callbacks = binding(
        kernel, workspace, timeout_seconds=0.2 if interrupt == "timeout" else 30
    )
    entered, cleaned = asyncio.Event(), asyncio.Event()
    try:

        async def verify():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        service = CompletionService(kernel.session)
        running = asyncio.create_task(service.run(plan, **(callbacks | {"verify": verify})))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if interrupt == "cancel":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            await running
        assert cleaned.is_set() and not provider.calls
        assert service.state().status == ("cancelled" if interrupt == "cancel" else "timed_out")
    finally:
        kernel.session.close()


async def test_two_host_invocations_cannot_own_the_same_completion(tmp_path):
    kernel, _, workspace = setup(tmp_path)
    await kernel.loop.start()
    plan, callbacks = binding(kernel, workspace)
    entered = asyncio.Event()

    async def identify():
        entered.set()
        await asyncio.Event().wait()

    service = CompletionService(kernel.session)
    first = asyncio.create_task(service.run(plan, **(callbacks | {"identify": identify})))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(ValueError, match="already active"):
            await asyncio.wait_for(
                CompletionService(kernel.session).run(plan, **callbacks),
                timeout=1,
            )
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        kernel.session.close()
