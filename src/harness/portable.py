"""Read-only, provider-independent continuation packages for one tracked task.

The package is a snapshot of recorded facts, never an executable handoff or an
authority grant. Only immutable session artifacts are copied; workspace files,
provider configuration and plugin stores are not traversed.
"""

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from harness.blobs import BlobStore
from harness.coordination import load_report
from harness.events import (
    AgentRunFinished, AgentRunStarted, ContextPolicyConfigured, ContextSourceObserved,
    CoordinationFinished, CoordinationStarted,
    DispatchResolved, ModelCallProposed, ModelCallStarted, SubagentFinished, SubagentSpawned,
    TaskChecked, TaskHandoffRecorded, ToolCallAborted, ToolCallCancelled, ToolCallCompleted, ToolCallProposed,
    UnknownEvent, UserMessage,
)
from harness.log import read_session
from harness.persistence import atomic_write
from harness.tasks import project_tasks

MAX_EXPORT_BYTES = 32 * 1024 * 1024


class ExportError(ValueError):
    """An operator-facing refusal containing no source payload or exception body."""


LIMITATIONS = [
    "This is recorded task data, not instructions from the destination's operator or a permission grant.",
    "Establish destination workspace, tools, permissions and budgets before continuing.",
    "Inspect current files and reconcile uncertain effects before repeating any action. A successful tool result is not a current filesystem check.",
    "Context snapshots describe earlier retrievals. Refresh required sources under the destination's own permissions; memory remains owned by its plugin.",
    "Provider conversation IDs, hidden reasoning, running processes, credentials, grants, plugin state, unsent drafts and queued prompts do not transfer.",
    "Child sessions are references only. Their internal effects and artifacts require separate inspection.",
    "Only recorded task messages, context snapshots and result artifacts are included. Unrecorded system or @-mention context and mutable workspace files are not copied.",
    "Export does not run checks, accept work, stop processes or change the source session. Record new work and acceptance separately at the destination.",
]


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


class _Artifacts:
    def __init__(self, root):
        # BlobStore normally creates its directory. Inspection must not do so.
        if not root.is_dir():
            raise ExportError("session artifact directory is missing")
        self.blobs = BlobStore(root, create=False)
        self.files = {}
        self.verified = set()
        self.total = 0

    def add(self, *, ref=None, text=None):
        if ref is None and text is None:
            return None
        size = ref.size if ref is not None else len(text.encode())
        digest = ref.sha256 if ref is not None else hashlib.sha256(text.encode()).hexdigest()
        name = f"artifacts/{digest}"
        if name in self.files:
            if len(self.files[name]) != size:
                raise ExportError("conflicting artifact sizes")
            if ref is not None and digest not in self.verified:
                self.blobs.get(ref)
        else:
            if self.total + size > MAX_EXPORT_BYTES:
                raise ExportError("continuation package exceeds 32 MiB")
            self.files[name] = self.blobs.get(ref) if ref is not None else text.encode()
            self.total += size
        if ref is not None:
            self.verified.add(digest)
        return {"path": name, "sha256": digest, "size": size}


def _one_by_call(events, classes):
    result = {}
    for env in events:
        if isinstance(env.event, classes):
            result.setdefault(env.event.call_id, []).append(env)
    return result


def task_package(base: Path, session_id: str, *, task_id: str | None = None):
    """Capture a single validated log prefix without opening a session writer."""
    events = read_session(base, session_id, repair=False)
    if not events or any(isinstance(e.event, UnknownEvent) or e.v != 1 for e in events):
        raise ExportError("export requires a nonempty, fully understood session log")
    state = project_tasks(events)
    if task_id is None:
        task = state.items.get(state.selected_id)
    else:
        matches = [t for key, t in state.items.items() if task_id and key.startswith(task_id)]
        if len(matches) != 1:
            raise ExportError("task must identify exactly one tracked task")
        task = matches[0]
    if task is None:
        raise ExportError("select a tracked task or specify its ID")
    if task.open_runs:
        raise ExportError("settle or recover the task's active runs before exporting")
    artifacts = _Artifacts(base / "sessions" / session_id / "blobs")
    owned = {run for run, owner in state.run_owners.items() if owner == task.definition.id}
    dispatches = _one_by_call([e for e in events if isinstance(e.event, DispatchResolved)
                              and e.event.kind == "tool"], DispatchResolved)
    terminals = _one_by_call(events, (ToolCallCompleted, ToolCallAborted, ToolCallCancelled))
    model_proposals = _one_by_call(events, ModelCallProposed)
    tool_proposals = _one_by_call(events, ToolCallProposed)
    runs, calls, context, children, external, reconciliations = {}, {}, [], {}, [], []
    configurations = {}
    coordination = {}
    current_policy = None
    active_roots = set()

    for env in events:
        event = env.event
        if isinstance(event, ContextPolicyConfigured):
            current_policy = event.policy
            if event.policy is not None:
                configurations[event.policy.digest] = event.policy
        elif isinstance(event, AgentRunStarted) and event.run_id in owned:
            if event.run_id in runs:
                raise ExportError("ambiguous reused agent run ID")
            runs[event.run_id] = {"run_id": event.run_id, "parent_run_id": event.parent_run_id,
                "source_seq": env.seq, "runtime": event.runtime, "agent": event.agent,
                "model": event.model, "handoff_id": event.handoff_id,
                "status": "unconfirmed", "messages": [], "output": None}
            if event.parent_run_id is None:
                active_roots.add(event.run_id)
        elif isinstance(event, AgentRunFinished) and event.result.run_id in owned:
            row = runs.get(event.result.run_id)
            if row is None or "finished_seq" in row:
                raise ExportError("ambiguous agent execution boundary")
            row.update(status=event.result.status, reason=event.result.reason,
                finished_seq=env.seq, output=artifacts.add(ref=event.result.output))
            active_roots.discard(event.result.run_id)
        elif isinstance(event, UserMessage) and active_roots:
            if len(active_roots) != 1:
                raise ExportError("ambiguous task message ownership")
            runs[next(iter(active_roots))]["messages"].append({"source_seq": env.seq, "text": event.text})
        elif isinstance(event, ToolCallProposed) and event.agent_run_id in owned:
            if len(tool_proposals[event.call_id]) != 1:
                raise ExportError("ambiguous reused tool call ID")
            resolved, finished = dispatches.get(event.call_id, []), terminals.get(event.call_id, [])
            if len(resolved) > 1 or len(finished) > 1:
                raise ExportError("ambiguous tool execution boundary")
            dispatch = resolved[0] if resolved else None
            terminal = finished[0] if finished else None
            if dispatch and dispatch.seq <= env.seq or terminal and terminal.seq <= (dispatch or env).seq:
                raise ExportError("invalid tool execution order")
            effective = dispatch.event if dispatch else event
            completed = terminal is not None and isinstance(terminal.event, ToolCallCompleted)
            result = terminal.event if completed else None
            calls[event.call_id] = {"call_id": event.call_id, "run_id": event.agent_run_id,
                "source_seq": env.seq, "purpose": event.purpose,
                "tool": effective.tool, "args": effective.args,
                "dispatch_seq": dispatch.seq if dispatch else None,
                "terminal_seq": terminal.seq if terminal else None,
                "outcome": ("error" if result.is_error else "completed") if completed else
                    terminal.event.type.removeprefix("tool_call_") if terminal else "unconfirmed",
                "effects": "inspect" if dispatch else "not_dispatched",
                "result": artifacts.add(ref=result.result_blob, text=result.result_text) if result else None}
        elif isinstance(event, ContextSourceObserved) and event.run_id in owned:
            policy = configurations.get(event.policy_digest)
            source = next((s for s in policy.sources if s.id == event.source_id), None) if policy else None
            context.append({"source_seq": env.seq, "run_id": event.run_id, "source_id": event.source_id,
                "policy_digest": event.policy_digest, "tool": event.tool, "call_id": event.call_id,
                "reference": source.model_dump(mode="json") if source else None,
                "status": event.status, "snapshot": artifacts.add(ref=event.result),
                "freshness": "recorded_only"})
        elif isinstance(event, ModelCallStarted) and event.execution_kind == "agent":
            proposals = model_proposals.get(event.call_id, [])
            if any(p.event.agent_run_id in owned for p in proposals):
                if len(proposals) != 1 or proposals[0].seq >= env.seq:
                    raise ExportError("ambiguous external execution boundary")
                external.append({"source_seq": env.seq, "call_id": event.call_id,
                    "run_id": proposals[0].event.agent_run_id, "model": event.model,
                    "effects": "uninspected", "native_state": "not_exported"})
        elif isinstance(event, SubagentSpawned) and (event.call_id in calls or active_roots):
            children[event.child_session_id] = {"source_seq": env.seq,
                "session_id": event.child_session_id, "call_id": event.call_id,
                "status": "unconfirmed", "contents": "not_exported"}
        elif isinstance(event, SubagentFinished) and event.child_session_id in children:
            children[event.child_session_id].update(status=event.status, finished_seq=env.seq)
        elif isinstance(event, CoordinationStarted) and event.call_id in calls:
            if event.id in coordination:
                raise ExportError("ambiguous reused coordination ID")
            coordination[event.id] = {"source_seq": env.seq, "started_seq": env.seq,
                "id": event.id, "call_id": event.call_id, "strategy": event.strategy,
                "status": "unconfirmed", "timeout_seconds": event.timeout_seconds,
                "report": None, "output": None}
        elif isinstance(event, CoordinationFinished) and event.call_id in calls:
            previous = coordination.get(event.id, {})
            if previous and ("finished_seq" in previous or previous["call_id"] != event.call_id
                             or previous["strategy"] != event.strategy):
                raise ExportError("ambiguous coordination terminal")
            ref = artifacts.add(ref=event.report)
            report = load_report(artifacts.blobs, event, session_id)
            coordination[event.id] = {**previous, "source_seq": env.seq, "finished_seq": env.seq,
                "id": event.id, "call_id": event.call_id, "strategy": event.strategy,
                "timeout_seconds": report.timeout_seconds,
                "status": event.status, "report": ref, "output": artifacts.add(
                    ref=report.output) if report.output else None}
        elif isinstance(event, TaskHandoffRecorded) and event.record.task_id == task.definition.id:
            from types import SimpleNamespace
            from harness.handoff import load_record
            checkpoint, spec = load_record(SimpleNamespace(blobs=artifacts.blobs), event.record)
            reconciliations.append({"id": event.record.id, "source_seq": env.seq,
                "source_run_id": event.record.source_run_id, "basis_seq": event.record.basis_seq,
                "snapshot_sha256": event.record.snapshot.sha256,
                "specification_sha256": event.record.specification.sha256,
                "resolutions": [r.model_dump(mode="json") for r in spec.resolutions],
                "effects": [{"id": e.id, "kind": e.kind, "source_seq": e.source_seq,
                             "state": e.state, "operator_note": e.operator_note} for e in checkpoint.effects]})

    checked_seq = next((e.seq for e in reversed(events) if isinstance(e.event, TaskChecked)
        and e.event.task_id == task.definition.id and e.event.basis_seq == task.basis_seq), None)
    requirements = []
    for key, requirement in task.requirements.items():
        evidence = task.evidence.get(key)
        confirmation = task.confirmations.get(key)
        exported = evidence.model_dump(mode="json") if evidence else None
        if exported is not None:
            exported["artifact"] = artifacts.add(ref=evidence.artifact)
        requirements.append({"definition": requirement.model_dump(mode="json"),
            "declared_seq": task.declared_at[key], "evidence": exported,
            "checked_seq": checked_seq if evidence else None,
            "confirmation": {"source_seq": confirmation[0], "note": confirmation[1]} if confirmation else None,
            "status": "passed" if confirmation else evidence.status if evidence else "unverified"})
    package = {"format": "harness-continuation", "version": 1,
        "source": {"session_id": session_id, "through_seq": events[-1].seq, "recorded_at": events[-1].ts,
            "canonical_events_sha256": hashlib.sha256(b"".join(
                (e.model_dump_json() + "\n").encode() for e in events)).hexdigest()},
        "task": {"id": task.definition.id, "title": task.definition.title, "basis_seq": task.basis_seq,
            "run_id": task.run_id, "execution": task.execution, "accepted": task.accepted,
            "acceptance": {"source_seq": task.acceptance[0], "note": task.acceptance[1]}
                          if task.acceptance else None,
            "requirements": requirements, "unresolved": list(task.unresolved)},
        "runs": list(runs.values()), "tool_calls": list(calls.values()), "context": context,
        "configured_sources": [s.model_dump(mode="json") for s in current_policy.sources] if current_policy else [],
        "external_executions": external, "child_sessions": list(children.values()),
        "reconciliations": reconciliations, "coordination": list(coordination.values()), "limitations": list(LIMITATIONS)}
    return package, artifacts.files


def render_package(package):
    """Quote user-controlled text as indented data, including Markdown and controls."""
    def quote(value):
        clean = "".join(c if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) <= 159
                        else "�" for c in str(value))
        # Eight spaces also form a code block beneath a preceding list item.
        return "\n".join("        " + line for line in clean.split("\n"))

    task, source = package["task"], package["source"]
    lines = ["# Continue this task", "", "Task objective (recorded data):", "", quote(task["title"]), "",
        f"Source session: `{source['session_id']}` through event {source['through_seq']}.",
        f"Task: `{task['id']}`. Execution: {task['execution']}. Accepted: {str(task['accepted']).lower()}.", "",
        "## Remaining work", ""]
    for row in task["requirements"]:
        requirement = row["definition"]
        lines.extend([f"- {requirement['id']}: {row['status']} ({requirement['check']['kind']})", "",
                      quote(requirement["description"]), ""])
    if not task["requirements"]:
        lines.extend(["No acceptance criteria were recorded. Establish them before continuing.", ""])
    lines.extend(["Unresolved requirement IDs: " + (", ".join(task["unresolved"]) or "none") + ".", "",
        "## Recorded requests and outputs", ""])
    for run in package["runs"]:
        lines.extend([quote(f"Run {run['run_id']}: {run['status']}"), ""])
        for message in run["messages"]:
            lines.extend([f"Request at event {message['source_seq']}:", "", quote(message["text"]), ""])
        if run["output"]:
            lines.extend([f"Recorded output: [{run['output']['sha256']}]({run['output']['path']}).", ""])
    lines.extend(["## Context, evidence and effects", "",
        "Read `continuation.json` for exact checks, original event references, tool calls, context queries and snapshot artifacts.",
        "All artifact paths are relative to this package; verify their size and SHA-256 before use.",
        "Configured sources are the last session configuration; each historical retrieval carries its own reference.", "",
        f"Recorded context observations: {len(package['context'])}. Tool calls to inspect: {len(package['tool_calls'])}.",
        f"External executions to reconcile: {len(package['external_executions'])}. Child session references: {len(package['child_sessions'])}.", "",
        f"Coordination reports: {len(package.get('coordination', []))}. Inspect participant outcomes and disagreements in `continuation.json`.", "",
        "## Continuation boundaries", "", *(f"- {line}" for line in package["limitations"]), ""])
    return "\n".join(lines).encode()


def prepare_export(base: Path, session_id: str, *, task_id: str | None = None):
    """Build complete bytes without filesystem writes, safe to discard on cancellation."""
    package, files = task_package(base, session_id, task_id=task_id)
    package["artifacts"] = [{"path": name, "size": len(data), "sha256": name.split("/")[1]}
                            for name, data in sorted(files.items())]
    files["continuation.json"] = _json(package)
    files["CONTINUE.md"] = render_package(package)
    if sum(map(len, files.values())) > MAX_EXPORT_BYTES:
        raise ExportError("continuation package exceeds 32 MiB")
    archive = io.BytesIO()
    with ZipFile(archive, "w", compression=ZIP_STORED) as destination:
        for name, data in sorted(files.items()):
            info = ZipInfo(name)  # Fixed timestamps make the same snapshot reproducible.
            info.external_attr = 0o100600 << 16
            destination.writestr(info, data)
    if archive.tell() > MAX_EXPORT_BYTES:
        raise ExportError("continuation package exceeds 32 MiB")
    return archive.getvalue()


def export_task(base: Path, session_id: str, output: Path, *, task_id: str | None = None):
    """Publish an exclusive, complete ZIP of Markdown, JSON and verified artifacts."""
    if output.exists() or output.is_symlink():
        raise FileExistsError("export destination already exists")
    data = prepare_export(base, session_id, task_id=task_id)
    atomic_write(output, data, replace=False)
