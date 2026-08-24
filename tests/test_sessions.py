"""Tests for the session lister: list_sessions enumerates the writer's
session directory into age/first-prompt/last-model summaries, newest first,
and never raises -- a torn or otherwise unreadable log appears in the
listing with its `error` field set instead of being dropped."""

import os

from harness.events import Envelope, ModelCallCompleted, SessionStarted, UserMessage
from harness.log import EventLogWriter
from harness.sessions import SessionSummary, list_sessions
from harness.types import CallId, ModelId, SessionId


def _write_session(base, session_id, events, *, mtime=None):
    with EventLogWriter(base, SessionId(session_id)) as w:
        for i, event in enumerate(events, start=1):
            w.append(Envelope(session_id=SessionId(session_id), seq=i, ts=float(i), event=event))
    if mtime is not None:
        path = base / "sessions" / f"{session_id}.jsonl"
        os.utime(path, (mtime, mtime))


def test_empty_base_returns_empty_list(tmp_path):
    assert list_sessions(tmp_path) == []


def test_no_sessions_dir_at_all_returns_empty_list(tmp_path):
    # base exists but nothing has ever been written under it
    assert list_sessions(tmp_path / "nonexistent") == []


def test_lists_sessions_newest_first_by_mtime(tmp_path):
    _write_session(tmp_path, "old", [SessionStarted()], mtime=1_000_000)
    _write_session(tmp_path, "new", [SessionStarted()], mtime=3_000_000)
    _write_session(tmp_path, "mid", [SessionStarted()], mtime=2_000_000)
    summaries = list_sessions(tmp_path)
    assert [s.session_id for s in summaries] == [
        SessionId("new"),
        SessionId("mid"),
        SessionId("old"),
    ]


def test_first_prompt_event_count_and_last_model(tmp_path):
    events = [
        SessionStarted(),
        UserMessage(text="a" * 100),  # first_prompt truncates to 80 chars
        ModelCallCompleted(
            call_id=CallId("c1"), model=ModelId("first-model"), message={}, usage={}
        ),
        UserMessage(text="second question"),  # not the FIRST prompt -- ignored
        ModelCallCompleted(
            call_id=CallId("c2"), model=ModelId("second-model"), message={}, usage={}
        ),
    ]
    _write_session(tmp_path, "s1", events)
    summaries = list_sessions(tmp_path)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.session_id == SessionId("s1")
    assert summary.event_count == len(events)
    assert summary.first_prompt == "a" * 80
    assert summary.last_model == "second-model"
    assert summary.error is None


def test_session_with_no_user_message_has_empty_first_prompt(tmp_path):
    _write_session(tmp_path, "s1", [SessionStarted()])
    summaries = list_sessions(tmp_path)
    assert summaries[0].first_prompt == ""
    assert summaries[0].last_model is None


def test_torn_log_appears_with_error_set_not_hidden(tmp_path):
    _write_session(tmp_path, "good", [SessionStarted(), UserMessage(text="hi")])
    _write_session(tmp_path, "torn", [SessionStarted()])
    path = tmp_path / "sessions" / "torn.jsonl"
    with open(path, "a") as fh:
        fh.write('{"v": 1, "session_id": "torn", "seq": 2, "ts"')  # crash mid-write

    summaries = list_sessions(tmp_path)

    assert len(summaries) == 2  # torn log is NOT dropped from the listing
    by_id = {s.session_id: s for s in summaries}
    assert by_id[SessionId("good")].error is None
    torn_summary = by_id[SessionId("torn")]
    assert torn_summary.error is not None
    assert "torn" in torn_summary.error.lower() or "byte" in torn_summary.error.lower()


def test_limit_caps_returned_sessions(tmp_path):
    _write_session(tmp_path, "a", [SessionStarted()], mtime=1_000_000)
    _write_session(tmp_path, "b", [SessionStarted()], mtime=2_000_000)
    _write_session(tmp_path, "c", [SessionStarted()], mtime=3_000_000)
    summaries = list_sessions(tmp_path, limit=2)
    assert [s.session_id for s in summaries] == [SessionId("c"), SessionId("b")]


def test_returns_session_summary_instances(tmp_path):
    _write_session(tmp_path, "s1", [SessionStarted()])
    summaries = list_sessions(tmp_path)
    assert isinstance(summaries[0], SessionSummary)
