"""Operator judgements reference the original turn without rerunning inference."""

import pytest

from harness.agent import AgentResult
from harness.cli import _subcommand
from harness.events import AgentRunFinished, AgentRunStarted, ImprovementRecorded
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.session import Session
from harness.types import SessionId


def reply(session, name, *, runtime="harness", parent=None):
    session.append(AgentRunStarted(task_id=name, run_id=name, runtime=runtime, parent_run_id=parent))
    return session.append(AgentRunFinished(result=AgentResult(task_id=name, run_id=name, status="completed"))).seq


def test_closed_feedback_targets_latest_or_named_primary_reply_and_is_idempotent(tmp_path, capsys):
    base = tmp_path
    sid = SessionId("review")
    with Session(base, sid) as session:
        session.start()
        first = reply(session, "first")
        latest = reply(session, "latest")
        child = reply(session, "child", parent="latest")
        reply(session, "author", runtime="resident-source-author")
    command = ["improvements", "--base-dir", str(tmp_path), sid]
    note = "You could have checked with an available tool."
    _subcommand([*command, "--feedback", note])
    _subcommand([*command, "--feedback", note])
    _subcommand([*command, "--turn", str(first), "--feedback", "This needed a shorter answer."])
    assert f"turn ending at event {latest}" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        _subcommand([*command, "--turn", str(child), "--feedback", note])
    rows = read_session(base, sid)
    assert [row.event.type for row in rows if row.seq > child + 2] == ["improvement_recorded"] * 2
    records = [r for r in read_improvements(tmp_path, sid).evidence.values() if r.category == "correction"]
    assert [(r.source_seq, r.category) for r in records] == [(latest, "correction"), (first, "correction")]
    assert records[0].observation == "Operator feedback: " + note


def test_feedback_refuses_active_or_missing_sessions_without_creating_a_run(tmp_path):
    from harness.improvement_feedback import record_closed_feedback
    from harness.log import SessionLockedError

    sid = SessionId("active")
    with Session(tmp_path, sid) as session:
        session.start()
        reply(session, "answer")
        with pytest.raises(SessionLockedError):
            record_closed_feedback(tmp_path, sid, "Late correction")
        assert all(r.category != "correction" for r in read_improvements(tmp_path, sid).evidence.values())
    with pytest.raises(ValueError, match="Session not found"):
        record_closed_feedback(tmp_path, SessionId("missing"), "Late correction")
    assert not list((tmp_path / "sessions").glob("missing*"))


def test_confirmed_flaw_records_failure_once_and_keeps_the_original_reply(tmp_path):
    sid = SessionId("failed-answer")
    with Session(tmp_path, sid) as session:
        session.start()
        seq = reply(session, "answer")
    original = read_session(tmp_path, sid)
    command = ["improvements", "--base-dir", str(tmp_path), sid, "--turn", str(seq),
               "--failure", "The answer used the planned destination instead of the confirmed assignment."]
    _subcommand(command)
    _subcommand(command)
    rows = read_session(tmp_path, sid)
    assert rows[:-1] == original
    error = rows[-1].event.record
    assert error.category == "failure" and error.source_seq == seq
    assert read_improvements(tmp_path, sid).evidence[error.id] == error


async def test_terminal_feedback_keeps_literal_prose_and_applies_the_active_redactor(tmp_path):
    from tests.test_tui import make_app
    from tests.test_tui_improvements import submit

    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        seq = reply(app.kernel.session, "answer")
        app.kernel.loop.dispatcher._redact = lambda s: s.replace("private-token", "[redacted]")
        await submit(app, pilot, f"/improvements feedback --turn {seq} It didn't check private-token.")
        await submit(app, pilot, "/improvements feedback It didn't check private-token.")
        events = [r.event for r in read_session(tmp_path, app.kernel.session.id)]
        record, = [event.record for event in events if isinstance(event, ImprovementRecorded)]
        assert record.source_seq == seq
        assert record.observation == "Operator feedback: It didn't check [redacted]."
        assert not any(e.type == "model_call_started" for e in events)
