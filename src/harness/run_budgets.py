"""Operator grants for live root task timers, separate from activity observation.

Grants are written before rescheduling on the owning event loop. They never
become session defaults, child authority, or timers restored after a restart.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class RunBudgetStatus:
    run_id: str
    task_id: str
    session_id: str
    timeout_seconds: float
    remaining_seconds: float
    extension_blocked: str | None


@dataclass
class _RunBudget:
    session: object
    task_id: str
    run_id: str
    timer: asyncio.Timeout
    owner: asyncio.Task
    observation: object
    timeout_seconds: float
    blocked: str | None

    def refusal(self):
        if self.owner.done() or self.owner.cancelling() or self.timer.expired():
            return "run is finishing or cancelling"
        if self.timer.when() <= self.owner.get_loop().time():
            return "task time budget has already elapsed"
        return self.blocked


class RunBudgets:
    """Live task timers shared by one session tree. Access on the owning loop."""

    def __init__(self):
        self._runs: dict[str, _RunBudget] = {}

    @contextmanager
    def track(self, *, session, task, run_id, timer, observation, blocked):
        run = _RunBudget(session, task.id, run_id, timer, asyncio.current_task(),
                         observation, task.limits.timeout_seconds, blocked)
        self._runs[run_id] = run
        try:
            yield run
        finally:
            self._runs.pop(run_id, None)

    def snapshot(self, session_id) -> tuple[RunBudgetStatus, ...]:
        return tuple(RunBudgetStatus(
            run_id=run.run_id, task_id=run.task_id, session_id=str(run.session.id),
            timeout_seconds=run.timeout_seconds,
            remaining_seconds=max(0, run.timer.when() - run.owner.get_loop().time()),
            extension_blocked=run.refusal(),
        ) for run in self._runs.values() if run.session.id == session_id)

    def extend(self, session, run_id: str, seconds: float) -> RunBudgetStatus:
        """Grant additional finite time to a specifically identified live run."""
        if type(seconds) not in (int, float):
            raise ValueError("additional seconds must be positive and finite")
        try:
            seconds = float(seconds)
        except OverflowError:
            raise ValueError("additional seconds must be positive and finite") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("additional seconds must be positive and finite")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{8,32}", run_id):
            raise ValueError("use a run ID or an unambiguous prefix of at least 8 characters")
        matches = [run for key, run in self._runs.items()
                   if key.startswith(run_id) and run.session is session]
        if len(matches) != 1:
            raise ValueError("run ID does not identify one live run in this session")
        run = matches[0]
        if asyncio.get_running_loop() is not run.owner.get_loop():
            raise ValueError("task extensions require the owning event loop")
        if reason := run.refusal():
            raise ValueError(reason)
        total = run.timeout_seconds + seconds
        deadline = run.timer.when() + (total - run.timeout_seconds)
        if (not math.isfinite(total) or not math.isfinite(deadline)
                or total <= run.timeout_seconds or deadline <= run.timer.when()):
            raise ValueError("extension must produce a larger finite budget")
        from harness.events import TaskBudgetExtended
        event = TaskBudgetExtended(run_id=run.run_id, task_id=run.task_id,
            previous_timeout_seconds=run.timeout_seconds, timeout_seconds=total)
        # No await between admission, durable grant, and rescheduling. The
        # original deadline is checked before the synchronous append; slow
        # persistence can consume part of the newly granted time.
        session.append(event)
        run.timer.reschedule(deadline)
        run.timeout_seconds = total
        # Updating the displayed budget is not evidence of execution activity.
        run.observation.deadline = deadline
        return next(row for row in self.snapshot(session.id) if row.run_id == run.run_id)


def extension_block(task, scope) -> str | None:
    """Capture authority before bound_task materializes all default fields."""
    from harness.agent import current_agent_run
    from harness.handoff import current_handoff
    if task.handoff_id is not None or current_handoff.get() is not None:
        return "handoff task limits cannot be extended"
    if scope.depth or current_agent_run.get() is not None:
        return "delegated task limits cannot be extended"
    if "timeout_seconds" in task.limits.model_fields_set:
        return "explicit or replayed task timeouts cannot be extended"
    return None


def extend_execution(kernel, run_id, seconds):
    """Operator entry point; this is not a model tool or an idle settings edit."""
    from harness.agent import current_agent_run
    from harness.execution import current_scope
    scope = kernel.loop.dispatcher.scope
    if scope.depth or current_scope.get() is not None or current_agent_run.get() is not None:
        raise ValueError("only a root operator can extend a task budget")
    return scope.budget.runs.extend(kernel.session, run_id, seconds)


def render_run_budgets(scope) -> str:
    rows = scope.budget.runs.snapshot(scope.session.id)
    if not rows:
        return "No live task timers in this session."
    lines = ["Live task timers (extensions affect this run only):"]
    for row in rows:
        control = row.extension_blocked or f"/execution extend {row.run_id[:8]} 300"
        lines.append(f"  run {row.run_id}: {row.timeout_seconds:g}s total; "
                     f"{row.remaining_seconds:.1f}s remaining; {control}")
    lines.append("Model requests, external processes, context, children, and coordinators keep their own caps; "
                 "/activity shows observed earlier deadlines.")
    return "\n".join(lines)
