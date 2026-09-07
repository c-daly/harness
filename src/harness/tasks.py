"""Durable user obligations and checks of recorded evidence, independent of agents.

Checks read the session log and immutable blobs only. They do not run commands,
inspect mutable workspace files, infer user approval, or grant any authority.
"""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from harness.blobs import BlobIntegrityError, BlobRef, MissingBlobError

MAX_REQUIREMENTS = 32
MAX_EVIDENCE_BYTES = 1024 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ReviewCheck(_Record):
    kind: Literal["review"] = "review"


class OutputCheck(_Record):
    kind: Literal["output"] = "output"
    sha256: Digest


class ToolResultCheck(_Record):
    kind: Literal["tool_result"] = "tool_result"
    tool: str = Field(min_length=1, max_length=256)
    args: dict[str, JsonValue]
    sha256: Digest

    @field_validator("args")
    @classmethod
    def bounded_args(cls, value):
        if len(canonical_args(value).encode()) > 8192:
            raise ValueError("check arguments exceed 8192 bytes")
        return value


def canonical_args(args) -> str:
    # JSON, rather than Python equality: true and 1 are different tool arguments.
    return json.dumps(args, sort_keys=True, separators=(",", ":"), allow_nan=False)


class TaskRequirement(_Record):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=2048)
    check: Annotated[ReviewCheck | OutputCheck | ToolResultCheck, Field(discriminator="kind")] = Field(
        default_factory=ReviewCheck)

    @field_validator("description")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("description must not be blank")
        return value


class TaskDefinition(_Record):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    title: str = Field(min_length=1, max_length=4096)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("title must not be blank")
        return value


class RequirementEvidence(_Record):
    requirement_id: str
    status: Literal["passed", "failed", "unverified"]
    reason: str
    source_seq: int | None = None
    call_id: str | None = None
    artifact: BlobRef | None = None
    actual_sha256: Digest | None = None


@dataclass
class TrackedTask:
    definition: TaskDefinition
    requirements: dict[str, TaskRequirement] = field(default_factory=dict)
    declared_at: dict[str, int] = field(default_factory=dict)
    basis_seq: int = 0
    run_id: str | None = None
    run_started_seq: int = 0
    execution: str = "not started"
    execution_reason: str = ""
    open_runs: set[str] = field(default_factory=set)
    evidence: dict[str, RequirementEvidence] = field(default_factory=dict)
    confirmations: dict[str, tuple[int, str]] = field(default_factory=dict)
    acceptance: tuple[int, str] | None = None

    @property
    def criteria(self) -> tuple[str, ...]:
        return (f"Objective: {self.definition.title}", *(
            f"{r.id}: {r.description}" + (f"; check={r.check.model_dump_json()}"
                                         if r.check.kind != "review" else " (user review required)")
            for r in self.requirements.values()))

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(key for key, req in self.requirements.items()
                     if (key not in self.confirmations if req.check.kind == "review" else
                         key not in self.evidence or self.evidence[key].status != "passed"))

    @property
    def accepted(self) -> bool:
        return bool(self.acceptance and self.requirements and not self.unresolved
                    and self.execution == "completed" and not self.open_runs)

    def invalidate(self, seq):
        self.basis_seq = seq
        self.evidence.clear()
        self.confirmations.clear()
        self.acceptance = None


@dataclass
class TaskState:
    items: dict[str, TrackedTask] = field(default_factory=dict)
    selected_id: str | None = None
    # Includes same-session external-runtime descendants, never unrelated runs.
    run_owners: dict[str, str] = field(default_factory=dict)
    open_runs: set[str] = field(default_factory=set)

    def apply(self, env) -> None:
        from harness.events import (
            AgentRunFinished, AgentRunStarted, TaskAccepted, TaskChecked, TaskCreated,
            TaskRequirementAdded, TaskRequirementConfirmed, TaskSelected,
        )
        event = env.event
        if isinstance(event, AgentRunStarted):
            self.open_runs.add(event.run_id)
        elif isinstance(event, AgentRunFinished):
            self.open_runs.discard(event.result.run_id)
        if isinstance(event, TaskCreated) and event.definition is not None:
            if event.definition.id not in self.items:
                self.items[event.definition.id] = TrackedTask(event.definition, basis_seq=env.seq)
        elif isinstance(event, TaskSelected):
            if event.task_id is None or event.task_id in self.items:
                self.selected_id = event.task_id
        elif isinstance(event, AgentRunStarted):
            owner = self.run_owners.get(event.parent_run_id)
            if owner is None and event.task_id in self.items:
                owner = event.task_id
                task = self.items[owner]
                task.invalidate(env.seq)
                task.run_id, task.run_started_seq = event.run_id, env.seq
                task.execution = "running"
                task.execution_reason = ""
            if owner is not None:
                self.run_owners[event.run_id] = owner
                self.items[owner].open_runs.add(event.run_id)
        elif isinstance(event, AgentRunFinished):
            owner = self.run_owners.get(event.result.run_id)
            if owner is not None:
                task = self.items[owner]
                task.open_runs.discard(event.result.run_id)
                if event.result.run_id == task.run_id:
                    task.execution = event.result.status
                    task.execution_reason = event.result.reason
                    task.invalidate(env.seq)
        elif isinstance(event, (TaskRequirementAdded, TaskChecked, TaskRequirementConfirmed, TaskAccepted)):
            task = self.items.get(event.task_id)
            if task is None:
                return
            if isinstance(event, TaskRequirementAdded):
                req = event.requirement
                if req is not None and req.id not in task.requirements:
                    task.requirements[req.id] = req
                    task.declared_at[req.id] = env.seq
                    task.invalidate(env.seq)
            elif event.basis_seq == task.basis_seq and not task.open_runs:
                if isinstance(event, TaskChecked):
                    task.evidence = {e.requirement_id: e for e in event.evidence
                                     if e.requirement_id in task.requirements
                                     and task.requirements[e.requirement_id].check.kind != "review"}
                    if task.unresolved:
                        task.acceptance = None
                elif isinstance(event, TaskRequirementConfirmed):
                    req = task.requirements.get(event.requirement_id)
                    if req is not None and req.check.kind == "review":
                        task.confirmations[req.id] = (env.seq, event.note)
                elif task.requirements and not task.unresolved and task.execution == "completed":
                    task.acceptance = (env.seq, event.note)


def project_tasks(envelopes) -> TaskState:
    state = TaskState()
    for env in envelopes:
        state.apply(env)
    return state


class TaskService:
    """Explicit caller commands. This service is not registered as an agent tool."""

    def __init__(self, session):
        self.session = session

    def _read(self):
        from harness.log import read_session
        return read_session(self.session.base, self.session.id, repair=False)

    def state(self) -> TaskState:
        if self.session._task_state is None:
            self.session._task_state = project_tasks(self._read())
        return deepcopy(self.session._task_state)

    def selected(self) -> TrackedTask | None:
        state = self.state()
        return state.items.get(state.selected_id)

    def _idle(self):
        if self.session._seq == 0:
            raise ValueError("start the session before changing tasks")
        if self.state().open_runs:
            raise ValueError("an agent is running; wait for it to settle")

    def _selected(self):
        task = self.selected()
        if task is None:
            raise ValueError("no task selected; use /task new OBJECTIVE or /task use ID")
        return task

    def create(self, title: str) -> TaskDefinition:
        from harness.events import TaskCreated, TaskSelected
        self._idle()
        definition = TaskDefinition(title=title)
        self.session.append(TaskCreated(definition=definition))
        self.session.append(TaskSelected(task_id=definition.id))
        return definition

    def select(self, identity: str | None) -> None:
        from harness.events import TaskSelected
        self._idle()
        if identity is not None:
            matches = [key for key in self.state().items if identity and key.startswith(identity)]
            if len(matches) != 1:
                raise ValueError("task ID must identify exactly one task")
            identity = matches[0]
        self.session.append(TaskSelected(task_id=identity))

    def add_requirement(self, requirement: TaskRequirement | dict) -> None:
        from harness.events import TaskRequirementAdded
        self._idle()
        task = self._selected()
        data = requirement.model_dump() if isinstance(requirement, TaskRequirement) else requirement
        req = TaskRequirement.model_validate(data)
        if req.id in task.requirements:
            raise ValueError("requirement ID already exists; requirements cannot be overwritten")
        if len(task.requirements) >= MAX_REQUIREMENTS:
            raise ValueError(f"task already has {MAX_REQUIREMENTS} requirements")
        self.session.append(TaskRequirementAdded(task_id=task.definition.id, requirement=req))

    def prepare(self, prompt, *, context=()):
        from harness.agent import AgentTask
        task = self.selected()
        if task is None:
            return AgentTask(prompt=prompt, context=context)
        if task.run_id is not None:
            from harness.messages import Message
            # This summary is derived before the new attempt invalidates old evidence.
            import json
            previous = {"run_id": task.run_id, "execution": task.execution,
                        "reason": task.execution_reason[:512], "accepted": task.accepted,
                        "unresolved": list(task.unresolved)}
            context = (*context, Message.system_text(
                "Previous task attempt (historical session data):\n" + json.dumps(previous) +
                "\nThis attempt needs fresh evidence. Inspect current state before repeating uncertain work."
            ))
        return AgentTask(id=task.definition.id, prompt=prompt, context=context,
                         acceptance_criteria=task.criteria)

    def validate_run(self, task):
        tracked = self.state().items.get(task.id)
        if tracked is not None:
            if tracked.open_runs:
                raise ValueError("this task already has a running attempt")
            if task.acceptance_criteria != tracked.criteria:
                raise ValueError("task requirements changed; prepare the task again before running")

    def check(self) -> TrackedTask:
        from harness.events import TaskChecked
        self._idle()
        events = self._read()
        state = project_tasks(events)
        task = state.items.get(state.selected_id)
        if task is None:
            raise ValueError("no task selected")
        evidence = tuple(_check_requirement(req, task, events, self.session.blobs)
                         for req in task.requirements.values() if req.check.kind != "review")
        self.session.append(TaskChecked(task_id=task.definition.id, basis_seq=task.basis_seq,
                                        evidence=evidence))
        return self._selected()

    def confirm(self, requirement_id: str, note: str) -> None:
        from harness.events import TaskRequirementConfirmed
        self._idle()
        task = self._selected()
        req = task.requirements.get(requirement_id)
        if req is None or req.check.kind != "review":
            raise ValueError("confirm identifies a user-review requirement; checks cannot be overridden")
        if task.execution != "completed":
            raise ValueError("user review requires a completed execution")
        self.session.append(TaskRequirementConfirmed(task_id=task.definition.id,
            requirement_id=requirement_id, basis_seq=task.basis_seq, note=_review_note(note)))

    def accept(self, note: str) -> None:
        from harness.events import TaskAccepted
        note = _review_note(note)
        self._idle()
        task = self._selected()
        if not task.requirements:
            raise ValueError("define at least one requirement before acceptance")
        if task.execution != "completed":
            raise ValueError("acceptance requires a completed execution")
        task = self.check()  # Re-read blobs; an earlier check is not current integrity proof.
        if task.unresolved:
            raise ValueError("unresolved requirements: " + ", ".join(task.unresolved))
        self.session.append(TaskAccepted(task_id=task.definition.id, basis_seq=task.basis_seq, note=note))


def _review_note(note):
    if not isinstance(note, str) or not note.strip() or len(note) > 2048:
        raise ValueError("review note must contain 1 to 2048 characters")
    return note


def _check_requirement(requirement, task, events, blobs):
    from harness.events import (
        AgentRunFinished, AgentRunStarted, DispatchResolved, ToolCallCompleted, ToolCallProposed,
        ToolCallAborted, ToolCallCancelled,
    )
    details = {"requirement_id": requirement.id}

    def result(status, reason, **extra):
        return RequirementEvidence(**details, status=status, reason=reason, **extra)

    if task.run_id is None:
        return result("unverified", "no execution")
    starts = [e for e in events if isinstance(e.event, AgentRunStarted) and e.event.run_id == task.run_id]
    finishes = [e for e in events if isinstance(e.event, AgentRunFinished) and e.event.result.run_id == task.run_id]
    if len(starts) != 1 or len(finishes) != 1 or starts[0].seq >= finishes[0].seq:
        return result("unverified", "execution boundary is missing or ambiguous")
    if task.declared_at[requirement.id] >= task.run_started_seq:
        return result("unverified", "requirement added after this attempt; run the task again")
    ref, data = None, None
    if isinstance(requirement.check, OutputCheck):
        source = finishes[0]
        if source.event.result.output is None:
            return result("unverified", "no recorded output artifact")
        details["source_seq"] = source.seq
        ref = source.event.result.output
    else:
        # Match the latest call by proposal order in this root attempt, including
        # its same-session descendants. Unrelated and previous attempts cannot help.
        runs, proposed, resolved, terminal, ended = set(), {}, {}, {}, {}
        proposal_counts, resolution_counts, terminal_counts = Counter(), Counter(), Counter()
        for env in events:
            event = env.event
            if isinstance(event, ToolCallProposed):
                proposal_counts[event.call_id] += 1
            if isinstance(event, AgentRunStarted) and (
                    event.run_id == task.run_id or event.parent_run_id in runs):
                runs.add(event.run_id)
            elif isinstance(event, AgentRunFinished) and event.result.run_id in runs:
                runs.discard(event.result.run_id)
                ended[event.result.run_id] = env.seq
            elif isinstance(event, ToolCallProposed) and event.agent_run_id in runs:
                proposed[event.call_id] = env
            elif isinstance(event, DispatchResolved) and event.kind == "tool":
                resolved[event.call_id] = env
                resolution_counts[event.call_id] += 1
            elif isinstance(event, (ToolCallCompleted, ToolCallCancelled, ToolCallAborted)):
                terminal[event.call_id] = env
                terminal_counts[event.call_id] += 1
        if any(proposal_counts[call_id] != 1 for call_id in proposed):
            return result("unverified", "ambiguous reused call ID in this attempt")
        matches = []
        for call_id, proposal in proposed.items():
            effective = resolved.get(call_id, proposal).event
            if (str(effective.tool) == requirement.check.tool and
                    canonical_args(effective.args) == canonical_args(requirement.check.args)):
                matches.append((proposal.seq, call_id))
        if not matches:
            return result("unverified", "no matching tool call in this attempt")
        _, call_id = max(matches)
        source = terminal.get(call_id)
        details.update(call_id=str(call_id), source_seq=source.seq if source else proposed[call_id].seq)
        if call_id not in resolved:
            return result("unverified", "tool was not dispatched")
        if resolution_counts[call_id] != 1 or terminal_counts[call_id] > 1:
            return result("unverified", "ambiguous tool dispatch or result")
        if source is None or not isinstance(source.event, ToolCallCompleted):
            return result("unverified", "tool did not complete")
        proposal = proposed[call_id]
        last = min(ended.get(task.run_id, 0), ended.get(proposal.event.agent_run_id, 0))
        if not proposal.seq < resolved[call_id].seq < source.seq < last:
            return result("unverified", "tool evidence is outside its execution boundary")
        if source.event.is_error:
            return result("failed", "tool reported an error")
        ref = source.event.result_blob
        if ref is None:
            text = source.event.result_text
            if text is None:
                return result("unverified", "tool has no recorded result")
            if len(text) > MAX_EVIDENCE_BYTES:
                return result("unverified", "evidence exceeds 1048576 bytes")
            data = text.encode()
    if ref is not None:
        details["artifact"] = ref
        if ref.size > MAX_EVIDENCE_BYTES:
            return result("unverified", "evidence exceeds 1048576 bytes")
        try:
            data = blobs.get(ref)
        except (MissingBlobError, BlobIntegrityError, OSError):
            return result("unverified", "evidence artifact missing or corrupt")
    if len(data) > MAX_EVIDENCE_BYTES:
        return result("unverified", "evidence exceeds 1048576 bytes")
    actual = hashlib.sha256(data).hexdigest()
    matched = actual == requirement.check.sha256
    return result("passed" if matched else "failed",
                  "recorded bytes match" if matched else "recorded bytes differ", actual_sha256=actual)


def task_summary(task: TrackedTask) -> str:
    state = "accepted by user" if task.accepted else (
        f"{len(task.unresolved)}/{len(task.requirements)} unresolved" if task.requirements else
        "no requirements defined")
    return f"{task.definition.id[:8]}: {task.definition.title} | {state} | execution: {task.execution}"


def render_task(task: TrackedTask) -> str:
    lines = [task_summary(task)]
    for req in task.requirements.values():
        evidence = task.evidence.get(req.id)
        if req.id in task.confirmations:
            seq, note = task.confirmations[req.id]
            detail = f"confirmed by user at event {seq}: {note}"
        elif evidence is not None:
            detail = f"{evidence.status}: {evidence.reason}"
            if evidence.source_seq is not None:
                detail += f"; event {evidence.source_seq}"
            if evidence.artifact is not None:
                detail += f"; blob {evidence.artifact.sha256}"
        else:
            detail = "user review pending" if req.check.kind == "review" else "check pending"
        lines.append(f"  {req.id}: {req.description} — {detail}")
    if task.accepted:
        lines.append(f"User acceptance at event {task.acceptance[0]}: {task.acceptance[1]}")
    lines.append("Evidence applies to this recorded attempt.")
    return "\n".join(lines)
