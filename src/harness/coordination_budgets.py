"""Audited operator grants for live core coordinator timers, never replayed."""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class CoordinationBudgetStatus:
    id: str
    strategy: str
    session_id: str | None
    run_id: str | None
    timeout_seconds: float
    remaining_seconds: float
    extension_blocked: str | None


@dataclass
class _CoordinationBudget:
    session: object
    id: str
    strategy: str
    run_id: str | None
    timer: asyncio.Timeout
    owner: asyncio.Task
    observation: object
    timeout_seconds: float
    blocked: str | None

    def refusal(self):
        if self.owner.done() or self.owner.cancelling() or self.timer.expired():
            return "coordinator is finishing or cancelling"
        if self.session is not None:
            budget = self.session._execution_budget
            if budget is not None and budget.controls.stopping(self.run_id):
                return "enclosing agent run is finishing or cancelling"
        if self.timer.when() <= self.owner.get_loop().time():
            return "coordinator time budget has already elapsed"
        return self.blocked

    def status(self):
        return CoordinationBudgetStatus(self.id, self.strategy,
            str(self.session.id) if self.session is not None else None, self.run_id,
            self.timeout_seconds, max(0, self.timer.when() - self.owner.get_loop().time()),
            self.refusal())


class CoordinationBudgets:
    """One live session-tree registry; only its owning root operator can grant time."""

    def __init__(self):
        self.root = None
        self._coordinations = {}

    def attach(self, root):
        if self.root is not None and self.root is not root:
            raise ValueError("coordinator budgets cannot change root ownership")
        self.root = root

    @contextmanager
    def track(self, *, session, id, strategy, timer, observation, timeout_seconds):
        from harness.agent import current_agent_run
        from harness.handoff import current_handoff
        active = current_agent_run.get()
        blocked = ("handoff coordinator timers cannot be extended" if current_handoff.get() is not None
                   else "coordinator has no owning session" if session is None else None)
        run = _CoordinationBudget(session, id, strategy, active.run_id if active else None,
            timer, asyncio.current_task(), observation, timeout_seconds, blocked)
        self._coordinations[id] = run
        try:
            yield run
        finally:
            self._coordinations.pop(id, None)

    def snapshot(self) -> tuple[CoordinationBudgetStatus, ...]:
        return tuple(run.status() for run in self._coordinations.values())

    def extend(self, session, coordination_id, seconds):
        if self.root is None or session is not self.root:
            raise ValueError("only the owning root session can extend coordinator budgets")
        if type(seconds) not in (int, float):
            raise ValueError("additional seconds must be positive and finite")
        try:
            seconds = float(seconds)
        except OverflowError:
            raise ValueError("additional seconds must be positive and finite") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("additional seconds must be positive and finite")
        if not isinstance(coordination_id, str) or not re.fullmatch(r"[0-9a-f]{8,32}", coordination_id):
            raise ValueError("use a coordinator ID or an unambiguous prefix of at least 8 characters")
        matches = [run for key, run in self._coordinations.items() if key.startswith(coordination_id)]
        if len(matches) != 1:
            raise ValueError("coordinator ID does not identify one live coordinator in this session tree")
        run = matches[0]
        if asyncio.get_running_loop() is not run.owner.get_loop():
            raise ValueError("coordinator extensions require the owning event loop")
        if reason := run.refusal():
            raise ValueError(reason)
        total = run.timeout_seconds + seconds
        deadline = run.timer.when() + (total - run.timeout_seconds)
        if (not math.isfinite(total) or not math.isfinite(deadline)
                or total <= run.timeout_seconds or deadline <= run.timer.when()):
            raise ValueError("extension must produce a larger finite budget")
        from harness.events import CoordinationBudgetExtended
        event = CoordinationBudgetExtended(coordination_id=run.id, target_session_id=run.session.id,
            previous_timeout_seconds=run.timeout_seconds, timeout_seconds=total)
        # Admission, durable intent and timer rescheduling share one event-loop
        # turn. Persistence time consumes part of the grant; expiry cannot revive work.
        if session.append(event).event != event:
            raise ValueError("coordinator budget grants cannot be rewritten")
        run.timer.reschedule(deadline)
        run.timeout_seconds = total
        run.observation.deadline = deadline  # A grant is not an activity signal.
        return run.status()


def extend_coordination(kernel, coordination_id, seconds):
    """Root operator entry point, not a model or plugin tool."""
    from harness.agent import current_agent_run
    from harness.execution import current_scope
    scope = kernel.loop.dispatcher.scope
    if scope.depth or current_scope.get() is not None or current_agent_run.get() is not None:
        raise ValueError("only a root operator can extend a coordinator budget")
    return scope.budget.coordinations.extend(kernel.session, coordination_id, seconds)


def render_coordination_budgets(budget):
    from harness.telemetry import _safe
    rows = budget.coordinations.snapshot()
    if not rows:
        return "No live coordinator timers in this session tree."
    lines = ["Live coordinator timers (each grant changes only the selected coordinator):"]
    for row in rows[:50]:
        action = row.extension_blocked or f"/execution extend-coordinator {row.id} 300"
        lines.append(f"  {row.strategy} {row.id} | session {row.session_id} | "
                     f"{row.timeout_seconds:g}s total; {row.remaining_seconds:.1f}s remaining | {action}")
    if len(rows) > 50:
        lines.append(f"  {len(rows) - 50} more coordinator timers omitted from display.")
    lines.append("Enclosing tasks, member agents, model calls and other coordinators retain their own caps.")
    return _safe("\n".join(lines))
