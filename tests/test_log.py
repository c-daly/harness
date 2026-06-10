import json

import pytest

from harness.events import Envelope, SessionStarted, ToolCallProposed, UserMessage
from harness.log import EventLogWriter, SessionLockedError
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
