"""Per-session append-only JSONL log: the source of truth (with the blob sidecar)."""

import json
import os
from pathlib import Path

from harness.events import Envelope, parse_envelope_line
from harness.types import SessionId


class SessionLockedError(Exception):
    """Another writer holds this session. Double-resume would interleave two writers."""


class EventLogWriter:
    def __init__(self, base: Path, session_id: SessionId) -> None:
        sessions = base / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        self._lock_path = sessions / f"{session_id}.lock"
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SessionLockedError(str(self._lock_path)) from None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self.path = sessions / f"{session_id}.jsonl"
        try:
            self._fh = open(self.path, "a", encoding="utf-8")
            # one syscall per session start: make the new file's directory
            # entry durable — intent fsyncs can't protect a name that was
            # never written to disk
            dir_fd = os.open(sessions, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            if getattr(self, "_fh", None) is not None:
                self._fh.close()
            self._lock_path.unlink(missing_ok=True)
            raise

    def append(self, envelope: Envelope) -> None:
        self._fh.write(envelope.model_dump_json() + "\n")
        self._fh.flush()
        if envelope.event.is_intent:
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()
        self._lock_path.unlink(missing_ok=True)

    def __enter__(self) -> "EventLogWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class TornLogError(Exception):
    """Log ends in a torn line and repair was not authorized."""


def read_session(base: Path, session_id: SessionId, *, repair: bool = False) -> list[Envelope]:
    path = base / "sessions" / f"{session_id}.jsonl"
    raw = path.read_text(encoding="utf-8")
    envelopes: list[Envelope] = []
    good_offset = 0
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            good_offset += len(line)
            continue
        try:
            json.loads(stripped)  # structural check first: is this even a complete line?
        except json.JSONDecodeError:
            torn = raw[good_offset:]
            if not repair:
                raise TornLogError(f"{path}: torn tail at byte {good_offset}") from None
            (path.parent / f"{session_id}.torn").write_text(torn, encoding="utf-8")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw[:good_offset])
            break
        envelopes.append(parse_envelope_line(stripped))
        good_offset += len(line)
    return envelopes
