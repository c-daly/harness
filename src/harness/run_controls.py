"""Operator cancellation of individual owned runs, with subtree cleanup.

Cancellation is cooperative. The request is durable before delivery; it is not
proof that a provider or its effects have stopped. No control survives restart.
"""

import asyncio
from dataclasses import dataclass
import re

from harness.task_cleanup import await_owned


class OperatorRunCancelled(Exception):
    """An explicit stop settled locally; callers receive a cancelled outcome."""


@dataclass(frozen=True)
class RunControlStatus:
    run_id: str
    task_id: str
    session_id: str
    runtime: str
    parent_run_id: str | None
    cancellation_requested: bool
    cancellation_blocked: str | None


@dataclass
class _RunControl:
    session: object
    task_id: str
    run_id: str
    runtime: str
    parent_run_id: str | None
    worker: asyncio.Task
    caller: asyncio.Task
    prior_cancels: int
    requested: bool = False

    def refusal(self):
        if (self.requested or self.worker.done() or self.worker.cancelling()
                or self.caller.cancelling() > self.prior_cancels):
            return "run is finishing or cancelling"
        return None

    def status(self):
        return RunControlStatus(self.run_id, self.task_id, str(self.session.id), self.runtime,
                                self.parent_run_id, self.requested, self.refusal())


class RunControls:
    """Shared live registry. Only its bound root operator can request a stop."""

    def __init__(self):
        self.root = None
        self._runs = {}

    def attach(self, root):
        if self.root is not None and self.root is not root:
            raise ValueError("run controls cannot change root ownership")
        self.root = root

    def snapshot(self) -> tuple[RunControlStatus, ...]:
        return tuple(run.status() for run in self._runs.values())

    def stopping(self, run_id):
        run = self._runs.get(run_id)
        return run is not None and run.refusal() is not None

    def check_active(self):
        """A swallowed cancellation cannot admit fresh work in a stopped subtree."""
        from harness.agent import current_agent_run
        active = current_agent_run.get()
        run = self._runs.get(active.run_id) if active else None
        while run is not None:
            if run.requested:
                raise asyncio.CancelledError()
            run = self._runs.get(run.parent_run_id)

    async def run(self, *, session, task, run_id, runtime, parent_run_id, execute):
        # A separate body task makes cancellation local even for a sequential
        # child or a typed external runtime nested inside the native root.
        caller = asyncio.current_task()
        worker = asyncio.create_task(execute(), name=f"harness-run-{run_id}")
        run = _RunControl(session, task.id, run_id, runtime, parent_run_id, worker,
                          caller, caller.cancelling())
        self._runs[run_id] = run
        try:
            try:
                result = await await_owned(worker)
            except asyncio.CancelledError:
                if caller.cancelling() > run.prior_cancels:
                    raise  # Parent interruption/deadline still owns this subtree.
                if run.requested:
                    raise OperatorRunCancelled() from None
                raise
            if run.requested:
                # A provider that swallowed cancellation cannot report success.
                raise OperatorRunCancelled()
            return result
        finally:
            self._runs.pop(run_id, None)

    def cancel(self, session, run_id):
        if session is not self.root:
            raise ValueError("only the owning root session can cancel agent runs")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{8,32}", run_id):
            raise ValueError("use a run ID or an unambiguous prefix of at least 8 characters")
        matches = [run for key, run in self._runs.items() if key.startswith(run_id)]
        if len(matches) != 1:
            raise ValueError("run ID does not identify one live run in this session tree")
        run = matches[0]
        if asyncio.get_running_loop() is not run.worker.get_loop():
            raise ValueError("run cancellation requires the owning event loop")
        if reason := run.refusal():
            raise ValueError(reason)
        from harness.events import AgentRunCancelRequested
        event = AgentRunCancelRequested(run_id=run.run_id, task_id=run.task_id,
                                        target_session_id=run.session.id)
        if session.append(event).event != event:
            raise ValueError("run cancellation requests cannot be rewritten")
        # No await between admission, durable intent and cancellation delivery.
        run.requested = True
        run.worker.cancel()
        return run.status()


def cancel_run(kernel, run_id):
    """Operator entry point, never registered as a model or plugin tool."""
    from harness.agent import current_agent_run
    from harness.execution import current_scope
    scope = kernel.loop.dispatcher.scope
    if scope.depth or current_scope.get() is not None or current_agent_run.get() is not None:
        raise ValueError("only a root operator can cancel an agent run")
    return scope.budget.controls.cancel(kernel.session, run_id)


def render_run_controls(controls):
    from harness.telemetry import _safe
    rows = controls.snapshot()
    if not rows:
        return "No live agent runs to cancel."
    lines = ["Agent run controls (cancel the selected run and its descendants):"]
    for row in rows[:50]:
        action = row.cancellation_blocked or f"/activity cancel {row.run_id}"
        lines.append(f"  {row.runtime} | session {row.session_id} | run {row.run_id} | {action}")
    if len(rows) > 50:
        lines.append(f"  {len(rows) - 50} more agent runs omitted from display.")
    lines.append("Cancellation waits for cleanup; siblings can continue. Recorded effects still need inspection.")
    return _safe("\n".join(lines))
