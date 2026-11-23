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
        observed = {e.source_seq: e.observation for e in state.evidence.values()}
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
