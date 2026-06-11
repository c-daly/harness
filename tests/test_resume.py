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


def test_resume_clean_session_appends_nothing(tmp_path):
    with Session(tmp_path, SessionId("s1")) as s:
        s.start()
        s.append(UserMessage(text="hi"))
    before = len(read_session(tmp_path, SessionId("s1")))
    session, messages = resume_session(tmp_path, SessionId("s1"))
    session.close()
    assert len(read_session(tmp_path, SessionId("s1"))) == before
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
