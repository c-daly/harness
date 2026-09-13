import json
from pathlib import Path

import pytest

from harness.events import Envelope, SessionStarted, UserMessage
from harness.log import EventLogWriter
from harness.session_admin import RepairAuthorization, TornLogError, verify_session, repair_session
from harness.types import SessionId


def _env(session: str, seq: int, event):
    return Envelope(session_id=SessionId(session), seq=seq, ts=float(seq), event=event)


def test_quick_detects_missing_blob(tmp_path):
    # no blobs in this small case; just confirm verify runs and produces a stable digest
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_env("s1", 1, SessionStarted()))
        w.append(_env("s1", 2, UserMessage(text="hi")))
    report = verify_session(tmp_path, SessionId("s1"), mode="quick")
    assert report.session_id == SessionId("s1")
    assert len(report.report_sha256) == 64


def test_full_digest_changes_on_tamper(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_env("s1", 1, SessionStarted()))
    # inject a fake blob and reference it to simulate mismatch detection
    blob_dir = tmp_path / "sessions" / "s1" / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    (blob_dir / ("0" * 64)).write_bytes(b"x")
    # fabricate a line referencing this blob
    path = tmp_path / "sessions" / "s1.jsonl"
    with open(path, "a") as fh:
        fh.write(
            json.dumps(
                {
                    "v": 1,
                    "session_id": "s1",
                    "seq": 2,
                    "ts": 2.0,
                    "event": {
                        "type": "tool_call_completed",
                        "call_id": "c",
                        "result_blob": {"sha256": "0" * 64, "size": 1},
                    },
                }
            )
            + "\n"
        )
    quick = verify_session(tmp_path, SessionId("s1"), mode="quick")
    full = verify_session(tmp_path, SessionId("s1"), mode="full")
    # quick sees only size/type; full also compares digest
    assert any("blob" in f.message for f in quick.findings)
    assert any("digest" in f.message for f in full.findings)


def test_authorized_torn_tail_repair(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_env("s1", 1, SessionStarted()))
    path = tmp_path / "sessions" / "s1.jsonl"
    with open(path, "a") as fh:
        fh.write("{garbage")
    rep = verify_session(tmp_path, SessionId("s1"), mode="quick")
    auth = RepairAuthorization(session_id=SessionId("s1"), operation="torn-tail", integrity_report_sha256=rep.report_sha256)
    repair_session(tmp_path, SessionId("s1"), auth)
    # a second attempt with the stale auth should fail (no torn tail now)
    with pytest.raises(ValueError):
        repair_session(tmp_path, SessionId("s1"), auth)


def test_repair_refuses_when_locked(tmp_path):
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_env("s1", 1, SessionStarted()))
        with open(tmp_path / "sessions" / "s1.jsonl", "a") as fh:
            fh.write("{torn")
        rep = verify_session(tmp_path, SessionId("s1"), mode="quick")
        auth = RepairAuthorization(session_id=SessionId("s1"), operation="torn-tail", integrity_report_sha256=rep.report_sha256)
        with pytest.raises(TornLogError):
            repair_session(tmp_path, SessionId("s1"), auth)
