"""Resume a session: fold the log, close dangling intents, continue the seq line.

SESSION_START lifecycle hooks do not re-fire on resume — the session already
started once and context hooks (memory briefs) must not double-inject. Seed
the rebuilt transcript into AgentLoop(history=...) and do not call start().
"""

import os
from pathlib import Path

from harness.events import SessionResumed
from harness.fold import fold, resume_repairs
from harness.log import SessionLockedError, read_session
from harness.messages import Message
from harness.session import Session
from harness.types import ModelId, SessionId


def _clear_stale_lock(base: Path, session_id: SessionId) -> None:
    """Remove the lockfile iff its recorded pid is no longer alive.

    A live pid means a real concurrent writer: refuse loudly."""
    lock = base / "sessions" / f"{session_id}.lock"
    if not lock.exists():
        return
    raw = lock.read_text(encoding="utf-8").strip()
    try:
        pid = int(raw)
    except ValueError:
        raise SessionLockedError(f"{lock}: unreadable pid {raw!r} — remove manually") from None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        lock.unlink(missing_ok=True)  # owner is dead; the lock is stale
        return
    except PermissionError:
        pass  # pid exists under another uid: treat as alive
    raise SessionLockedError(f"{lock}: held by live pid {pid}")


def resume_session(
    base: Path,
    session_id: SessionId,
    *,
    default_model: ModelId | None = None,
) -> tuple[Session, list[Message]]:
    """Reopen a session for continued writing.

    Returns (session, transcript). The session's seq continues after the last
    logged event; a SessionResumed run boundary is always appended first;
    ToolCallAborted repairs for dangling intents follow and are reflected in
    the returned transcript."""
    _clear_stale_lock(base, session_id)
    envelopes = read_session(base, session_id, repair=True)
    state = fold(envelopes)
    session = Session(base, session_id, default_model=default_model, start_seq=state.last_seq)
    try:
        session.append(SessionResumed())
        appended = [session.append(repair) for repair in resume_repairs(state)]
        if appended:
            state = fold(envelopes + appended)
    except Exception:
        session.close()
        raise
    return session, state.messages
