"""Independent acceptance checks for the generated task-9 candidate."""

import subprocess
import sys

import pytest

from harness.events import UserMessage
from harness.session import Session
from harness.session_admin import RepairAuthorization, repair_session, verify_session
from harness.types import SessionId


def torn_session(base):
    session = Session(base, SessionId("review"))
    session.start()
    session.append(UserMessage(text="old"))
    session.close()
    path = base / "sessions/review.jsonl"
    path.write_bytes(path.read_bytes() + b'{"unfinished"')
    return path


def test_report_digest_changes_when_same_size_session_content_changes(tmp_path):
    path = torn_session(tmp_path)
    before = verify_session(tmp_path, SessionId("review"), "quick")
    raw = path.read_bytes()
    changed = raw.replace(b'"old"', b'"new"')
    assert changed != raw and len(changed) == len(raw)
    path.write_bytes(changed)
    after = verify_session(tmp_path, SessionId("review"), "quick")
    assert after.report_sha256 != before.report_sha256


def test_stale_authorization_refuses_same_offset_torn_tail_change(tmp_path):
    path = torn_session(tmp_path)
    before = verify_session(tmp_path, SessionId("review"), "quick")
    authorization = RepairAuthorization(session_id="review", operation="torn-tail",
        integrity_report_sha256=before.report_sha256)
    changed = path.read_bytes().replace(b'"unfinished"', b'"DIFFERENT!"')
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="stale"):
        repair_session(tmp_path, SessionId("review"), authorization)
    assert path.read_bytes() == changed


def test_sessions_cli_has_a_working_entrypoint():
    result = subprocess.run([sys.executable, "-c", "from harness.cli import main; main()",
        "sessions", "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "verify" in result.stdout
