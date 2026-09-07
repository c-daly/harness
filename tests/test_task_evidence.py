"""User obligations survive model claims, new attempts, and replay."""

import asyncio
import hashlib

import pytest

from harness.agent import AgentResult
from harness.cli import build_kernel
from harness.events import (
    AgentRunFinished, AgentRunStarted, CompactionApplied, DispatchResolved,
    ToolCallCompleted, ToolCallProposed, TodoListUpdated,
    TaskOutcome,
)
from harness.fold import fold
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.types import CallId, ModelId, ToolName


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
async def kernel(tmp_path):
    kernel = build_kernel(base_dir=tmp_path, provider=FakeProvider([
        text_turn("done"), text_turn("changed"), text_turn("done"),
    ]), model=ModelId("fake"))
    await kernel.loop.start()
    try:
        yield kernel
    finally:
        kernel.session.close()


def add_review(service, identity="review"):
    service.add_requirement({"id": identity, "description": "Review the result"})


def add_output(service):
    service.add_requirement({"id": "output", "description": "Exact expected answer",
                             "check": {"kind": "output", "sha256": digest("done")}})


def add_tool(service, **updates):
    check = {"kind": "tool_result", "tool": "verify", "args": {"target": "fixture"},
             "sha256": digest("PASS")}
    check.update(updates)
    service.add_requirement({"id": "test", "description": "Recorded fixture check",
                             "check": check})


def start(kernel, task_id, run_id="run", parent=None):
    kernel.session.append(AgentRunStarted(task_id=task_id, run_id=run_id,
                                         runtime="test", parent_run_id=parent))


def finish(kernel, task_id, run_id="run", status="completed"):
    kernel.session.append(AgentRunFinished(result=AgentResult(
        task_id=task_id, run_id=run_id, status=status,
        output=kernel.session.blobs.put(b"done"))))


def tool(kernel, call="call", run_id="run", *, result="PASS", error=False,
         resolved=True, args=None, blob=False):
    args = {"target": "fixture"} if args is None else args
    kernel.session.append(ToolCallProposed(call_id=CallId(call), tool=ToolName("verify"),
                                          args={"target": "original"}, agent_run_id=run_id))
    if resolved:
        kernel.session.append(DispatchResolved(call_id=CallId(call), kind="tool",
                                              tool=ToolName("verify"), args=args))
    ref = kernel.session.blobs.put(result.encode()) if blob else None
    kernel.session.append(ToolCallCompleted(call_id=CallId(call),
        result_text=None if blob else result, result_blob=ref, is_error=error))
    return ref


async def test_model_claim_and_todo_do_not_resolve_user_requirement(kernel):
    service = kernel.tasks
    task = service.create("Implement and verify the change")
    add_review(service)
    result = await kernel.loop.run_task(service.prepare("Do it"))
    kernel.session.append(TodoListUpdated(items=[{"content": "Review", "status": "completed"}]))
    kernel.session.append(TaskOutcome(status="ok", judge="model", score=1))
    state = service.check()
    assert result.task_id == task.id and result.status == "completed"
    assert result.acceptance == "unverified"
    assert state.unresolved == ("review",) and not state.accepted
    with pytest.raises(ValueError, match="unresolved"):
        service.accept("model says done")


async def test_exact_artifact_plus_explicit_review_and_acceptance_survive_resume(kernel):
    service = kernel.tasks
    task = service.create("Produce the expected result")
    add_output(service)
    add_review(service)
    await kernel.loop.run_task(service.prepare("Do it"))
    state = service.check()
    assert state.evidence["output"].status == "passed"
    assert state.evidence["output"].artifact.sha256 == digest("done")
    service.confirm("review", "I inspected the result")
    service.accept("Accepted after checking the fixture")
    assert service.selected().accepted
    kernel.session.append(CompactionApplied(from_seq=1, to_seq=999, summary="done"))
    before = fold(read_session(kernel.session.base, kernel.session.id)).tasks
    kernel.session.close()
    resumed = build_kernel(base_dir=kernel.session.base, provider=FakeProvider([]),
        model=ModelId("other"), resume_session_id=kernel.session.id)
    try:
        assert resumed.tasks.selected().accepted
        assert resumed.tasks.selected().definition.id == task.id
        assert fold(read_session(kernel.session.base, kernel.session.id)).tasks == before
    finally:
        resumed.session.close()


async def test_new_attempt_invalidates_checks_and_manual_acceptance(kernel):
    service = kernel.tasks
    service.create("Produce result")
    add_output(service)
    add_review(service)
    await kernel.loop.run_task(service.prepare("first"))
    service.check()
    service.confirm("review", "reviewed")
    service.accept("accepted")
    await kernel.loop.run_task(service.prepare("change it"))
    stale = service.selected()
    assert not stale.accepted and set(stale.unresolved) == {"review", "output"}
    current = service.check()
    assert current.evidence["output"].status == "failed"
    assert current.unresolved == ("output", "review")


@pytest.mark.parametrize("mode", ["inline", "blob", "rewrite", "error", "blocked", "later_failure", "other_run"])
async def test_tool_evidence_uses_effective_args_terminal_result_and_current_run(kernel, mode):
    service = kernel.tasks
    task = service.create("Verify fixture")
    add_tool(service)
    start(kernel, task.id)
    tool(kernel, blob=mode == "blob", error=mode == "error", resolved=mode != "blocked",
         args={"target": "wrong"} if mode == "rewrite" else None)
    if mode == "later_failure":
        tool(kernel, call="later", result="FAIL", error=True)
    finish(kernel, task.id)
    if mode == "other_run":
        start(kernel, task.id, "next")
        finish(kernel, task.id, "next")
    state = service.check()
    assert (not state.unresolved) == (mode in {"inline", "blob"})
    assert not state.accepted  # passing checks never infer user acceptance


async def test_nested_runtime_evidence_is_scoped_to_its_root_attempt(kernel):
    service = kernel.tasks
    task = service.create("Nested check")
    add_tool(service)
    start(kernel, task.id)
    start(kernel, "child-task", "child", "run")
    tool(kernel, run_id="child")
    finish(kernel, "child-task", "child")
    finish(kernel, task.id)
    assert not service.check().unresolved


@pytest.mark.parametrize("damage", ["missing", "corrupt", "oversize"])
async def test_artifact_failure_is_unverified_and_never_accepted(kernel, damage):
    service = kernel.tasks
    task = service.create("Verify stored evidence")
    add_tool(service)
    start(kernel, task.id)
    ref = tool(kernel, blob=True, result="PASS" if damage != "oversize" else "X" * (1024 * 1024 + 1))
    finish(kernel, task.id)
    path = kernel.session.blobs._root / ref.sha256
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"FAKE")
    state = service.check()
    assert state.evidence["test"].status == "unverified"
    assert state.unresolved == ("test",)


async def test_acceptance_rechecks_artifacts_after_earlier_pass(kernel):
    service = kernel.tasks
    service.create("Exact answer")
    add_output(service)
    await kernel.loop.run_task(service.prepare("do it"))
    assert not service.check().unresolved
    (kernel.session.blobs._root / digest("done")).unlink()
    with pytest.raises(ValueError, match="unresolved"):
        service.accept("accept")
    assert not service.selected().accepted


async def test_empty_requirements_and_incomplete_execution_cannot_be_accepted(kernel):
    service = kernel.tasks
    task = service.create("Define success first")
    with pytest.raises(ValueError, match="requirement"):
        service.accept("accept")
    add_output(service)
    start(kernel, task.id)
    finish(kernel, task.id, status="incomplete")
    service.check()
    with pytest.raises(ValueError, match="completed"):
        service.accept("accept")


async def test_crash_repair_keeps_obligations_and_does_not_repeat_work(kernel):
    service = kernel.tasks
    task = service.create("Interrupted work")
    add_review(service)
    start(kernel, task.id)
    kernel.session.close()
    resumed = build_kernel(base_dir=kernel.session.base, provider=FakeProvider([]),
        model=ModelId("fake"), resume_session_id=kernel.session.id)
    try:
        state = resumed.tasks.selected()
        assert state.execution == "aborted" and state.unresolved == ("review",)
        assert not state.accepted
    finally:
        resumed.session.close()


async def test_busy_mutations_and_forged_requirements_are_rejected(kernel):
    service = kernel.tasks
    task = service.create("Keep the user's requirements")
    add_review(service)
    start(kernel, task.id)
    with pytest.raises(ValueError, match="running"):
        service.add_requirement({"id": "extra", "description": "another"})
    with pytest.raises(ValueError, match="running"):
        service.confirm("review", "accepted while busy")
    finish(kernel, task.id)
    forged = service.prepare("do it").model_copy(update={"acceptance_criteria": ()})
    with pytest.raises(ValueError, match="requirements"):
        await kernel.loop.run_task(forged)


async def test_selection_is_durable_and_can_be_detached_without_erasing_work(kernel):
    service = kernel.tasks
    first = service.create("First")
    add_review(service)
    second = service.create("Second")
    service.select(first.id[:8])
    assert service.prepare("continue").id == first.id
    service.select(None)
    assert service.selected() is None
    assert service.prepare("unrelated").id not in {first.id, second.id}
    assert len(service.state().items) == 2


async def test_cancelled_run_leaves_checks_stale(kernel):
    service = kernel.tasks
    service.create("Cancelable work")
    add_review(service)
    entered = asyncio.Event()

    class Waiting:
        async def infer(self, request):
            entered.set()
            await asyncio.Event().wait()
            yield

    kernel.set_provider(Waiting())
    running = asyncio.create_task(kernel.loop.run_task(service.prepare("wait")))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert service.selected().execution == "cancelled"
    assert service.selected().unresolved == ("review",)


async def test_checks_cannot_reuse_other_tasks_or_unrelated_calls(kernel):
    service = kernel.tasks
    task = service.create("Scoped evidence")
    add_tool(service)
    start(kernel, task.id)
    start(kernel, "unrelated", "other")
    tool(kernel, run_id="other")
    finish(kernel, "unrelated", "other")
    finish(kernel, task.id)
    assert service.check().unresolved == ("test",)


async def test_expectations_are_fixed_before_attempt_and_new_requirements_invalidate_acceptance(kernel):
    service = kernel.tasks
    service.create("Requirements first")
    add_review(service)
    await kernel.loop.run_task(service.prepare("work"))
    service.confirm("review", "reviewed")
    service.accept("accepted")
    add_output(service)
    assert not service.selected().accepted
    assert set(service.selected().unresolved) == {"output", "review"}
    assert service.check().evidence["output"].status == "unverified"
    with pytest.raises(ValueError, match="overwritten"):
        add_output(service)


@pytest.mark.parametrize("args", [{"target": True}, {"target": 1.0}, {"target": 1, "extra": 2}])
async def test_tool_argument_match_is_exact_json_not_python_coercion(kernel, args):
    service = kernel.tasks
    task = service.create("Exact arguments")
    add_tool(service, args={"target": 1})
    start(kernel, task.id)
    tool(kernel, args=args)
    finish(kernel, task.id)
    assert service.check().unresolved == ("test",)


async def test_stale_acceptance_and_confirmation_events_do_not_apply_to_later_attempt(kernel):
    from harness.events import TaskAccepted, TaskRequirementConfirmed
    service = kernel.tasks
    task = service.create("Review applies to one attempt")
    add_review(service)
    await kernel.loop.run_task(service.prepare("first"))
    old_basis = service.selected().basis_seq
    await kernel.loop.run_task(service.prepare("second"))
    kernel.session.append(TaskRequirementConfirmed(task_id=task.id, basis_seq=old_basis,
        requirement_id="review", note="stale review"))
    kernel.session.append(TaskAccepted(task_id=task.id, basis_seq=old_basis, note="stale acceptance"))
    assert service.selected().unresolved == ("review",) and not service.selected().accepted


@pytest.mark.parametrize("requirement", [
    {"id": "a", "description": " "},
    {"id": "bad id", "description": "test"},
    {"id": "a", "description": "test", "check": {"kind": "output", "sha256": "bad"}},
    {"id": "a", "description": "test", "check": {"kind": "tool_result", "tool": "bash", "args": {}}},
    {"id": "a", "description": "test", "check": {"kind": "tool_result", "tool": "bash",
        "args": {"huge": "x" * 8193}, "sha256": "a" * 64}},
])
async def test_invalid_or_unbounded_requirements_never_reach_log(kernel, requirement):
    service = kernel.tasks
    service.create("Bounded requirements")
    before = read_session(kernel.session.base, kernel.session.id)
    with pytest.raises(ValueError):
        service.add_requirement(requirement)
    assert read_session(kernel.session.base, kernel.session.id) == before


async def test_requirement_limit_and_no_overriding_machine_checks(kernel):
    service = kernel.tasks
    service.create("Bounded review")
    add_output(service)
    for index in range(31):
        add_review(service, identity=f"r{index}")
    with pytest.raises(ValueError, match="32"):
        add_review(service, identity="extra")
    with pytest.raises(ValueError, match="cannot be overridden"):
        service.confirm("output", "force pass")


async def test_core_events_round_trip_and_unknown_event_keeps_payload(kernel):
    from harness.events import parse_envelope_line, UnknownEvent
    service = kernel.tasks
    service.create("Event compatibility")
    add_review(service)
    await kernel.loop.run_task(service.prepare("work"))
    service.check()
    service.confirm("review", "reviewed")
    service.accept("accepted")
    events = read_session(kernel.session.base, kernel.session.id)
    for env in events:
        assert parse_envelope_line(env.model_dump_json()) == env
    last = events[-1].model_dump(mode="json")
    last["event"]["type"] = "future_task_event"
    import json
    unknown = parse_envelope_line(json.dumps(last))
    assert isinstance(unknown.event, UnknownEvent)
    assert unknown.event.raw["note"] == "accepted"


async def test_task_cli_inspects_without_writes_and_headless_resume_uses_selected_task(kernel, monkeypatch, capsys):
    from harness.cli import main, run_once
    service = kernel.tasks
    task = service.create("Continue outside the TUI")
    add_review(service)
    before = read_session(kernel.session.base, kernel.session.id)
    monkeypatch.setattr("sys.argv", ["harness", "tasks", str(kernel.session.id), "--base-dir",
                                   str(kernel.session.base), "--task", task.id[:8]])
    main()
    assert "1/1 unresolved" in capsys.readouterr().out
    assert read_session(kernel.session.base, kernel.session.id) == before
    kernel.session.close()
    resumed = build_kernel(base_dir=kernel.session.base, provider=FakeProvider([text_turn("done")]),
        model=ModelId("fake"), resume_session_id=kernel.session.id)
    assert await run_once(resumed, "continue") == "done"
    events = read_session(kernel.session.base, kernel.session.id)
    run, = [e.event for e in events if isinstance(e.event, AgentRunStarted)]
    assert run.task_id == task.id and "Review the result" in " ".join(run.acceptance_criteria)


async def test_warm_task_status_and_prompt_preparation_do_not_reread_history(kernel, monkeypatch):
    service = kernel.tasks
    task = service.create("Keep input responsive")
    add_review(service)
    before = service.selected()

    def no_read(*args, **kwargs):
        raise AssertionError("warm task status and preparation must not reread the full log")

    monkeypatch.setattr("harness.log.read_session", no_read)
    prepared = service.prepare("continue")
    service.validate_run(prepared)
    start(kernel, task.id)
    assert service.selected().execution == "running"
    finish(kernel, task.id)
    assert service.selected().execution == "completed"
    assert before.execution == "not started"  # callers receive independent snapshots


async def test_task_projection_does_not_publish_a_failed_log_write(kernel, monkeypatch):
    from harness.events import TaskRequirementAdded
    from harness.tasks import TaskRequirement
    service = kernel.tasks
    task = service.create("Source of truth first")

    def fail(envelope):
        raise OSError("disk failure")

    monkeypatch.setattr(kernel.session._writer, "append", fail)
    with pytest.raises(OSError):
        kernel.session.append(TaskRequirementAdded(task_id=task.id,
            requirement=TaskRequirement(id="missing", description="must not appear")))
    assert not service.selected().requirements


async def test_task_evidence_can_supply_core_improvement_sources_without_plugins(kernel):
    from harness.improvement import Evidence
    service = kernel.tasks
    service.create("Collect an actual outcome")
    add_output(service)
    await kernel.loop.run_task(service.prepare("first"))
    await kernel.loop.run_task(service.prepare("changed result"))
    assert service.check().unresolved == ("output",)
    source = read_session(kernel.session.base, kernel.session.id)[-1]
    kernel.improvements.record(Evidence(id="failed-output", source_session=kernel.session.id,
        source_seq=source.seq, category="task_outcome", observation="Recorded output did not match the fixture"))
    assert kernel.improvements.state.evidence["failed-output"].source_seq == source.seq
    assert not kernel.improvements.state.candidates


async def test_task_cli_does_not_emit_terminal_control_sequences(kernel, monkeypatch, capsys):
    from harness.cli import main
    kernel.tasks.create("Untrusted \x1b]0;title\x07 \x9b2J")
    monkeypatch.setattr("sys.argv", ["harness", "tasks", str(kernel.session.id), "--base-dir",
                                   str(kernel.session.base)])
    main()
    output = capsys.readouterr().out
    assert "Untrusted" in output
    assert all(ch in "\n\t" or ord(ch) >= 32 and not 127 <= ord(ch) <= 159 for ch in output)


@pytest.mark.parametrize("ambiguity", ["reused_call", "duplicate_result", "after_run"])
async def test_ambiguous_or_out_of_run_tool_evidence_cannot_certify_requirement(kernel, ambiguity):
    service = kernel.tasks
    task = service.create("Unambiguous execution evidence")
    add_tool(service)
    start(kernel, task.id)
    if ambiguity == "after_run":
        finish(kernel, task.id)
        tool(kernel)
    else:
        tool(kernel, result="FAIL")
        if ambiguity == "reused_call":
            tool(kernel, result="PASS")
        else:
            kernel.session.append(ToolCallCompleted(call_id=CallId("call"), result_text="PASS"))
        finish(kernel, task.id)
    assert service.check().evidence["test"].status == "unverified"


async def test_duplicate_run_terminal_is_not_valid_output_evidence(kernel):
    service = kernel.tasks
    task = service.create("One run has one outcome")
    add_output(service)
    start(kernel, task.id)
    finish(kernel, task.id, status="failed")
    finish(kernel, task.id)
    assert service.check().evidence["output"].status == "unverified"
