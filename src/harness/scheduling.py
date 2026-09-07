"""Bounded admission for declared local device groups within one session tree.

Reservations cover startup and the entire inference stream. Priority orders
waiting requests; it does not preempt a running model or control other programs.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from harness.errors import LocalBusy
from harness.types import CallId

Priority = Literal["interactive", "work", "background"]
_PRIORITY = {"interactive": 0, "work": 1, "background": 2}
_held_groups: ContextVar[frozenset] = ContextVar("local_held_groups", default=frozenset())


def request_priority(purpose: str, depth: int) -> Priority:
    if purpose.startswith(("semantic:", "evaluation:")):
        return "background"
    return "interactive" if depth == 0 and purpose in ("conversation", "compaction") else "work"


class LocalRequestObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    request_id: str
    call_id: CallId | None = None
    task_id: str | None = None
    run_id: str | None = None
    alias: str
    group: str
    priority: Priority
    status: Literal["queued", "acquired", "released", "cancelled", "rejected"]
    reason: Literal["capacity", "waiting", "finished", "failed", "cancelled", "background_busy",
                    "queue_full", "closing", "reentrant", "endpoint_group_changed"]
    wait_ms: float = Field(ge=0)


@dataclass(eq=False)
class _Ticket:
    fields: dict
    future: asyncio.Future
    priority: Priority
    started: float = field(default_factory=time.monotonic)
    wait_ms: float | None = None


@dataclass
class _Lane:
    active: _Ticket | None = None
    waiting: list[_Ticket] = field(default_factory=list)


class LocalScheduler:
    def __init__(self, *, max_waiting=32):
        if type(max_waiting) is not int or max_waiting < 0:
            raise ValueError("local queue limit must be a nonnegative integer")
        self.max_waiting = max_waiting
        self._lanes: dict[str, _Lane] = {}
        self._endpoints: dict[str, str] = {}
        self._closing = False

    def activity(self, group):
        lane = self._lanes.get(group)
        return (lane.active.fields["alias"] if lane and lane.active else None,
                len(lane.waiting) if lane else 0)

    def _wake(self, lane):
        while lane.active is None and lane.waiting and not self._closing:
            ticket = min(lane.waiting, key=lambda item: _PRIORITY[item.priority])
            lane.waiting.remove(ticket)
            if not ticket.future.done():
                lane.active = ticket
                ticket.future.set_result(None)

    def close(self):
        self._closing = True
        for lane in self._lanes.values():
            for ticket in lane.waiting:
                if not ticket.future.done():
                    ticket.future.set_exception(LocalBusy("local scheduler is closing"))
            lane.waiting.clear()

    @asynccontextmanager
    async def slot(self, resolved, *, emit, priority: Priority, call_id=None):
        from harness.agent import current_agent_run
        from harness.events import LocalRequestObserved

        if priority not in _PRIORITY:
            raise ValueError("unknown local request priority")
        group = resolved.local.resource_group
        active_run = current_agent_run.get()
        ticket = _Ticket(dict(request_id=uuid4().hex, call_id=call_id, alias=resolved.alias,
            group=group, priority=priority, task_id=active_run.task.id if active_run else None,
            run_id=active_run.run_id if active_run else None), asyncio.get_running_loop().create_future(), priority)

        def record(status, reason):
            emit(LocalRequestObserved(observation=LocalRequestObservation(**ticket.fields,
                status=status, reason=reason, wait_ms=ticket.wait_ms if ticket.wait_ms is not None
                else (time.monotonic() - ticket.started) * 1000)))

        lane = self._lanes.setdefault(group, _Lane())
        key = (self, group, ticket)
        reason = None
        if self._closing:
            reason = "closing"
        elif any(owner[:2] == key[:2] and lane.active is owner[2] for owner in _held_groups.get()):
            reason = "reentrant"
        elif self._endpoints.get(resolved.api_base, group) != group:
            reason = "endpoint_group_changed"
        elif lane.active or lane.waiting:
            if priority == "background":
                reason = "background_busy"
            elif sum(len(item.waiting) for item in self._lanes.values()) >= self.max_waiting:
                reason = "queue_full"
        if reason:
            record("rejected", reason)
            raise LocalBusy(f"local group {group}: {reason}")
        self._endpoints[resolved.api_base] = group
        token = None
        status, reason = "released", "finished"
        try:
            if lane.active is None and not lane.waiting:
                lane.active = ticket
                ticket.future.set_result(None)
            else:
                lane.waiting.append(ticket)
                record("queued", "waiting")
            await ticket.future
            ticket.wait_ms = (time.monotonic() - ticket.started) * 1000
            record("acquired", "capacity")
            token = _held_groups.set(_held_groups.get() | {key})
            yield
        except asyncio.CancelledError:
            status, reason = "cancelled", "cancelled"
            raise
        except BaseException:
            reason = "closing" if self._closing else "failed"
            if ticket.wait_ms is None:
                status = "cancelled"
            raise
        finally:
            if token is not None:
                _held_groups.reset(token)
            if ticket in lane.waiting:
                lane.waiting.remove(ticket)
            if lane.active is ticket:
                lane.active = None
            ticket.future.cancel()
            self._wake(lane)
            record(status, reason)


def render_local_request(observation, *, saved=False):
    from harness.telemetry import _safe
    text = (f"Local request {observation.alias}: {observation.status}; group {observation.group}; "
            f"{observation.priority}; {observation.reason.replace('_', ' ')}")
    if saved and observation.status in ("queued", "acquired"):
        text += "; recorded state, live admission unknown"
    return _safe(text + ".")
