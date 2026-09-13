"""Frozen operator behavior checks for task 9; not a substitute for scope review.

CLI contract: sessions --base-dir DIR COMMAND ID [options]. Lifecycle commands
take --output, --report-sha256, --operation and --confirm as appropriate. Storage
configuration has an explicit loader and EventLogWriter accepts storage_limits
and free_bytes for deterministic boundary probes. These are interface choices
for this task, not worker-authored grading controls.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from harness.events import Envelope, SessionStarted, ToolCallProposed, UserMessage
from harness.log import EventLogWriter
from harness.session import Session
from harness.session_admin import verify_session
from harness.types import SessionId


def make_session(base, identity="lifecycle"):
    session = Session(base, SessionId(identity))
    session.start()
    ref = session.blobs.put(b"retained artifact")
    from harness.events import CustomEvent
    session.append(CustomEvent(namespace="test", name="artifact", data={"ref": ref.model_dump()}))
    session.append(UserMessage(text="retained conversation"))
    session.close()
    return identity, ref


def cli(base, *args):
    return subprocess.run([sys.executable, "-c", "from harness.cli import main; main()", "sessions",
                           "--base-dir", str(base), *args], capture_output=True, text=True, timeout=30)


def okay(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_lifecycle_cli_exports_real_deterministic_archive_then_restores_and_purges(tmp_path):
    identity, ref = make_session(tmp_path)
    original = (tmp_path / f"sessions/{identity}.jsonl").read_bytes()
    assert identity in okay(cli(tmp_path, "list"))
    assert identity in okay(cli(tmp_path, "show", identity))
    assert okay(cli(tmp_path, "verify", identity, "--full")).strip()
    archives = [tmp_path / f"export-{n}.tar" for n in range(2)]
    for archive in archives:
        okay(cli(tmp_path, "export", identity, "--output", str(archive)))
        assert archive.is_file(), "export must produce an archive, not just return successfully"
    assert archives[0].read_bytes() == archives[1].read_bytes()
    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()
        assert names == sorted(names)
        assert any("manifest" in name for name in names)
        assert any(ref.sha256 in name for name in names)
        assert all(member.uid == 0 and member.gid == 0 and member.mtime == 0 for member in archive.getmembers())
        manifest_name = next(name for name in names if "manifest" in name)
        manifest = json.loads(archive.extractfile(manifest_name).read())
        assert hashlib.sha256(original).hexdigest() in json.dumps(manifest)
    okay(cli(tmp_path, "trash", identity))
    assert not (tmp_path / f"sessions/{identity}.jsonl").exists()
    trash_dirs = [p for p in tmp_path.rglob("*") if p.is_dir() and "trash" in p.name]
    assert trash_dirs and all(p.stat().st_mode & 0o777 == 0o700 for p in trash_dirs)
    okay(cli(tmp_path, "restore", identity))
    assert (tmp_path / f"sessions/{identity}.jsonl").read_bytes() == original
    assert (tmp_path / f"sessions/{identity}/blobs/{ref.sha256}").read_bytes() == b"retained artifact"
    okay(cli(tmp_path, "trash", identity))
    assert cli(tmp_path, "purge", identity, "--confirm", "wrong").returncode != 0
    okay(cli(tmp_path, "restore", identity))
    okay(cli(tmp_path, "trash", identity))
    okay(cli(tmp_path, "purge", identity, "--confirm", identity))
    assert cli(tmp_path, "restore", identity).returncode != 0


def test_locked_session_refuses_mutation_and_export_without_losing_data(tmp_path):
    from harness.log import SessionLock
    identity, _ = make_session(tmp_path)
    path = tmp_path / f"sessions/{identity}.jsonl"
    before = path.read_bytes()
    with SessionLock(tmp_path, SessionId(identity)):
        for args in [("trash", identity), ("restore", identity),
                     ("purge", identity, "--confirm", identity),
                     ("export", identity, "--output", str(tmp_path / "locked.tar"))]:
            result = cli(tmp_path, *args)
            assert result.returncode != 0, args
            assert "lock" in (result.stdout + result.stderr).lower(), result.stderr
            assert path.read_bytes() == before
    assert not (tmp_path / "locked.tar").exists()


def test_quick_and_full_inspect_real_blob_integrity(tmp_path):
    identity, ref = make_session(tmp_path)
    blob = tmp_path / f"sessions/{identity}/blobs/{ref.sha256}"
    blob.write_bytes(b"x" * ref.size)
    quick = verify_session(tmp_path, SessionId(identity), "quick")
    full = verify_session(tmp_path, SessionId(identity), "full")
    assert not any(f.severity == "error" for f in quick.findings)
    assert any(f.severity == "error" and "digest" in f.message for f in full.findings)
    blob.write_bytes(b"x")
    assert any(f.severity == "error" and "size" in f.message for f in verify_session(tmp_path, identity, "quick").findings)
    blob.unlink()
    assert any(f.severity == "error" and "missing" in f.message for f in verify_session(tmp_path, identity, "quick").findings)


def test_verification_reports_dangling_intent_and_real_lock_state(tmp_path):
    session = Session(tmp_path, SessionId("intent"))
    session.start()
    session.append(ToolCallProposed(call_id="unsettled", tool="write_file", args={"file_path": "x"}))
    session.close()
    report = verify_session(tmp_path, session.id, "quick")
    assert any(f.call_id == "unsettled" and f.remediation for f in report.findings)
    assert not any("locked" in f.message.lower() for f in report.findings)
    from harness.log import SessionLock
    with SessionLock(tmp_path, session.id):
        assert any("locked" in f.message.lower() for f in verify_session(tmp_path, session.id, "quick").findings)


def test_storage_limits_enforce_event_log_and_free_space_boundaries(tmp_path):
    from harness.storage import StorageLimits, StorageLimitExceeded
    event = Envelope(session_id="storage", seq=1, ts=1, event=SessionStarted())
    length = len((event.model_dump_json() + "\n").encode())
    limits = StorageLimits(max_event_bytes=length, max_session_log_bytes=length * 2, min_free_bytes=10)
    free = [10]
    with EventLogWriter(tmp_path, SessionId("storage"), storage_limits=limits, free_bytes=lambda: free[0]) as writer:
        writer.append(event)
        before = writer.path.read_bytes()
        large = Envelope(session_id="storage", seq=2, ts=2, event=UserMessage(text="x" * length))
        with pytest.raises(StorageLimitExceeded):
            writer.append(large)
        assert writer.path.read_bytes() == before
        free[0] = 9
        with pytest.raises(StorageLimitExceeded):
            writer.append(event)
        assert writer.path.read_bytes() == before
        free[0] = 10
        writer.append(event)
        with pytest.raises(StorageLimitExceeded):
            writer.append(event)
        assert writer.path.read_bytes() == before + before


def test_storage_defaults_and_explicit_runtime_config(tmp_path):
    from harness.storage import StorageLimits, load_storage_limits
    limits = StorageLimits()
    assert (limits.max_event_bytes, limits.max_session_log_bytes, limits.min_free_bytes) == (1048576, 1073741824, 134217728)
    config = tmp_path / "runtime.toml"
    config.write_text("[storage]\nmax_event_bytes = 1000\nmax_session_log_bytes = 2000\nmin_free_bytes = 3\n")
    loaded = load_storage_limits(config)
    assert (loaded.max_event_bytes, loaded.max_session_log_bytes, loaded.min_free_bytes) == (1000, 2000, 3)


def test_session_summaries_and_user_documentation_cover_new_operations(tmp_path):
    from harness.sessions import list_sessions
    identity, _ = make_session(tmp_path)
    summary = next(s for s in list_sessions(tmp_path) if s.session_id == identity)
    assert hasattr(summary, "integrity") and hasattr(summary, "locked")
    assert summary.locked is False
    guide = Path("docs/user-guide.md").read_text().lower()
    for operation in ("sessions verify", "sessions export", "sessions trash", "sessions restore", "sessions purge"):
        assert operation in guide, f"missing documentation for {operation}"
