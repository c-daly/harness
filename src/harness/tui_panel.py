"""The activity panel: Files / Agents / Workflows tabs over the session's own
event stream.

Kept out of tui.py on purpose (one responsibility). The fold helpers
(`fold_files`, `fold_agents`) are pure functions over a list of envelopes --
no Textual, no side effects, no hooks -- so they're testable the same way
harness.fold is: feed synthetic envelopes in, get rows out. `ActivityPanel`
is the thin Textual widget that renders whatever they compute; it holds no
fold logic of its own, only the DOM plumbing (tabs, refresh timing, the one
place -- the agent-swarm section -- that reaches back out through a caller
-supplied dispatch callback).

Coordination-call attribution (the "strategy" grouping key on AgentRow) is
exact: SubagentSpawned carries the call_id of the tool call that caused it,
so coordination calls running concurrently in one turn stay correctly
separated.
"""

from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static, TabbedContent, TabPane

from harness.events import (
    CoordinationFinished,
    Envelope,
    SubagentFinished,
    SubagentSpawned,
    ToolCallCompleted,
    ToolCallProposed,
)

# --- Files tab ---------------------------------------------------------

_FILE_MARKERS = {"read_file": "R", "write_file": "W", "edit_file": "E"}


@dataclass(frozen=True)
class FileRow:
    path: str
    markers: frozenset[str]


def fold_files(events: list[Envelope]) -> list[FileRow]:
    """One row per path touched by a successful read_file/write_file/edit_file
    call, newest-touched first, markers accumulated across every touch of
    that path (a later edit of an already-read path adds E to the SAME row
    rather than opening a second one). Failed calls (is_error) never mark."""
    intents: dict[str, tuple[str, str]] = {}  # call_id -> (marker, path)
    markers: dict[str, set[str]] = {}
    order: list[str] = []  # touch order, oldest first; reversed on output

    for env in events:
        ev = env.event
        if isinstance(ev, ToolCallProposed):
            marker = _FILE_MARKERS.get(str(ev.tool))
            if marker is None:
                continue
            path = ev.args.get("file_path")
            if path is None:
                continue
            intents[str(ev.call_id)] = (marker, str(path))
        elif isinstance(ev, ToolCallCompleted):
            touched = intents.pop(str(ev.call_id), None)
            if touched is None or ev.is_error:
                continue
            marker, path = touched
            if path in markers:
                order.remove(path)
            else:
                markers[path] = set()
            markers[path].add(marker)
            order.append(path)

    return [FileRow(path=p, markers=frozenset(markers[p])) for p in reversed(order)]


# --- Agents tab ----------------------------------------------------------

_DISPATCH_TOOL = "dispatch_agent"
_STRATEGY_TOOLS = frozenset({"ensemble", "consult_panel", "escalate"})


@dataclass(frozen=True)
class AgentRow:
    call_id: str
    label: str
    status: str  # running | done | error | incomplete | cancelled
    model: "str | None"
    strategy: "str | None"  # grouping key shared by one coordination call's experts


def fold_agents(events: list[Envelope]) -> list[AgentRow]:
    """One row per dispatch_agent call (child outcome takes precedence over a
    successful tool return) plus one row per expert spawned by an
    open ensemble/consult_panel/escalate call (status from that expert's own
    SubagentSpawned/SubagentFinished, `strategy` set to the coordination
    call's call_id so experts of the same call share a grouping key). Recorded
    aggregate outcomes get a separate result row in that same strategy group.
    Newest-touched first."""
    rows: dict[str, AgentRow] = {}
    order: list[str] = []
    open_dispatch: set[str] = set()
    strategy_calls: set[str] = set()  # ensemble/consult_panel/escalate call_ids seen
    child_rows: dict[str, str] = {}

    def upsert(call_id: str, **changes) -> None:
        if call_id not in rows:
            order.append(call_id)
            rows[call_id] = AgentRow(
                call_id=call_id, label=call_id, status="running", model=None, strategy=None
            )
        rows[call_id] = replace(rows[call_id], **changes)

    for env in events:
        ev = env.event
        if isinstance(ev, ToolCallProposed):
            tool = str(ev.tool)
            call_id = str(ev.call_id)
            if tool == _DISPATCH_TOOL:
                agent = ev.args.get("agent")
                model = ev.args.get("model")
                label = str(agent) if agent else (str(model) if model else _DISPATCH_TOOL)
                upsert(call_id, label=label, model=str(model) if model else None)
                open_dispatch.add(call_id)
            elif tool in _STRATEGY_TOOLS:
                strategy_calls.add(call_id)
        elif isinstance(ev, ToolCallCompleted):
            call_id = str(ev.call_id)
            if call_id in open_dispatch:
                open_dispatch.discard(call_id)
                if rows[call_id].status == "running" or ev.is_error:
                    upsert(call_id, status="error" if ev.is_error else "done")
        elif isinstance(ev, SubagentSpawned):
            group = str(ev.call_id) if ev.call_id is not None else None
            if group in open_dispatch:
                child_rows[str(ev.child_session_id)] = group
            # a dispatch_agent spawn already has its own row keyed by the
            # dispatch call; only coordination experts get a child-keyed row
            if group is not None and group in strategy_calls:
                child = str(ev.child_session_id)
                model = str(ev.model) if ev.model else None
                label = str(ev.agent) if ev.agent else (model or "(agent)")
                upsert(child, label=label, model=model, strategy=group)
        elif isinstance(ev, SubagentFinished):
            child = str(ev.child_session_id)
            row = child_rows.get(child, child)
            if row in rows:
                status = {"ok": "done", "error": "error", "cancelled": "cancelled",
                          "incomplete": "incomplete"}[ev.status]
                upsert(row, status=status)
        elif isinstance(ev, CoordinationFinished):
            status = {"completed": "done", "incomplete": "incomplete", "failed": "error",
                      "blocked": "error", "cancelled": "cancelled"}[ev.status]
            if ev.call_id in strategy_calls:
                upsert(str(ev.call_id), label=f"{ev.strategy} result", status=status, strategy=str(ev.call_id))
            elif ev.call_id in open_dispatch:
                upsert(str(ev.call_id), status=status)

    return [rows[cid] for cid in reversed(order)]


def _render_files(rows: list[FileRow]) -> str:
    if not rows:
        return "(no file activity yet)"
    return "\n".join(f"[{''.join(sorted(r.markers))}] {r.path}" for r in rows)


def _render_agents(rows: list[AgentRow]) -> str:
    if not rows:
        return "(no agent activity yet)"
    lines = []
    for r in rows:
        suffix = f"  (strategy {r.strategy})" if r.strategy else ""
        lines.append(f"[{r.status}] {r.label}  model={r.model or '-'}{suffix}")
    return "\n".join(lines)


def _render_mixture(rows: list[AgentRow]) -> str:
    grouped = [r for r in rows if r.strategy is not None]
    if not grouped:
        return "(no ensemble/panel/escalate runs yet)"
    by_group: dict[str, list[AgentRow]] = {}
    for r in grouped:
        by_group.setdefault(r.strategy, []).append(r)
    lines = []
    for group, members in by_group.items():
        lines.append(f"strategy {group}:")
        lines.extend(f"  [{m.status}] {m.label}  model={m.model or '-'}" for m in members)
    return "\n".join(lines)


# --- the widget ------------------------------------------------------------


class ActivityPanel(Vertical):
    """Toggleable sidebar (hidden by default -- callers set/flip `.display`)
    with Files / Agents / Workflows tabs. Files/Agents render a pure local
    fold of every envelope this panel has been fed via `record()` -- no bus
    writes, no dispatch. The Workflows tab always carries the mixture
    section (grouped ensemble/consult_panel/escalate rows); the agent-swarm
    section exists ONLY when constructed with agent_swarm_enabled=True, and
    is fetched through `fetch_agent_swarm_state` on tab-open or the refresh
    key -- never on a timer.
    """

    DEFAULT_CSS = """
    ActivityPanel {
        dock: right;
        width: 44;
        border-left: solid $accent;
        background: $panel;
    }
    """

    BINDINGS = [Binding("r", "refresh_workflows", "Refresh workflows", show=False)]
    can_focus = True

    def __init__(
        self,
        *,
        agent_swarm_enabled: bool,
        fetch_agent_swarm_state: "Callable[[], Awaitable[str]] | None" = None,
        id: "str | None" = None,
    ) -> None:
        super().__init__(id=id)
        self.display = False  # hidden until the app toggles it on
        self._agent_swarm_enabled = agent_swarm_enabled
        self._fetch_agent_swarm_state = fetch_agent_swarm_state
        self._events: list[Envelope] = []

    def compose(self) -> ComposeResult:
        with TabbedContent(id="activity-tabs"):
            with TabPane("Files", id="tab-files"):
                yield Static(id="files-body", markup=False)
            with TabPane("Agents", id="tab-agents"):
                yield Static(id="agents-body", markup=False)
            with TabPane("Workflows", id="tab-workflows"):
                yield Static(id="workflows-mixture", markup=False)
                # Named test contract: this widget exists iff agent-swarm is
                # enabled -- absence, not an empty placeholder, is the signal.
                if self._agent_swarm_enabled:
                    yield Static(id="workflows-agent-swarm", markup=False)

    def record(self, envelope: Envelope) -> None:
        """Fed by the app's bus pump for every session event -- cheap
        bookkeeping only; no DOM touched, no fold recomputed here."""
        self._events.append(envelope)

    def reset(self) -> None:
        """A kernel rebuild (/clear, /resume) hands this session a brand new
        event history; the panel's Files/Agents view must not keep showing
        the OLD session's rows."""
        self._events = []
        self.refresh_files_and_agents()

    def refresh_files_and_agents(self) -> None:
        if not self.is_mounted:
            return
        files = fold_files(self._events)
        agents = fold_agents(self._events)
        self.query_one("#files-body", Static).update(_render_files(files))
        self.query_one("#agents-body", Static).update(_render_agents(agents))
        self.query_one("#workflows-mixture", Static).update(_render_mixture(agents))

    async def _refresh_agent_swarm(self) -> None:
        if not self._agent_swarm_enabled or self._fetch_agent_swarm_state is None:
            return
        try:
            text = await self._fetch_agent_swarm_state()
        except Exception as exc:
            text = f"error: {exc}"
        self.query_one("#workflows-agent-swarm", Static).update(text)

    def action_refresh_workflows(self) -> None:
        self.run_worker(self._refresh_agent_swarm(), exit_on_error=False)

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane is not None and event.pane.id == "tab-workflows":
            self.action_refresh_workflows()
