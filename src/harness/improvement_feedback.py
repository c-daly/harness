"""Attach explicit operator feedback to a finished conversation turn."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from harness.events import ImprovementRecorded
from harness.improvement import Evidence
from harness.improvement_journal import ImprovementJournal
from harness.improvement_outcomes import finished_replies
from harness.log import EventLogWriter, SessionLock, read_session
from harness.session import Session


@dataclass(frozen=True)
class FeedbackReceipt:
    evidence: Evidence
    created: bool

    def __str__(self):
        label = "failure" if self.evidence.category == "failure" else "feedback"
        action = f"Saved {label}" if self.created else f"This {label} is already recorded"
        return f"{action} for turn ending at event {self.evidence.source_seq}. Evidence {self.evidence.id}"


def record_feedback(journal, note: str, *, turn: int | None = None, failure=False, envelopes=None, receipt=False):
    session = journal.session
    label = "failure" if failure else "feedback"
    observation = ("Operator-confirmed flawed response: " if failure else "Operator feedback: ") + note.strip()
    if not note.strip() or len(observation) > 4096:
        raise ValueError("Enter a nonblank observation that fits the 4096-character evidence field.")
    if envelopes is None:
        envelopes = read_session(session.base, session.id, repair=False)
    target = None
    for env in finished_replies(envelopes):
        if turn is None or env.seq == turn:
            target = env
    if target is None:
        if turn is not None:
            raise ValueError(f"Event {turn} is not the end of a conversation turn in this session.")
        raise ValueError("This session has no finished conversation turn to review.")

    digest = hashlib.sha256(f"{session.id}\0{target.seq}\0{observation}".encode()).hexdigest()
    evidence = Evidence(id=f"{label}:{target.seq}:{digest}", source_session=session.id,
                        source_seq=target.seq, category="failure" if failure else "correction", observation=observation)
    previous = next((env.event.record for env in envelopes if isinstance(env.event, ImprovementRecorded)
                     and env.event.record.id == evidence.id), None)
    if previous is None:
        journal.record(evidence)
    elif previous != evidence:
        raise ValueError("The existing feedback differs from its expected record.")
    saved = FeedbackReceipt(evidence, previous is None)
    return saved if receipt else str(saved)


def record_closed_feedback(base: Path, session_id, note: str, *, turn: int | None = None, failure=False, receipt=False):
    # Hold the ordinary writer lock from selecting the turn through durable append.
    # Bookkeeping on a closed session must not resume work or repair a torn log.
    if not (base / "sessions" / f"{session_id}.jsonl").is_file():
        raise ValueError("Session not found.")
    with SessionLock(base, session_id, recover_stale=True) as lock:
        envelopes = read_session(base, session_id, repair=False)
        if not envelopes:
            raise ValueError("This session has no finished conversation turn to review.")
        with Session(base, session_id, start_seq=envelopes[-1].seq,
                     _writer=EventLogWriter(base, session_id, _lock=lock)) as session:
            return record_feedback(ImprovementJournal(session), note, turn=turn, failure=failure,
                                   envelopes=envelopes, receipt=receipt)
