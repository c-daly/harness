"""Session runtime: seq stamping, log append (sync, source of truth), subscriber fan-out."""

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from harness.blobs import BlobStore
from harness.events import (
    AgentRunFinished, AgentRunStarted, Envelope, Event, ImprovementRecorded, SessionResumed, SessionStarted, TaskChecked,
)
from harness.log import EventLogWriter
from harness.types import ModelId, SessionId

if TYPE_CHECKING:
    from harness.redaction import EventRedactor


class SubscriberBus:
    """Bounded per-subscriber queues; drop-oldest. Observation never stalls the session."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.append(queue)
        return queue

    def publish(self, envelope) -> None:
        for queue in self._queues:
            while True:
                try:
                    queue.put_nowait(envelope)
                    break
                except asyncio.QueueFull:
                    queue.get_nowait()  # drop oldest


class Session:
    def __init__(
        self,
        base: Path,
        session_id: SessionId,
        *,
        parent: tuple[SessionId, int] | None = None,
        default_model: ModelId | None = None,
        start_seq: int = 0,
        redactors: "list[EventRedactor] | None" = None,
        _writer: EventLogWriter | None = None,
    ) -> None:
        self.id = session_id
        self.base = base
        self._parent = parent
        self._default_model = default_model
        self._writer = _writer or EventLogWriter(base, session_id)
        try:
            self.blobs = BlobStore(base / "sessions" / str(session_id) / "blobs")
        except BaseException:
            self._writer.close()
            raise
        self.bus = SubscriberBus()
        self._seq = start_seq
        self._closed = False
        self._redactors: list = list(redactors or [])
        # Optional derived task view, seeded from replay on first access. Keep
        # ordinary status/preparation off the log-reading path after that point.
        self._task_state = None
        # One live owner prevents independent dispatchers from resetting admission.
        self._execution_ledger = None
        self._execution_budget = None
        self._experience_project = None
        self._experience_run = False

    def observe_project_scope(self, event):
        """Recover the single-project prefix across starts, including older native bindings."""
        from harness.project_examples import projected_scope
        self._experience_project = projected_scope(self._experience_project, event)
        if isinstance(event, (SessionStarted, SessionResumed)):
            self._experience_run = False
        elif isinstance(event, AgentRunStarted) and event.parent_run_id is None:
            self._experience_run = bool(self._experience_project)

    def start(self, *, project=None) -> Envelope:
        if self._seq != 0:
            raise RuntimeError("Session.start() already called")
        parent_id, parent_seq = self._parent if self._parent else (None, None)
        return self.append(
            SessionStarted(
                parent_session_id=parent_id,
                parent_seq=parent_seq,
                default_model=self._default_model,
                project=project,
            )
        )

    def append(self, event: Event) -> Envelope:
        for redactor in self._redactors:
            event = redactor(event)  # frozen events: redactors must model_copy(update=...)
        if isinstance(event, ImprovementRecorded) and event.record.kind in {"prompt_change", "source_change"}:
            from harness.improvement_journal import validate_selection
            validate_selection(self, event.record)
        # _seq advances before the write and is not rolled back on failure: a failed write leaves a gap, never a duplicate
        self._seq += 1
        envelope = Envelope(session_id=self.id, seq=self._seq, ts=time.time(), event=event)
        self._writer.append(envelope)  # source of truth first
        if isinstance(event, (SessionStarted, SessionResumed, AgentRunStarted)):
            self.observe_project_scope(event)
        elif self._experience_run and (isinstance(event, (AgentRunFinished, TaskChecked)) or
                isinstance(event, ImprovementRecorded) and event.record.kind == "evidence"):
            from harness.project_examples import index_prefix
            try:
                index_prefix(self.base, self._experience_project, self.id, self._writer.path.stat().st_size, envelope.seq)
            except (OSError, ValueError):
                # An unavailable derived index must not invalidate a committed event.
                pass
        if self._task_state is not None:
            self._task_state.apply(envelope)
        self.bus.publish(envelope)  # observers second; never blocking
        return envelope

    def close(self) -> None:
        self._writer.close()
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_seq(self) -> int:
        return self._seq

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
