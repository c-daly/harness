"""Resume a session: fold the log, close dangling intents, continue the seq line.

SESSION_START lifecycle hooks do not re-fire on resume — the session already
started once and context hooks (memory briefs) must not double-inject. Seed
the rebuilt transcript into AgentLoop(history=...) and do not call start().
"""

from pathlib import Path
from typing import Callable

from harness.events import SessionResumed
from harness.fold import FoldedState, fold, resume_repairs
from harness.log import EventLogWriter, SessionLock, read_session
from harness.messages import Message
from harness.session import Session
from harness.types import ModelId, SessionId


def _clear_stale_lock(base: Path, session_id: SessionId) -> None:
    """Remove the lockfile iff its recorded pid is no longer alive.

    A live pid means a real concurrent writer: refuse loudly."""
    with SessionLock(base, session_id, recover_stale=True):
        pass  # Preflight only. _reopen holds ownership throughout recovery.


def _reopen(
    base: Path, session_id: SessionId, *, default_model: ModelId | None = None
):
    """Shared reopen: stale-lock clear, lenient-repair read, fold, seq-continued session."""
    lock = SessionLock(base, session_id, recover_stale=True)
    try:
        envelopes = read_session(base, session_id, repair=True, _lock=lock)
        state = fold(envelopes)
        writer = EventLogWriter(base, session_id, _lock=lock)
        session = Session(base, session_id, default_model=default_model,
                          start_seq=state.last_seq, _writer=writer)
    except BaseException:
        lock.close()
        raise
    return session, envelopes, state


def append_events(base: Path, session_id: SessionId, events: list) -> None:
    """Reopen a closed session just long enough to append bookkeeping events
    (outcomes, annotations). No SessionResumed: this is not a run.

    A crashed session's dangling intents are NOT repaired here — resume_session
    owns repair; an outcome appended to a dirty session is still readable and the
    next resume repairs as usual."""
    if not events:
        return  # avoid pointless lock churn
    session, _, _ = _reopen(base, session_id)
    try:
        for event in events:
            session.append(event)
    finally:
        session.close()


def resume_session(
    base: Path,
    session_id: SessionId,
    *,
    default_model: ModelId | None = None,
    configure: Callable[[FoldedState], None] | None = None,
) -> tuple[Session, list[Message]]:
    """Reopen a session for continued writing.

    Returns (session, transcript). The session's seq continues after the last
    logged event; a SessionResumed run boundary is always appended first;
    ToolCallAborted repairs for dangling intents follow and are reflected in
    the returned transcript."""
    session, envelopes, state = _reopen(base, session_id, default_model=default_model)
    try:
        # Configuration must be chosen from the replay held by this writer,
        # before a resumed boundary or repair is published. A failed choice
        # releases ownership without appending a misleading run boundary.
        if configure is not None:
            configure(state)
        session.append(SessionResumed())
        appended = [session.append(repair) for repair in resume_repairs(state)]
        if appended:
            state = fold(envelopes + appended)
    except BaseException:
        session.close()
        raise
    return session, state.messages
