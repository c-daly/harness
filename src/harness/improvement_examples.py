"""Read-only historical examples backed by ordinary task and improvement records."""

from dataclasses import dataclass
import hashlib
import json

from harness.events import (
    AgentRunFinished, AgentRunStarted, DispatchResolved, ImprovementRecorded,
    SessionResumed, SessionStarted, TaskChecked, TaskCreated, ToolCallAborted,
    ToolCallCancelled, ToolCallCompleted, ToolCallProposed, UserMessage,
)
from harness.improvement import Evidence
from harness.improvement_journal import project_improvements
from harness.improvement_outcomes import finished_replies
from harness.log import read_session
from harness.project_binding import read_project_session, same_project
from harness.semantic_assessment import ContextCandidate, ContextSelectionInput
from harness.tasks import TaskState


@dataclass(frozen=True)
class ImprovementExample:
    session_id: str
    situation: str
    action: str
    outcome: str
    situation_seq: int
    action_seq: int
    result_seq: int
    evidence_seq: int
    evidence_id: str

    @property
    def id(self):
        origin = [self.session_id, self.situation_seq, self.action_seq, self.result_seq, self.evidence_seq]
        return hashlib.sha256(json.dumps(origin).encode()).hexdigest()

    def context_candidate(self):
        # The historical record is available and verified at retrieval. Its
        # contents describe an earlier task, never a current-world assertion.
        summary = f"Situation\n{self.situation}\nAction or check\n{self.action}\nHistorical outcome\n{self.outcome}"
        return ContextCandidate(id=self.id, summary=summary, freshness="current")


def _checked_examples(envelopes, evidence_ids, allowed_runs, visible):
    tasks = TaskState()
    created = {}
    examples = {}
    for env in envelopes:
        event = env.event
        tasks.apply(env)
        if isinstance(event, TaskCreated) and event.definition:
            created.setdefault(event.definition.id, env.seq)
        elif isinstance(event, TaskChecked) and env.seq in evidence_ids:
            task = tasks.items.get(event.task_id)
            if (not task or event.basis_seq != task.basis_seq or task.open_runs
                    or allowed_runs is not None and task.run_id not in allowed_runs):
                continue
            for key, check in task.evidence.items():
                if check.source_seq is not None and not 0 < check.source_seq < env.seq:
                    continue
                requirement = task.requirements[key]
                method = requirement.check
                action = (f"{method.tool} — {requirement.description}" if method.kind == "tool_result"
                          else f"Check recorded output — {requirement.description}")
                # A recheck for acceptance can repeat the same historical case.
                example = ImprovementExample(
                    env.session_id, task.definition.title[:320], action[:256],
                    f"Requirement {check.status}. {check.reason[:160]}",
                    created[event.task_id], task.declared_at[key], check.source_seq or env.seq,
                    env.seq, evidence_ids[env.seq],
                )
                if visible is None or {example.situation_seq, example.action_seq, example.result_seq, env.seq} <= visible:
                    examples[task.definition.id, task.run_id, key] = example
    return examples.values()


def _tool_examples(envelopes, evidence_ids, allowed_runs, visible):
    # A tool result alone cannot certify the final answer. Retain its actual
    # execution and provenance; omit tool output and previous assistant text.
    finishes = {env.event.result.run_id: env for env in finished_replies(envelopes)}
    live = set()
    requests = {}
    proposals, resolutions, terminals = {}, {}, {}
    ambiguous = set()
    for env in envelopes:
        event = env.event
        if isinstance(event, AgentRunStarted):
            live.add(event.run_id)
        elif isinstance(event, AgentRunFinished):
            live.discard(event.result.run_id)
        elif isinstance(event, UserMessage) and len(live) == 1:
            current, = live
            if current in finishes:
                requests[current] = env if current not in requests else None
        elif isinstance(event, (ToolCallProposed, DispatchResolved, ToolCallCompleted, ToolCallCancelled, ToolCallAborted)):
            if isinstance(event, ToolCallProposed):
                group = proposals
            elif isinstance(event, DispatchResolved):
                if event.kind != "tool":
                    continue
                group = resolutions
            else:
                group = terminals
            if event.call_id in group:
                ambiguous.add(event.call_id)
            group[event.call_id] = env

    finish_by_seq = {env.seq: run for run, env in finishes.items()}
    confirmed = set()
    for env in envelopes:
        if not isinstance(env.event, ImprovementRecorded) or visible is not None and env.seq not in visible:
            continue
        failure = env.event.record
        if (not isinstance(failure, Evidence) or failure.category != "failure" or failure.source_session != env.session_id
                or failure.source_seq >= env.seq or failure.source_seq not in finish_by_seq
                or not failure.observation.startswith("Operator-confirmed flawed response: ")):
            continue
        run = finish_by_seq[failure.source_seq]
        request = requests.get(run)
        digest = hashlib.sha256(f"{env.session_id}\0{failure.source_seq}\0{failure.observation}".encode()).hexdigest()
        if (request is None or failure.id != f"failure:{failure.source_seq}:{digest}"
                or allowed_runs is not None and run not in allowed_runs):
            continue
        confirmed.add(run)
        yield ImprovementExample(env.session_id, request.event.text[:320], "The primary delivered a response.",
            failure.observation[:320], request.seq, failure.source_seq, failure.source_seq, env.seq, failure.id)

    for call_id, proposal in proposals.items():
        call = proposal.event
        request = requests.get(call.agent_run_id)
        finish = finishes.get(call.agent_run_id)
        resolved, terminal = resolutions.get(call_id), terminals.get(call_id)
        if (call_id in ambiguous or call.agent_run_id in confirmed or request is None or finish is None or finish.seq not in evidence_ids
                or allowed_runs is not None and call.agent_run_id not in allowed_runs
                or call.task_id != finish.event.result.task_id or call.purpose == "context"
                or resolved is None or terminal is None or not isinstance(terminal.event, ToolCallCompleted)
                or not request.seq < proposal.seq < resolved.seq < terminal.seq < finish.seq):
            continue
        tool, args = resolved.event.tool, resolved.event.args or {}
        if tool not in {"read_file", "glob", "grep"}:
            continue
        # Only the explicitly recorded scope of the read/search is useful here.
        # Search terms, file contents, commands and model outputs can contain unrelated private text.
        target = args.get("file_path" if tool == "read_file" else "path")
        action = f"{tool} on {target[:160]}" if isinstance(target, str) and target else str(tool)
        returned = "returned an error" if terminal.event.is_error else "returned a result"
        yield ImprovementExample(
            finish.session_id, request.event.text[:320], action,
            f"The tool {returned}. Primary execution ended {finish.event.result.status}. Answer correctness was unverified.",
            request.seq, resolved.seq, terminal.seq, finish.seq, evidence_ids[finish.seq],
        )


def session_examples(base, session_id, *, before_seq=None, project=None, max_bytes=None,
                     prefix=None, same_project_only=False, after_run_seq=0) -> tuple[ImprovementExample, ...]:
    """Read one caller-authorized session. All facts must precede the optional cutoff."""
    if prefix is not None and project is None:
        raise ValueError("Cross-session examples require a recorded repository.")
    if project is None and not same_project_only:
        envelopes = read_session(base, session_id, repair=False)
    else:
        envelopes = read_project_session(base, session_id, project, max_bytes=max_bytes, prefix=prefix)
    if before_seq is not None:
        envelopes = [env for env in envelopes if env.seq < before_seq]
    state = project_improvements(envelopes)
    evidence_ids = {e.source_seq: e.id for e in state.evidence.values()
                    if e.category == "task_outcome" and e.source_session == session_id}
    allowed = None
    visible = None
    if project is not None or same_project_only:
        from collections import Counter
        starts = [e for e in envelopes if isinstance(e.event, AgentRunStarted)]
        counts = Counter(e.event.run_id for e in starts)
        allowed = {e.event.run_id for e in starts if counts[e.event.run_id] == 1 and e.event.parent_run_id is None
                   and e.event.runtime == "harness" and e.event.agent in (None, "saoirse")
                   and same_project(e.event.capabilities.get("project"), project)
                   and (project is not None or e.seq > after_run_seq)}
        visible = set()
        in_scope = False
        for env in envelopes:
            event = env.event
            if isinstance(event, SessionStarted):
                in_scope = same_project(event.project, project)
            elif isinstance(event, SessionResumed):
                in_scope = False
            elif isinstance(event, AgentRunStarted) and event.parent_run_id is None:
                in_scope = event.run_id in allowed
            if in_scope and (project is not None or env.seq > after_run_seq):
                visible.add(env.seq)
    checked = _checked_examples(envelopes, evidence_ids, allowed, visible)
    checked_results = {example.result_seq for example in checked}
    examples = [*checked, *(e for e in _tool_examples(envelopes, evidence_ids, allowed, visible)
                               if e.result_seq not in checked_results)]
    if visible is not None:
        examples = [e for e in examples if {e.situation_seq, e.action_seq, e.result_seq, e.evidence_seq} <= visible]
    examples.sort(key=lambda e: (e.evidence_seq, e.result_seq, e.action_seq), reverse=True)
    return tuple(examples)


async def select_examples(semantics, *, model, query, examples, failure_review=None, enabled=True):
    """Run native advisory context selection on an already authorized, ordered pool."""
    candidates = []
    available = {}
    for example in examples:
        if example.id in available:
            continue
        candidate = example.context_candidate()
        candidates.append(candidate)
        available[example.id] = example
        if len(candidates) == 16:
            break
    data = ContextSelectionInput(query=query, candidates=tuple(candidates))
    options = {"failure_review": failure_review} if failure_review is not None else {}
    if not enabled:
        options["enabled"] = False
    observation = await semantics.select_context(data, model=model, exact_model=True, **options)
    ids = observation.result.selected_ids if observation.status == "ok" and observation.result else ()
    return observation, tuple(available[key] for key in ids)
