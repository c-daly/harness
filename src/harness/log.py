"""Per-session append-only JSONL log: the source of truth (with the blob sidecar)."""

import fcntl
import json
import os
import re
from pathlib import Path

from harness.events import Envelope, parse_envelope_line
from harness.persistence import atomic_write, sync_directory
from harness.types import SessionId


class SessionLockedError(Exception):
    """Another writer holds this session. Double-resume would interleave two writers."""


def _session_directory(base: Path, session_id: SessionId) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(session_id)):
        raise ValueError("invalid session id")
    return base / "sessions"


class SessionLock:
    """Own recovery and appending as one transaction.

    The advisory guard inode is permanent: unlinking a flock file would allow
    two owners on different inodes. The PID marker also excludes older Harness
    writers and is removed only while the advisory guard is still held.
    """

    def __init__(self, base: Path, session_id: SessionId, *, recover_stale=False) -> None:
        directory = _session_directory(base, session_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{session_id}.lock"
        self._marker_owned = False
        fd = os.open(directory / f"{session_id}.guard",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._guard = os.fdopen(fd, "r+b")
        try:
            try:
                fcntl.flock(self._guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionLockedError(f"{self.path}: session lock held") from None
            if self.path.exists() or self.path.is_symlink():
                if not recover_stale or self.path.is_symlink():
                    raise SessionLockedError(f"{self.path}: session lock held")
                raw = self.path.read_text(encoding="utf-8").strip()
                try:
                    pid = int(raw)
                    if pid <= 0:
                        raise ValueError
                except ValueError:
                    raise SessionLockedError(f"{self.path}: unreadable pid — inspect manually") from None
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    self.path.unlink()
                except PermissionError:
                    raise SessionLockedError(f"{self.path}: held by live pid {pid}") from None
                else:
                    raise SessionLockedError(f"{self.path}: held by live pid {pid}")
            try:
                marker = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                raise SessionLockedError(f"{self.path}: session lock held") from None
            self._marker_owned = True
            with os.fdopen(marker, "wb") as output:
                output.write(str(os.getpid()).encode())
                output.flush()
                os.fsync(output.fileno())
            sync_directory(directory)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._guard.closed:
            return
        try:
            if self._marker_owned:
                self.path.unlink(missing_ok=True)
                self._marker_owned = False
        finally:
            self._guard.close()

    def __enter__(self) -> "SessionLock":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class EventLogWriter:
    def __init__(self, base: Path, session_id: SessionId, *, _lock: SessionLock | None = None) -> None:
        sessions = _session_directory(base, session_id)
        self._lock = _lock or SessionLock(base, session_id)
        self.path = sessions / f"{session_id}.jsonl"
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            self._fh = os.fdopen(fd, "a", encoding="utf-8")
            # one syscall per session start: make the new file's directory
            # entry durable — intent fsyncs can't protect a name that was
            # never written to disk
            sync_directory(sessions)
        except BaseException:
            if getattr(self, "_fh", None) is not None:
                self._fh.close()
            self._lock.close()
            raise

    def append(self, envelope: Envelope) -> None:
        self._fh.write(envelope.model_dump_json() + "\n")
        self._fh.flush()
        if envelope.event.is_intent:
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        try:
            if not self._fh.closed:
                try:
                    self._fh.flush()
                    os.fsync(self._fh.fileno())
                finally:
                    self._fh.close()
        finally:
            self._lock.close()

    def __enter__(self) -> "EventLogWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class TornLogError(Exception):
    """Log ends in a torn line and repair was not authorized."""


def _scan(raw: bytes, session_id: SessionId) -> tuple[list[Envelope], int | None]:
    envelopes: list[Envelope] = []
    offset = 0
    for line in raw.splitlines(keepends=True):
        # A newline commits a JSONL record. A complete JSON value without its
        # delimiter must not be followed by an append on the same line.
        if not line.endswith(b"\n"):
            return envelopes, offset
        try:
            stripped = line.decode("utf-8").strip()
            if stripped:
                json.loads(stripped)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return envelopes, offset
        if stripped:
            envelope = parse_envelope_line(stripped)
            if envelope.session_id != session_id or (envelopes and envelope.seq <= envelopes[-1].seq):
                raise ValueError("session log has inconsistent identity or sequence")
            envelopes.append(envelope)
        offset += len(line)
    return envelopes, None


def read_session(
    base: Path, session_id: SessionId, *, repair: bool = False, _lock: SessionLock | None = None,
) -> list[Envelope]:
    """Read a session log into envelopes.

    Valid lines parse via parse_envelope_line (unknown event types degrade to
    UnknownEvent). A structurally incomplete line — a torn tail from a crash,
    or mid-file corruption — stops the scan: without repair, TornLogError;
    with repair=True, everything from the first bad line onward (including
    any valid lines after mid-file corruption) is quarantined to <id>.torn
    and the log is atomically replaced by its valid prefix. Repair refuses to run
    while the session lock is held (a live writer may own the torn bytes),
    and refuses to overwrite a differing prior quarantine.
    """
    path = _session_directory(base, session_id) / f"{session_id}.jsonl"
    raw = path.read_bytes()
    envelopes, offset = _scan(raw, session_id)
    if offset is None:
        return envelopes
    if not repair:
        raise TornLogError(f"{path}: torn tail at byte {offset}")
    if _lock is None:
        try:
            lock = SessionLock(base, session_id)
        except SessionLockedError:
            raise TornLogError(f"{path}: repair refused — session lock held") from None
        with lock:
            # Reread after obtaining ownership; an earlier writer may have
            # finished the record between our first read and lock acquisition.
            return read_session(base, session_id, repair=True, _lock=lock)
    if _lock.path != path.with_suffix(".lock") or _lock._guard.closed:
        raise SessionLockedError("repair requires ownership of this session")
    torn = raw[offset:]
    torn_path = path.with_suffix(".torn")
    try:
        atomic_write(torn_path, torn, replace=False)
    except FileExistsError:
        if torn_path.is_symlink() or torn_path.read_bytes() != torn:
            raise TornLogError(
                f"{torn_path}: differing quarantine already exists — move it aside first"
            ) from None
    atomic_write(path, raw[:offset])
    return envelopes
