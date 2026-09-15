"""Contract for referencing plugin workflow state without importing plugins.

Core already records tool facts for every MCP dispatch: ToolCallProposed,
DispatchResolved, and ToolCallCompleted. This module derives everything it
needs about a plugin-managed workflow purely from those facts, without ever
importing agent-swarm or memory internals.

- project_plugin_workflows folds completed workflow tool calls from a
  session log into PluginWorkflowRef facts. Pure, total, log-only.
- count_memory_contributions and count_accepted_records fold completed
  memory-plugin tool calls from the same log.
- reconcile_from_log builds a ReconciliationReport from the log alone.
  Every ref is reported unknown: without a live check, core never invents
  a status.
- reconcile is the live version. It re-dispatches
  workflow__workflow_get_state through the kernel ordinary dispatcher for
  every ref, and classifies the answer.
- main implements a read-only harness plugins reconcile CLI that reports
  from the log alone and never starts or contacts any MCP server.

THE LAW: an in-memory plugin workflow lost across a plugin restart is not
resumable by core. Core does not persist or guess at plugin-side state
across that loss; it can only observe, through ordinary dispatch, whatever
the plugin is willing to report right now. See docs/plugin-reconciliation.md.
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness.events import DispatchResolved, ToolCallCompleted, ToolCallProposed
from harness.hooks import ProposedToolCall
from harness.log import TornLogError, read_session
from harness.types import SessionId, ToolName, new_call_id

Status = Literal["active", "finished", "unknown", "unavailable"]

_WORKFLOW_VERBS = frozenset(
    {"workflow_start", "workflow_get_state", "workflow_advance_phase", "workflow_stop"}
)
_MEMORY_READ_TOOLS = frozenset({"memory_get", "memory_list", "memory_brief"})
_MEMORY_WRITE_TOOL = "memory_write"


@dataclass(frozen=True)
class PluginWorkflowRef:
    server: str
    workflow_id: str
    last_phase: str | None
    observed_seq: int
    observed_call_id: str


@dataclass(frozen=True)
class ReconciliationReport:
    refs: tuple[PluginWorkflowRef, ...]
    statuses: dict[str, Status]
    non_resumable: tuple[str, ...]
    memory_contributions: int
    accepted_records: int
    live_checks: tuple[str, ...] = ()


def _ref_key(server: str, workflow_id: str) -> str:
    return f"{server}:{workflow_id}"


def _split_mcp_tool(tool: str) -> list[str] | None:
    if not isinstance(tool, str) or not tool.startswith("mcp__"):
        return None
    return tool.split("__")


def _workflow_tool(tool: str) -> tuple[str, str] | None:
    parts = _split_mcp_tool(tool)
    if parts is None or len(parts) != 4:
        return None
    marker, server, group, verb = parts
    if marker != "mcp" or group != "workflow" or verb not in _WORKFLOW_VERBS or not server:
        return None
    return server, verb


def _memory_tool(tool: str) -> str | None:
    parts = _split_mcp_tool(tool)
    if parts is None or len(parts) != 3 or parts[0] != "mcp" or parts[1] != "memory":
        return None
    return parts[2]


def _text_field(data: object, key: str) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def project_plugin_workflows(envelopes) -> tuple[PluginWorkflowRef, ...]:
    proposed: dict[str, tuple[str, dict]] = {}
    refs: dict[tuple[str, str], PluginWorkflowRef] = {}
    for env in envelopes:
        event = env.event
        if isinstance(event, ToolCallProposed):
            proposed[str(event.call_id)] = (str(event.tool), dict(event.args))
        elif isinstance(event, DispatchResolved) and event.kind == "tool" and event.tool is not None:
            proposed[str(event.call_id)] = (str(event.tool), dict(event.args or {}))
        elif isinstance(event, ToolCallCompleted) and not event.is_error:
            entry = proposed.get(str(event.call_id))
            if entry is None:
                continue
            tool, args = entry
            parsed = _workflow_tool(tool)
            if parsed is None:
                continue
            server, _verb = parsed
            data = None
            if isinstance(event.result_text, str):
                try:
                    data = json.loads(event.result_text)
                except (json.JSONDecodeError, TypeError, ValueError):
                    data = None
            if isinstance(data, dict) and "error" in data:
                continue
            workflow_id = _text_field(data, "workflow_id") or _text_field(args, "workflow_id")
            if workflow_id is None:
                continue
            phase = _text_field(data, "phase")
            key = (server, workflow_id)
            previous = refs.get(key)
            refs[key] = PluginWorkflowRef(
                server=server,
                workflow_id=workflow_id,
                last_phase=phase if phase is not None else (previous.last_phase if previous else None),
                observed_seq=env.seq,
                observed_call_id=str(event.call_id),
            )
    return tuple(sorted(refs.values(), key=lambda r: (r.server, r.workflow_id)))


def count_memory_contributions(envelopes) -> int:
    proposed: dict[str, tuple[str, str]] = {}
    count = 0
    for env in envelopes:
        event = env.event
        if isinstance(event, ToolCallProposed):
            proposed[str(event.call_id)] = (str(event.tool), event.purpose)
        elif isinstance(event, DispatchResolved) and event.kind == "tool" and event.tool is not None:
            entry = proposed.get(str(event.call_id))
            if entry is not None:
                proposed[str(event.call_id)] = (str(event.tool), entry[1])
        elif isinstance(event, ToolCallCompleted) and not event.is_error:
            entry = proposed.get(str(event.call_id))
            if entry is None:
                continue
            tool, purpose = entry
            if purpose == "context" and _memory_tool(tool) in _MEMORY_READ_TOOLS:
                count += 1
    return count


def count_accepted_records(envelopes) -> int:
    proposed: dict[str, str] = {}
    count = 0
    for env in envelopes:
        event = env.event
        if isinstance(event, ToolCallProposed):
            proposed[str(event.call_id)] = str(event.tool)
        elif isinstance(event, DispatchResolved) and event.kind == "tool" and event.tool is not None:
            if str(event.call_id) in proposed:
                proposed[str(event.call_id)] = str(event.tool)
        elif isinstance(event, ToolCallCompleted) and not event.is_error:
            tool = proposed.get(str(event.call_id))
            if tool is None or _memory_tool(tool) != _MEMORY_WRITE_TOOL:
                continue
            text = event.result_text or ""
            if not text.startswith("error:"):
                count += 1
    return count


def reconcile_from_log(envelopes) -> ReconciliationReport:
    refs = project_plugin_workflows(envelopes)
    statuses: dict[str, Status] = {_ref_key(r.server, r.workflow_id): "unknown" for r in refs}
    return ReconciliationReport(
        refs=refs,
        statuses=statuses,
        non_resumable=(),
        memory_contributions=count_memory_contributions(envelopes),
        accepted_records=count_accepted_records(envelopes),
    )


async def reconcile(kernel) -> ReconciliationReport:
    envelopes = read_session(kernel.session.base, kernel.session.id, repair=False)
    base = reconcile_from_log(envelopes)
    statuses = dict(base.statuses)
    non_resumable: list[str] = []
    for ref in base.refs:
        key = _ref_key(ref.server, ref.workflow_id)
        call = ProposedToolCall(
            call_id=new_call_id(),
            tool=ToolName(f"mcp__{ref.server}__workflow__workflow_get_state"),
            args={"workflow_id": ref.workflow_id},
        )
        outcome = await kernel.loop.dispatcher.dispatch_tool(call, purpose="context")
        resolved = outcome.resolved
        if resolved is not None and (
            resolved.kind != "tool"
            or resolved.call_id != call.call_id
            or resolved.tool != call.tool
            or (resolved.args or {}).get("workflow_id") != ref.workflow_id
        ):
            # A hook redirected this call; its result says nothing about this ref.
            statuses[key] = "unknown"
            continue
        if outcome.is_error:
            statuses[key] = "unavailable"
            non_resumable.append(key)
            continue
        if resolved is None:
            statuses[key] = "unknown"
            continue
        try:
            text = outcome.read_text()
        except Exception:
            statuses[key] = "unknown"
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            data = None
        if isinstance(data, dict) and "error" in data:
            statuses[key] = "unavailable"
            non_resumable.append(key)
        elif isinstance(data, dict) and data.get("status") in ("active", "finished"):
            statuses[key] = data["status"]
        else:
            statuses[key] = "unknown"
    current = reconcile_from_log(read_session(kernel.session.base, kernel.session.id, repair=False))
    return ReconciliationReport(
        refs=current.refs,
        statuses={**current.statuses, **statuses},
        non_resumable=tuple(non_resumable),
        memory_contributions=current.memory_contributions,
        accepted_records=current.accepted_records,
        live_checks=tuple(statuses),
    )


def render_reconciliation(report: ReconciliationReport) -> str:
    lines = [f"Plugin workflow refs: {len(report.refs)}"]
    if not report.refs:
        lines.append("  (none observed in this session log)")
    for ref in report.refs:
        key = _ref_key(ref.server, ref.workflow_id)
        status = report.statuses.get(key, "unknown")
        phase = ref.last_phase if ref.last_phase is not None else "(unknown)"
        if key in report.non_resumable:
            note = "not resumable"
        elif status in ("active", "finished"):
            note = "live check performed"
        elif key in report.live_checks:
            note = "live check inconclusive"
        else:
            note = "no live check performed"
        lines.append(
            f"  {ref.server}/{ref.workflow_id}: phase={phase}; status={status}; {note}"
            f" (event {ref.observed_seq})"
        )
    lines.append(f"Memory contributions (context reads): {report.memory_contributions}")
    lines.append(f"Accepted memory records (writes): {report.accepted_records}")
    return "\n".join(lines)


def summarize(report: ReconciliationReport) -> str:
    unavailable = sum(1 for status in report.statuses.values() if status == "unavailable")
    return (
        f"plugins: {len(report.refs)} workflow refs ({unavailable} unavailable), "
        f"{report.memory_contributions} memory contributions, "
        f"{report.accepted_records} accepted records"
    )


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="harness plugins", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    reconcile_parser = sub.add_parser(
        "reconcile", help="Report plugin workflow refs and memory counts from the recorded log."
    )
    reconcile_parser.add_argument("session_id")
    reconcile_parser.add_argument(
        "--base-dir", type=Path, default=Path.home() / ".local/share/harness"
    )
    args = parser.parse_args(argv)
    try:
        envelopes = read_session(args.base_dir, SessionId(args.session_id), repair=False)
    except (OSError, ValueError, TornLogError) as exc:
        parser.error(f"plugin reconciliation failed ({type(exc).__name__})")
        return
    report = reconcile_from_log(envelopes)
    print(render_reconciliation(report))
