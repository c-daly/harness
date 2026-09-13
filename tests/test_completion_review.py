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
    CompletionReview,
    CompletionService,
)
from harness.events import CompletionReviewRecorded
from harness.log import read_session
from harness.execution import ExecutionLimits
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
        task="reviewed-work",
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


async def test_review_continue_records_and_allows_progress(tmp_path):
    # First attempt claims done, second writes the file to pass
    kernel, provider, workspace = setup(
        tmp_path,
        [
            text_turn("I'm done; not really"),
            tool_call_turn("", "write_file", {"file_path": "result.txt", "content": "ready"}),
            text_turn("implemented"),
        ],
    )
    await kernel.loop.start()
    try:
        plan, callbacks = binding(kernel, workspace)
        seen = {}

        async def reviewer(prev_obs, curr_obs, outcome):
            # Should only be called between settled attempts; previous must exist
            assert prev_obs is not None and outcome is not None
            seen["prev"] = prev_obs
            seen["curr"] = curr_obs
            seen["outcome_status"] = outcome.status
            return CompletionReview(decision="continue", reason="still unresolved")

        state = await CompletionService(kernel.session).run(
            plan, reviewer=reviewer, **callbacks
        )
        assert state.status == "checks_passed" and state.attempts == 2
        # A review event must be recorded for the first settled attempt
        events = [e.event for e in read_session(kernel.session.base, kernel.session.id)]
        reviews = [e for e in events if isinstance(e, CompletionReviewRecorded)]
        assert len(reviews) == 1 and reviews[0].decision == "continue" and reviews[0].reason
        # Ensure reviewer saw real observations and outcome
        assert seen["outcome_status"] == "completed"
        assert isinstance(seen["prev"], CompletionObservation) and isinstance(
            seen["curr"], CompletionObservation
        )
    finally:
        kernel.session.close()


async def test_review_pause_and_resume_requires_fresh_check(tmp_path):
    kernel, provider, workspace = setup(
        tmp_path,
        [
            text_turn("not yet"),
            text_turn("still not"),
            tool_call_turn("", "write_file", {"file_path": "result.txt", "content": "ready"}),
            text_turn("done"),
        ],
    )
    await kernel.loop.start()
    try:
        plan, callbacks = binding(kernel, workspace)
        calls = {"count": 0}

        async def reviewer(prev_obs, curr_obs, outcome):
            calls["count"] += 1
            if calls["count"] == 1:
                # Pause after first attempt
                return CompletionReview(decision="pause", reason="checkpoint")
            return CompletionReview(decision="continue", reason="resume ok")

        service = CompletionService(kernel.session)
        paused = await service.run(plan, reviewer=reviewer, **callbacks)
        assert paused.status == "paused"
        # Resume must recheck before continuing (no extra attempt yet)
        resumed = await service.run(plan, reviewer=reviewer, **callbacks)
        assert resumed.status == "checks_passed" and resumed.attempts == 3
        # Reviews occur after each unresolved settled attempt: pause, then continue(s) until resolved
        events = [e.event for e in read_session(kernel.session.base, kernel.session.id)]
        reviews = [e for e in events if isinstance(e, CompletionReviewRecorded)]
        assert [r.decision for r in reviews] == ["pause", "continue", "continue"]
    finally:
        kernel.session.close()


async def test_review_invalid_or_error_blocks_without_new_work(tmp_path):
    kernel, provider, workspace = setup(tmp_path, [text_turn("no change")])
    await kernel.loop.start()
    try:
        plan, callbacks = binding(kernel, workspace)
        started = asyncio.Event()

        async def execute(prompt):
            started.set()
            return DelegationResult(status="completed", text="partial")

        # Invalid decision
        async def bad_reviewer(prev_obs, curr_obs, outcome):
            return CompletionReview(decision="bad", reason="oops")  # type: ignore[arg-type]

        service = CompletionService(kernel.session)
        state = await service.run(plan, reviewer=bad_reviewer, **(callbacks | {"execute": execute}))
        assert state.status == "blocked" and "invalid" in state.reason
        # Error in reviewer must block independently (use a fresh session)
        async def exploding(prev_obs, curr_obs, outcome):
            raise RuntimeError("review failed")

        kernel2, provider2, workspace2 = setup(tmp_path, [text_turn("no change")])
        await kernel2.loop.start()
        try:
            plan2, callbacks2 = binding(kernel2, workspace2)
            service2 = CompletionService(kernel2.session)
            state2 = await service2.run(plan2, reviewer=exploding, **(callbacks2 | {"execute": execute}))
            assert state2.status == "blocked" and "review failed" in state2.reason
            # No further attempts started in either case (only the first baseline check ran)
            assert provider.calls == [] and provider2.calls == []
        finally:
            kernel2.session.close()
    finally:
        kernel.session.close()


def test_review_event_ordering_and_projection_rules(tmp_path):
    # Manually construct illegal ordering: review without a fresh check
    from harness.events import (
        CompletionAttemptFinished,
        CompletionAttemptStarted,
        CompletionConfigured,
        CompletionStopped,
    )
    from harness.log import EventLogWriter, read_session
    from harness.session import Session

    base = tmp_path / "journal"
    sid = ModelId("s1")  # reuse ModelId for test identity convenience
    with EventLogWriter(base, sid) as w:  # type: ignore[arg-type]
        sess = Session(base, sid, _writer=w)  # type: ignore[arg-type]
        plan = CompletionPlan(
            task="t",
            objective="o",
            configuration=sess.blobs.put(b"cfg"),
            checks=(CompletionCheck(id="c", description="d"),),
        )
        sess.append(CompletionConfigured(plan=plan, deadline=None, workspace_sha256="0" * 64))
        sess.append(CompletionAttemptStarted(attempt=1))
        sess.append(
            CompletionAttemptFinished(
                attempt=1,
                outcome=DelegationResult(status="completed", text="ok"),
                workspace_sha256="0" * 64,
            )
        )
        # Insert review without a fresh CompletionChecked
        sess.append(CompletionReviewRecorded(attempt=1, decision="continue", reason="r"))
        sess.append(CompletionStopped(status="blocked", reason="x"))
    from harness.completion import project_completion

    with pytest.raises(ValueError):
        project_completion(read_session(base, sess.id))
