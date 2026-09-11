"""Durable admission counts shared by one session tree.

Reservations measure admitted attempts, including denied tools, failed calls,
retries and work interrupted before execution. Active capacity stays live-only.
Legacy root records provide conservative counts; missing descendant accounting
holds admission. No child log is needed to restore a tracked root's totals.
"""

from dataclasses import dataclass, replace
from pathlib import Path

COUNTERS = ("model_calls", "tool_calls", "children")
EVENT_TYPES = {"execution_counts_recorded", "execution_counts_linked"}


@dataclass
class ExecutionCounts:
    model_calls: int = 0
    tool_calls: int = 0
    children: int = 0
    legacy: bool = False
    incomplete: bool = False
    recording: bool = False
    root_session_id: str | None = None
    parent_session_id: str | None = None

    def values(self):
        return {name: getattr(self, name) for name in (*COUNTERS, "legacy", "incomplete")}

    def check_root(self):
        if self.root_session_id is not None:
            raise ValueError(f"shared execution accounting belongs to session {self.root_session_id}; resume that root session")
        owner = self.parent_session_id
        if owner is not None:
            raise ValueError(f"shared execution accounting belongs to an ancestor session ({owner}); resume the root session")

    def apply(self, event):
        from harness.events import (CoordinationStarted, ExecutionCountsLinked, ExecutionCountsRecorded,
            ModelCallStarted, RetryAttempted, SessionResumed, SessionStarted, SubagentSpawned,
            ToolCallProposed, UnknownEvent)
        if isinstance(event, UnknownEvent) and event.raw.get("type") in EVENT_TYPES:
            raise ValueError("invalid stored execution counts; refusing to reset consumption")
        if isinstance(event, SessionStarted):
            self.parent_session_id = event.parent_session_id
        elif isinstance(event, SessionResumed):
            # Older binaries preserve these events but cannot record reservations.
            # Their subsequent work must be counted as legacy on upgrade again.
            self.recording = False
        elif isinstance(event, ExecutionCountsLinked):
            if self.recording or any(getattr(self, name) for name in COUNTERS):
                raise ValueError("execution ownership link conflicts with root counts")
            if self.root_session_id not in (None, event.root_session_id):
                raise ValueError("execution ownership cannot change roots")
            self.root_session_id = str(event.root_session_id)
        elif isinstance(event, ExecutionCountsRecorded):
            self.check_root()
            expected = self.values()
            if event.kind in ("model", "tool", "child", "coordinator"):
                if not self.recording or self.incomplete:
                    raise ValueError("execution reservation lacks complete root accounting")
                counter = f"{event.kind}_calls" if event.kind in ("model", "tool") else "children"
                expected[counter] += 1
            elif event.kind == "retain":
                if not self.recording:
                    raise ValueError("retained counts lack root accounting")
                for name in COUNTERS:
                    expected[name] = max(expected[name], getattr(event, name))
            if any(getattr(event, name) != value for name, value in expected.items()):
                raise ValueError("stored execution counts do not follow prior reservations")
            for name in COUNTERS:
                setattr(self, name, expected[name])
            self.recording = True
        elif not self.recording and self.root_session_id is None:
            if isinstance(event, (ModelCallStarted, RetryAttempted)):
                self.model_calls += 1
                self.legacy = True
            elif isinstance(event, ToolCallProposed):
                self.tool_calls += 1
                self.legacy = True
            elif isinstance(event, (SubagentSpawned, CoordinationStarted)):
                self.children += 1
                self.legacy = True
                if isinstance(event, SubagentSpawned):
                    self.incomplete = True


def project_counts(events):
    state = ExecutionCounts()
    for envelope in events:
        state.apply(envelope.event)
    return state


class ExecutionLedger:
    def __init__(self):
        self.state = ExecutionCounts()
        self.session = None
        self.linked = set()
        self.healthy = True

    def _check(self):
        from harness.execution import BudgetExceeded
        if not self.healthy:
            raise BudgetExceeded("root execution budget: accounting write failed; restart to reconcile the ledger")

    def attach(self, session):
        from harness.events import ExecutionCountsLinked
        from harness.log import read_session
        self._check()
        if session._execution_ledger is not None and session._execution_ledger is not self:
            raise ValueError("session already has an execution ledger; share its execution scope")
        if self.session is None:
            # Standalone embedders may have reserved in-memory capacity first.
            retained = self.state.values()
            state = project_counts(read_session(session.base, session.id, repair=False)) if session._seq else ExecutionCounts()
            state.check_root()
            session._execution_ledger = self
            self.session, self.state = session, state
            self.record("attach", session)
            self.retain(**{name: retained[name] for name in COUNTERS})
        elif session is not self.session and session.id == self.session.id:
            raise ValueError("execution accounting cannot share ownership with another root writer")
        elif session is not self.session and session.id not in self.linked:
            try:
                event = ExecutionCountsLinked(root_session_id=self.session.id)
                if session.append(event).event != event:
                    raise ValueError("execution accounting records cannot be rewritten")
                session._execution_ledger = self
                self.linked.add(session.id)
            except BaseException:
                self.healthy = False
                raise

    def record(self, kind, source=None, *, call_id=None, **counts):
        from harness.events import ExecutionCountsRecorded
        self._check()
        source = source or self.session
        values = self.state.values() | counts
        event = ExecutionCountsRecorded(kind=kind, source_session_id=source.id if source else "unattached",
                                        call_id=call_id, **values)
        projected = replace(self.state)
        projected.apply(event)  # Validate before writing; never mutate on append failure.
        try:
            if self.session is not None and self.session.append(event).event != event:
                raise ValueError("execution accounting records cannot be rewritten")
        except BaseException:
            self.healthy = False
            raise
        self.state = projected

    def reserve(self, kind, source=None, *, call_id=None):
        from harness.execution import BudgetExceeded
        self._check()
        if source is not None:
            self.attach(source)
        elif not self.state.recording:
            self.record("attach")  # Pure in-memory embedders have no persistence claim.
        if self.state.incomplete:
            raise BudgetExceeded("root execution budget: legacy descendant counts are incomplete; inspect /execution and start a new session")
        counter = f"{kind}_calls" if kind in ("model", "tool") else "children"
        self.record(kind, source, call_id=call_id, **{counter: getattr(self.state, counter) + 1})

    def retain(self, **counts):
        values = {name: max(getattr(self.state, name), counts.get(name, 0)) for name in COUNTERS}
        if any(value != getattr(self.state, name) for name, value in values.items()):
            self.record("retain", self.session, **values)


def counts_snapshot(base, session_id, *, events=None):
    from harness.log import read_session
    if events is None:
        events = read_session(base, session_id, repair=False)
    state = project_counts(events)
    root = state.root_session_id or str(session_id)
    if state.root_session_id is not None:
        if Path(root).name != root or root in (".", ".."):
            raise ValueError("invalid execution root session")
        events = read_session(base, root, repair=False)
        state = project_counts(events)
        state.check_root()
    if state.parent_session_id is not None and state.root_session_id is None:
        root = None  # A legacy child cannot establish the tree root or its total work.
        state.incomplete = True
    return {"root_session_id": root, "through_seq": events[-1].seq if events else 0,
            "recording": state.recording, **state.values()}
