import json

import pytest

from harness.events import Envelope, SessionStarted, ToolCallProposed, UnknownEvent, UserMessage
from harness.log import EventLogWriter, SessionLockedError, TornLogError, read_session
from harness.types import CallId, SessionId, ToolName


def _envelope(seq: int, event) -> Envelope:
    return Envelope(session_id=SessionId("s1"), seq=seq, ts=float(seq), event=event)


def test_append_writes_one_json_line_per_event(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_envelope(1, SessionStarted()))
        w.append(_envelope(2, UserMessage(text="hi")))
    lines = (tmp_path / "sessions" / "s1.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["event"]["text"] == "hi"


def test_lockfile_prevents_second_writer(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")):
        with pytest.raises(SessionLockedError):
            EventLogWriter(tmp_path, SessionId("s1"))


def test_lock_released_on_close(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")):
        pass
    with EventLogWriter(tmp_path, SessionId("s1")):  # re-acquire works
        pass


def test_intent_events_are_fsynced(tmp_path, monkeypatch):
    synced: list[int] = []
    import os
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_envelope(1, UserMessage(text="no sync needed")))
        before = len(synced)
        w.append(_envelope(2, ToolCallProposed(call_id=CallId("c"), tool=ToolName("bash"), args={})))
        assert len(synced) == before + 1


def test_lock_released_when_init_fails(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s1.jsonl").mkdir()  # open-for-append will raise IsADirectoryError
    with pytest.raises(IsADirectoryError):
        EventLogWriter(tmp_path, SessionId("s1"))
    assert not (sessions / "s1.lock").exists()


# --- reader tests ---


def test_reader_round_trips(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_envelope(1, SessionStarted()))
        w.append(_envelope(2, UserMessage(text="hi")))
    envs = read_session(tmp_path, SessionId("s1"))
    assert [e.seq for e in envs] == [1, 2]
    assert envs[1].event.text == "hi"


def test_reader_preserves_unknown_events(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_envelope(1, SessionStarted()))
    path = tmp_path / "sessions" / "s1.jsonl"
    with open(path, "a") as fh:
        fh.write('{"v": 9, "session_id": "s1", "seq": 2, "ts": 2.0, "event": {"type": "novel"}}\n')
    envs = read_session(tmp_path, SessionId("s1"))
    assert isinstance(envs[1].event, UnknownEvent)


def test_reader_quarantines_torn_tail(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_envelope(1, SessionStarted()))
    path = tmp_path / "sessions" / "s1.jsonl"
    with open(path, "a") as fh:
        fh.write('{"v": 1, "session_id": "s1", "seq": 2, "ts"')  # crash mid-write, no newline
    envs = read_session(tmp_path, SessionId("s1"), repair=True)
    assert len(envs) == 1
    assert (tmp_path / "sessions" / "s1.torn").read_text().startswith('{"v": 1')
    # file itself truncated back to valid JSONL
    assert path.read_text().endswith("}\n")


def test_reader_without_repair_raises_on_torn_tail(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_envelope(1, SessionStarted()))
    with open(tmp_path / "sessions" / "s1.jsonl", "a") as fh:
        fh.write("{garbage")
    with pytest.raises(TornLogError):
        read_session(tmp_path, SessionId("s1"))
