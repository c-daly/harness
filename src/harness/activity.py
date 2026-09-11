"""Live core execution observations. Activity is not evidence of task completion.

Only active operations are retained. Monotonic times and context-local ownership
are deliberately not replayed from the event log or exported as live state.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
from uuid import uuid4


@dataclass(frozen=True)
class ActivityStatus:
    id: str
    parent_id: str | None
    session_id: str
    task_id: str | None
    run_id: str | None
    call_id: str | None
    kind: str
    label: str
    phase: str
    elapsed_seconds: float
    phase_seconds: float
    quiet_seconds: float
    last_signal: str
    stream_events: int
    remaining_seconds: float | None


current_activity: ContextVar["LiveActivity | None"] = ContextVar("harness_activity", default=None)


class LiveActivity:
    def __init__(self, tracker, *, parent, session_id, kind, label, phase, task_id, run_id, call_id):
        self.tracker = tracker
        self.id = uuid4().hex
        self.parent = parent
        self.session_id = str(session_id)
        self.task_id = task_id or (parent.task_id if parent else None)
        self.run_id = run_id or (parent.run_id if parent else None)
        self.call_id = call_id or (parent.call_id if parent else None)
        self.kind, self.label, self.phase = kind, label, phase
        self.started = self.phase_started = self.last_activity = tracker.clock()
        self.last_signal = "started"
        self.stream_events = 0
        self.deadline = None

    def touch(self, signal, *, stream=False):
        """Retain metadata only; late callbacks cannot resurrect a closed operation."""
        if self.id not in self.tracker._active:
            return
        now = self.tracker.clock()
        self.stream_events += int(stream)
        self.last_activity, self.last_signal = now, signal
        parent = self.parent
        while parent is not None and parent.id in self.tracker._active:
            parent.last_activity, parent.last_signal = now, "child activity"
            parent = parent.parent

    def update(self, *, phase=None, label=None, deadline=None):
        if phase is not None and phase != self.phase:
            self.phase, self.phase_started = phase, self.tracker.clock()
        if label is not None:
            self.label = label
        if deadline is not None:
            self.deadline = deadline
        self.touch("phase changed")


class ActivityTracker:
    """Shared by the live session tree; synchronous access on its owning loop."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._active: dict[str, LiveActivity] = {}

    @contextmanager
    def track(self, *, session_id, kind, label, phase, task_id=None, run_id=None, call_id=None, timeout=None):
        parent = current_activity.get()
        if parent is not None and (parent.tracker is not self or parent.id not in self._active):
            parent = None
        operation = LiveActivity(self, parent=parent, session_id=session_id, kind=kind,
                                 label=label, phase=phase, task_id=task_id, run_id=run_id, call_id=call_id)
        if timeout is not None:
            operation.deadline = operation.started + timeout
        self._active[operation.id] = operation
        operation.touch("started")
        token = current_activity.set(operation)
        try:
            yield operation
        finally:
            operation.touch("finished")
            self._active.pop(operation.id, None)
            current_activity.reset(token)

    def snapshot(self) -> tuple[ActivityStatus, ...]:
        now = self.clock()
        rows = []
        for operation in self._active.values():
            deadlines = []
            ancestor = operation
            while ancestor is not None:
                if ancestor.deadline is not None:
                    deadlines.append(ancestor.deadline)
                ancestor = ancestor.parent
            rows.append(ActivityStatus(
                id=operation.id, parent_id=operation.parent.id if operation.parent else None,
                session_id=operation.session_id, task_id=operation.task_id, run_id=operation.run_id,
                call_id=operation.call_id,
                kind=operation.kind, label=operation.label, phase=operation.phase,
                elapsed_seconds=max(0, now - operation.started),
                phase_seconds=max(0, now - operation.phase_started),
                quiet_seconds=max(0, now - operation.last_activity),
                last_signal=operation.last_signal, stream_events=operation.stream_events,
                remaining_seconds=max(0, min(deadlines) - now) if deadlines else None,
            ))
        return tuple(rows)


@contextmanager
def waiting(reason):
    """A concurrent wait gets its own row; sibling activity cannot erase it."""
    parent = current_activity.get()
    if parent is None or parent.id not in parent.tracker._active:
        yield
    else:
        with parent.tracker.track(session_id=parent.session_id, kind="wait", label=reason, phase="waiting"):
            yield


def render_activity(tracker) -> str:
    from harness.telemetry import _safe

    rows = tracker.snapshot()
    if not rows:
        return "No active core operations in this process. Saved runs do not establish live activity."
    lines = ["Live activity (this process and its descendants):"]
    for row in rows[:50]:
        budget = (f"{row.remaining_seconds:.0f}s remaining in observed budgets"
                  if row.remaining_seconds is not None else "budget not observed")
        if row.remaining_seconds == 0:
            budget = "observed budget elapsed; operation or cleanup still active"
        lines += [
            f"  {row.kind} {row.label} [{row.id[:8]}] | {row.phase} ({row.phase_seconds:.0f}s)",
            f"    elapsed {row.elapsed_seconds:.0f}s; last activity {row.quiet_seconds:.0f}s ago "
            f"({row.last_signal}); stream events {row.stream_events}; {budget}",
            f"    session {row.session_id}; task {row.task_id or 'none'}; run {row.run_id or 'none'}; "
            f"call {row.call_id or 'none'}; parent {row.parent_id or 'none'}",
        ]
    if len(rows) > 50:
        lines.append(f"  {len(rows) - 50} more active operations omitted from display.")
    lines.append("Activity does not verify progress. Silence does not establish a hang. "
                 "Unobserved provider work may continue; /execution shows configured budgets.")
    return _safe("\n".join(lines))


def activity_summary(tracker) -> str:
    rows = tracker.snapshot()
    if not rows:
        return ""
    elapsed = max(row.elapsed_seconds for row in rows)
    quiet = min(row.quiet_seconds for row in rows)
    waits = [row for row in rows if row.kind == "wait"]
    phase = f"{len(waits)} waiting: {waits[0].label}" if waits else rows[-1].phase
    from harness.telemetry import _safe
    return _safe(f"Active {len(rows)} | {elapsed:.0f}s elapsed | activity {quiet:.0f}s ago\n"
                 f"{phase} | /activity")
