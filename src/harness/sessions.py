"""Session lister: enumerate the writer's session directory into summaries.

One clear responsibility, kept separate from log.py's single-session
read/write contract: scan every `<id>.jsonl` under `base/sessions/`, fold
each into a lightweight summary for `--continue` and the TUI's `/resume`
picker. A torn or otherwise unreadable log is never dropped from the
listing -- it appears with `error` set so a user can still see (and, via
--resume's own repairing reopen, still recover) it."""

from dataclasses import dataclass
from pathlib import Path

from harness.events import ModelCallCompleted, UserMessage
from harness.log import read_session
from harness.types import SessionId

_FIRST_PROMPT_CAP = 80


@dataclass(frozen=True)
class SessionSummary:
    session_id: SessionId
    mtime: float
    event_count: int
    first_prompt: str
    last_model: str | None
    error: str | None = None


def list_sessions(base: Path, limit: int | None = None) -> list[SessionSummary]:
    """Newest-first (by log file mtime) summary of every session under
    `base`. Never raises: a session whose log can't be read (torn tail,
    missing file, anything else) still gets a row, with `error` set instead
    of the file being silently skipped."""
    sessions_dir = base / "sessions"
    if not sessions_dir.is_dir():
        return []

    summaries: list[SessionSummary] = []
    for path in sessions_dir.glob("*.jsonl"):
        session_id = SessionId(path.stem)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # vanished between glob and stat -- nothing to report

        try:
            envelopes = read_session(base, session_id, repair=False)
        except Exception as exc:
            summaries.append(
                SessionSummary(
                    session_id=session_id,
                    mtime=mtime,
                    event_count=0,
                    first_prompt="",
                    last_model=None,
                    error=str(exc),
                )
            )
            continue

        first_prompt = ""
        last_model: str | None = None
        for envelope in envelopes:
            event = envelope.event
            if not first_prompt and isinstance(event, UserMessage):
                first_prompt = event.text[:_FIRST_PROMPT_CAP]
            if isinstance(event, ModelCallCompleted) and event.purpose == "conversation":
                last_model = str(event.model)
        summaries.append(
            SessionSummary(
                session_id=session_id,
                mtime=mtime,
                event_count=len(envelopes),
                first_prompt=first_prompt,
                last_model=last_model,
            )
        )

    summaries.sort(key=lambda s: s.mtime, reverse=True)
    if limit is not None:
        summaries = summaries[:limit]
    return summaries
