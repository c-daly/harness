import pytest

from harness.agent import AgentResult
from harness.events import (
    AgentRunFinished, AgentRunStarted, ImprovementRecorded, SessionOutcome, TaskAccepted, TaskChecked,
    TaskOutcome,
)
from harness.improvement import Candidate, Evidence, ImprovementState
from harness.improvement_journal import ImprovementJournal, read_improvements
from harness.log import read_session
from harness.session import Session
from harness.tasks import TaskService
from harness.types import SessionId


async def test_shadow_correction_cannot_be_rebound_to_an_identical_earlier_exchange(tmp_path):
    from harness.events import SemanticObserved
    from harness.improvement_journal import project_improvements
    from tests.test_ordinary_reply_semantics import ReplyProvider, ready_kernel

    kernel = await ready_kernel(tmp_path, ReplyProvider(answer="That is the recorded assignment.", label="correction"))
    await kernel.loop.start()
    try:
        for _ in range(3):
            await kernel.loop.run_turn("The assignment is different.")
        rows = read_session(tmp_path, kernel.session.id, repair=False)
        first, second = [row for row in rows if isinstance(row.event, SemanticObserved)]
        assert first.event.observation.input_sha256 == second.event.observation.input_sha256
        assert first.event.observation.reply_origin != second.event.observation.reply_origin
        evidence = project_improvements(rows, blobs=kernel.session.blobs).evidence.values()
        assert {e.source_seq for e in evidence if e.category == "correction"} == {first.seq, second.seq}
        rebound = second.event.observation.model_copy(update={"reply_origin": first.event.observation.reply_origin})
        changed = [row.model_copy(update={"event": SemanticObserved(observation=rebound)})
                   if row.seq == second.seq else row for row in rows]
        evidence = project_improvements(changed, blobs=kernel.session.blobs).evidence.values()
        assert {e.source_seq for e in evidence if e.category == "correction"} == {first.seq}
    finally:
        await kernel.resources.close(emit=kernel.session.append)
        kernel.session.close()


def test_historical_evidence_links_marked_outcomes_checks_and_acceptance(tmp_path):
    with Session(tmp_path, SessionId("ordinary")) as session:
        session.start()
        tasks = TaskService(session)
        task = tasks.create("Find the assignment")
        required = session.blobs.put(b"East")
        tasks.add_requirement({"id": "answer", "description": "The current assignment",
                               "check": {"kind": "output", "sha256": required.sha256}})
        tasks.check()
        pending = session.last_seq
        finishes = []
        checks = []
        for run, output in [("first", b"West"), ("second", b"East")]:
            session.append(AgentRunStarted(task_id=task.id, run_id=run, runtime="harness"))
            finishes.append(session.append(AgentRunFinished(result=AgentResult(
                task_id=task.id, run_id=run, status="completed", output=session.blobs.put(output)))).seq)
            tasks.check()
            checks.append(session.last_seq)
        tasks.accept("Inspected the assignment")
        accepted = session.last_seq
        session.append(TaskChecked(task_id=task.id, basis_seq=1, evidence=tuple(tasks.selected().evidence.values())))
        session.append(TaskAccepted(task_id=task.id, basis_seq=1, note="stale"))
        marked = session.append(TaskOutcome(status="fail", score=0)).seq
        abandoned = session.append(SessionOutcome(status="abandoned")).seq
        before = session.last_seq
        state = read_improvements(session.base, session.id)
        observed = {e.source_seq: e.observation for e in state.evidence.values() if e.category == "task_outcome"}
        assert list(observed) == [pending, finishes[0], checks[0], finishes[1], checks[1], accepted - 1,
                                  accepted, marked, abandoned]
        assert "1 unverified" in observed[pending]
        assert "1 failed" in observed[checks[0]] and "1 passed" in observed[checks[1]]
        assert "acceptance unverified" in observed[finishes[1]]
        assert "explicitly accepted" in observed[accepted]
        assert "marked fail" in observed[marked] and "score was 0.0" in observed[marked]
        assert "marked abandoned" in observed[abandoned]
        assert all(item.source_session == session.id for item in state.evidence.values())
        assert not state.candidates and session.last_seq == before
    assert read_improvements(tmp_path, session.id).evidence == state.evidence


def test_nested_author_and_unmatched_finishes_cannot_supply_primary_outcomes(tmp_path):
    with Session(tmp_path, SessionId("parent")) as session:
        session.start()
        for run, parent, runtime in [("nested", "parent", "harness"), ("author", None, "resident-source-author")]:
            session.append(AgentRunStarted(task_id=run, run_id=run, parent_run_id=parent, runtime=runtime))
            session.append(AgentRunFinished(result=AgentResult(task_id=run, run_id=run, status="completed")))
        session.append(AgentRunFinished(result=AgentResult(task_id="unseen", run_id="unseen", status="failed")))
        assert not read_improvements(tmp_path, session.id).evidence
    with Session(tmp_path, SessionId("child"), parent=(session.id, 1)) as child:
        child.start()
        child.append(AgentRunStarted(task_id="nested", run_id="run", runtime="harness"))
        child.append(AgentRunFinished(result=AgentResult(task_id="nested", run_id="run", status="completed")))
        assert not read_improvements(tmp_path, child.id).evidence


def test_candidate_materializes_observation_once_even_after_interrupted_publication(tmp_path, monkeypatch):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        marked = session.append(SessionOutcome(status="ok")).seq
        old_id = f"session_outcome-{marked}"
        legacy = Evidence(id=old_id, source_session=session.id, source_seq=1, category="correction",
                          observation="Older clients allowed this ID")
        session.append(ImprovementRecorded(record=legacy))
        journal = ImprovementJournal(session)
        key, = journal.state.derived_evidence_ids
        assert key != old_id
        candidate = Candidate(id="candidate", target="prompt", incumbent_version="a" * 64,
                              artifact=session.blobs.put(b"candidate"), evidence_ids=(key,),
                              hypothesis="Reuse an observed success", expected_benefit="Preserve a passing case")
        publish = session.append

        def interrupt(event):
            if isinstance(event, ImprovementRecorded) and isinstance(event.record, Candidate):
                raise OSError("publication interrupted")
            return publish(event)

        with monkeypatch.context() as scope:
            scope.setattr(session, "append", interrupt)
            with pytest.raises(OSError, match="interrupted"):
                journal.record(candidate)
        journal.record(candidate)
        stored = [e.event.record for e in read_session(session.base, session.id)
                  if isinstance(e.event, ImprovementRecorded)]
        assert [r.id for r in stored] == [old_id, key, candidate.id]
        legacy_reader = ImprovementState()
        for record in stored:
            legacy_reader.apply(record)
        assert legacy_reader.evidence == journal.state.evidence
        assert not read_improvements(tmp_path, session.id).derived_evidence_ids


def test_read_only_projection_cannot_validate_a_preexisting_candidate_missing_its_evidence(tmp_path):
    with Session(tmp_path, SessionId("incomplete")) as session:
        session.start()
        marked = session.append(SessionOutcome(status="ok")).seq
        candidate = Candidate(id="candidate", target="prompt", incumbent_version="a" * 64,
                              artifact=session.blobs.put(b"candidate"), evidence_ids=(f"session_outcome-{marked}",),
                              hypothesis="Try", expected_benefit="Check")
        session.append(ImprovementRecorded(record=candidate))
        with pytest.raises(ValueError, match="missing evidence"):
            read_improvements(tmp_path, session.id)


def test_wrong_verified_response_produces_one_citable_failure_for_its_check(tmp_path):
    from harness.improvement_outcomes import checked_failure_turn

    with Session(tmp_path, SessionId("mismatch")) as session:
        session.start()
        tasks = TaskService(session)
        task = tasks.create("Report the current assignment")
        expected = session.blobs.put(b"east")
        actual = session.blobs.put(b"west")
        tasks.add_requirement({"id": "destination", "description": "Return the verified assignment",
                               "check": {"kind": "output", "sha256": expected.sha256}})
        session.append(AgentRunStarted(task_id=task.id, run_id="attempt", runtime="harness"))
        finish = session.append(AgentRunFinished(result=AgentResult(
            task_id=task.id, run_id="attempt", status="completed", output=actual)))
        tasks.check()
        check_seq = session.last_seq
        journal = ImprovementJournal(session)
        failure, = [item for item in journal.state.evidence.values() if item.category == "failure"]
        assert failure.source_seq == check_seq and failure.source_session == session.id
        assert checked_failure_turn(failure, read_session(tmp_path, session.id), session.blobs) == finish.seq
        assert expected.sha256 in failure.observation and actual.sha256 in failure.observation
        assert b"east" not in failure.observation.encode()
        journal.record(Candidate(id="use-check", target="prompt", incumbent_version="a"*64,
                                 artifact=session.blobs.put(b"candidate"), evidence_ids=(failure.id,),
                                 hypothesis="A fresh check catches stale answers", expected_benefit="Avoid a known mismatch"))
        rows = read_session(tmp_path, session.id)
        assert [row.event.record.id for row in rows if isinstance(row.event, ImprovementRecorded)] == [failure.id, "use-check"]
        assert failure.id not in read_improvements(tmp_path, session.id).derived_evidence_ids
        # A cited historical finding persists. Present-day review must still verify its source.
        (session.blobs._root / actual.sha256).unlink()
        assert read_improvements(tmp_path, session.id).evidence[failure.id] == failure
        assert checked_failure_turn(failure, rows, session.blobs) is None


@pytest.mark.parametrize("situation", ["match", "late", "generic-failure", "missing", "damaged", "tool-error"])
def test_unverified_output_or_unrelated_failure_does_not_establish_a_flawed_response(tmp_path, situation):
    from harness.events import DispatchResolved, ToolCallCompleted, ToolCallProposed

    with Session(tmp_path, SessionId("other")) as session:
        session.start()
        tasks = TaskService(session)
        task = tasks.create("Report the current assignment")
        expected = session.blobs.put(b"east")
        req = {"id": "destination", "description": "Verify the assignment",
               "check": {"kind": "output", "sha256": expected.sha256}}
        if situation == "tool-error":
            req["check"].update(kind="tool_result", tool="read_file", args={"file_path":"current"})
        if situation != "late":
            tasks.add_requirement(req)
        session.append(AgentRunStarted(task_id=task.id, run_id="attempt", runtime="harness"))
        if situation == "tool-error":
            session.append(ToolCallProposed(call_id="lookup", tool="read_file", args={"file_path":"current"},
                                            task_id=task.id, agent_run_id="attempt"))
            session.append(DispatchResolved(call_id="lookup", kind="tool", tool="read_file", args={"file_path":"current"}))
            session.append(ToolCallCompleted(call_id="lookup", result_text="Read failed", is_error=True))
        actual = expected if situation == "match" else session.blobs.put(b"wrong")
        session.append(AgentRunFinished(result=AgentResult(
            task_id=task.id, run_id="attempt", status="failed" if situation == "generic-failure" else "completed",
            output=None if situation == "generic-failure" else actual)))
        if situation == "late":
            tasks.add_requirement(req)
        tasks.check()
        if situation == "missing":
            (session.blobs._root / actual.sha256).unlink()
        if situation == "damaged":
            (session.blobs._root / actual.sha256).write_bytes(b"broken")
        assert not [item for item in read_improvements(tmp_path, session.id).evidence.values() if item.category == "failure"]
