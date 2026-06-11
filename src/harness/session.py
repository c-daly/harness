"""Session runtime: seq stamping, log append (sync, source of truth), subscriber fan-out."""

import asyncio
import time
from pathlib import Path

from harness.blobs import BlobStore
from harness.events import Envelope, Event, SessionStarted
from harness.log import EventLogWriter
from harness.types import ModelId, SessionId


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
    ) -> None:
        self.id = session_id
        self.base = base
        self._parent = parent
        self._default_model = default_model
        self._writer = EventLogWriter(base, session_id)
        self.blobs = BlobStore(base / "sessions" / str(session_id) / "blobs")
        self.bus = SubscriberBus()
        self._seq = start_seq
        self._closed = False

    def start(self) -> Envelope:
        if self._seq != 0:
            raise RuntimeError("Session.start() already called")
        parent_id, parent_seq = self._parent if self._parent else (None, None)
        return self.append(
            SessionStarted(
                parent_session_id=parent_id,
                parent_seq=parent_seq,
                default_model=self._default_model,
            )
        )

    def append(self, event: Event) -> Envelope:
        # _seq advances before the write and is not rolled back on failure: a failed write leaves a gap, never a duplicate
        self._seq += 1
        envelope = Envelope(session_id=self.id, seq=self._seq, ts=time.time(), event=event)
        self._writer.append(envelope)   # source of truth first
        self.bus.publish(envelope)      # observers second; never blocking
        return envelope

    def close(self) -> None:
        self._writer.close()
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
