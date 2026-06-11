# tests/test_resume.py
import subprocess

import pytest

from harness.events import ToolCallAborted, ToolCallProposed, UserMessage
from harness.log import SessionLockedError, read_session
from harness.messages import Role
from harness.resume import resume_session
from harness.session import Session
from harness.types import CallId, SessionId, ToolName


def _crashed_session(tmp_path):
    """A session whose process died: dangling intent, torn tail, stale lock."""
    session = Session(tmp_path, SessionId("s1"))
    session.start()
    session.append(UserMessage(text="do something"))
    session.append(ToolCallProposed(call_id=CallId("c9"), tool=ToolName("bash"), args={}))
    session._writer._fh.flush()
    path = tmp_path / "sessions" / "s1.jsonl"
    with open(path, "a") as fh:
        fh.write('{"v": 1, "torn')  # crash mid-write
    # simulate dead-process lock: overwrite pid with one that no longer runs
    proc = subprocess.Popen(["true"])
    proc.wait()
    (tmp_path / "sessions" / "s1.lock").write_text(str(proc.pid))
    session._writer._fh.close()  # release fd without unlinking lock (crash semantics)
    return tmp_path


def test_resume_repairs_and_continues_seq(tmp_path):
    base = _crashed_session(tmp_path)
    session, messages = resume_session(base, SessionId("s1"))
    try:
        envs = read_session(base, SessionId("s1"))
        aborted = [e for e in envs if isinstance(e.event, ToolCallAborted)]
        assert len(aborted) == 1 and aborted[0].event.call_id == "c9"
        seqs = [e.seq for e in envs]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # no duplicate seq
        # transcript: user message + aborted tool result
        assert [m.role for m in messages] == [Role.USER, Role.TOOL]
        # quarantine exists
        assert (base / "sessions" / "s1.torn").exists()
    finally:
        session.close()


def test_resume_refuses_live_lock(tmp_path):
    with Session(tmp_path, SessionId("s1")) as live:
        live.start()
        with pytest.raises(SessionLockedError):
            resume_session(tmp_path, SessionId("s1"))


def test_resume_clean_session_appends_only_run_boundary(tmp_path):
    """resume_session appends only the run boundary (SessionResumed); no repairs."""
    from harness.events import SessionResumed
    with Session(tmp_path, SessionId("s1")) as s:
        s.start()
        s.append(UserMessage(text="hi"))
    before = len(read_session(tmp_path, SessionId("s1")))
    session, messages = resume_session(tmp_path, SessionId("s1"))
    session.close()
    after = len(read_session(tmp_path, SessionId("s1")))
    assert after == before + 1
    last = read_session(tmp_path, SessionId("s1"))[-1]
    assert isinstance(last.event, SessionResumed)
    assert [m.role for m in messages] == [Role.USER]


def test_resumed_session_start_refused(tmp_path):
    with Session(tmp_path, SessionId("s1")) as s:
        s.start()
    session, _ = resume_session(tmp_path, SessionId("s1"))
    try:
        with pytest.raises(RuntimeError, match="already called"):
            session.start()
    finally:
        session.close()


def test_resume_marks_the_run_boundary(tmp_path):
    from harness.events import SessionResumed
    with Session(tmp_path, SessionId("s1")) as s:
        s.start()
        s.append(UserMessage(text="hi"))
    session, _ = resume_session(tmp_path, SessionId("s1"))
    session.close()
    envs = read_session(tmp_path, SessionId("s1"))
    resumed = [e for e in envs if isinstance(e.event, SessionResumed)]
    assert len(resumed) == 1
    # old logs (without the event) and folds are unaffected — fold ignores it
    from harness.fold import fold
    assert [m.role.value for m in fold(envs).messages] == ["user"]


def test_append_events_adds_outcome_without_run_boundary(tmp_path):
    from harness.events import SessionOutcome, SessionResumed
    from harness.resume import append_events
    with Session(tmp_path, SessionId("s1")) as s:
        s.start()
        s.append(UserMessage(text="hi"))
    append_events(tmp_path, SessionId("s1"), [SessionOutcome(status="ok", score=1.0, note="done")])
    envs = read_session(tmp_path, SessionId("s1"))
    assert envs[-1].event.status == "ok"
    assert not any(isinstance(e.event, SessionResumed) for e in envs)  # bookkeeping, not a run
    seqs = [e.seq for e in envs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_append_events_refuses_live_lock(tmp_path):
    from harness.events import SessionOutcome
    from harness.resume import append_events
    with Session(tmp_path, SessionId("s1")) as live:
        live.start()
        with pytest.raises(SessionLockedError):
            append_events(tmp_path, SessionId("s1"), [SessionOutcome(status="ok")])
