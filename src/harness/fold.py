"""Fold facts into state. Never executes side effects; never runs hooks.

Assumes a well-formed log: seqs strictly increasing and unique (the writer's
single monotonic counter guarantees this; the reader's repair path truncates,
never duplicates). Unknown EVENT types are skipped (UnknownEvent falls through
the dispatch); unknown BLOCK kinds inside ModelCallCompleted.message fail
loudly in Message.model_validate — a conscious asymmetry: new block kinds
require a binary upgrade, new event types must not break old readers.
"""

from dataclasses import dataclass, field

from harness.events import (
    AgentRunFinished,
    AgentRunStarted,
    CompactionApplied,
    DispatchResolved,
    Envelope,
    Event,
    EvaluationRunStarted,
    EvaluationRunFinished,
    ImprovementRecorded,
    ModelCallAborted,
    ModelCallCancelled,
    ModelCallCompleted,
    ModelCallFailed,
    ModelCorrectionRequested,
    ModelCallProposed,
    ResourceObserved,
    ContextPolicyConfigured,
    FallbackConfigured,
    FallbackDecided,
    TodoListUpdated,
    ToolCallAborted,
    ToolCallCancelled,
    ToolCallCompleted,
    ToolCallProposed,
    UserMessage,
)
from harness.messages import Message
from harness.context import ContextPolicy
from harness.fallback import FallbackDecision, FallbackPolicy
from harness.improvement import ExperimentResult
from harness.agent import AgentResult
from harness.types import CallId
from harness.tasks import TaskState


@dataclass
class FoldedState:
    tasks: TaskState = field(default_factory=TaskState)
    messages: list[Message] = field(default_factory=list)
    # call_id -> seq of the proposing event; an intent with no terminal fact
    open_intents: dict[CallId, int] = field(default_factory=dict)
    open_model_intents: dict[CallId, int] = field(default_factory=dict)
    open_agent_runs: dict[str, AgentRunStarted] = field(default_factory=dict)
    agent_runs: dict[str, AgentResult] = field(default_factory=dict)
    # Historical observations are evidence only; live readiness must recheck.
    resources: dict = field(default_factory=dict)
    context_policy: ContextPolicy | None = None
    fallback_policy: FallbackPolicy | None = None
    fallback_decisions: list[FallbackDecision] = field(default_factory=list)
    open_evaluations: dict[str, EvaluationRunStarted] = field(default_factory=dict)
    evaluation_results: dict[str, ExperimentResult] = field(default_factory=dict)
    last_seq: int = 0
    # seq -> index range bookkeeping for compaction
    _msg_seqs: list[int] = field(default_factory=list)
    # paths read or written successfully this session (read-before-edit gate, R-C1).
    # NOTE: these are as-recorded path strings, canonical ONLY when WorkspaceGuard ran.
    # Resume seeding MUST resolve each against the workspace root (resolve_in_workspace)
    # and silently drop unresolvable ones before constructing ReadState (wiring: Task 8).
    read_paths: set[str] = field(default_factory=set)
    # call_id -> file_path for in-flight read_file/write_file proposals
    _read_intents: dict[CallId, str] = field(default_factory=dict)
    # External runtime tool results are audited but are not native-loop replies.
    _agent_tool_calls: set[CallId] = field(default_factory=set)
    # native todo list: last-write-wins from TodoListUpdated events
    todos: list[dict] = field(default_factory=list)

    def _append(self, seq: int, message: Message) -> None:
        self.messages.append(message)
        self._msg_seqs.append(seq)


def fold(envelopes: list[Envelope]) -> FoldedState:
    state = FoldedState()
    for env in envelopes:
        state.tasks.apply(env)
        ev = env.event
        state.last_seq = max(state.last_seq, env.seq)
        if isinstance(ev, UserMessage):
            state._append(env.seq, Message.user_text(ev.text))
        elif isinstance(ev, ModelCorrectionRequested):
            state._append(env.seq, Message.system_text(ev.instruction))
        elif isinstance(ev, ModelCallProposed):
            state.open_model_intents[ev.call_id] = env.seq
        elif isinstance(ev, AgentRunStarted):
            state.open_agent_runs[ev.run_id] = ev
        elif isinstance(ev, ContextPolicyConfigured):
            state.context_policy = ev.policy
        elif isinstance(ev, FallbackConfigured):
            state.fallback_policy = ev.policy
        elif isinstance(ev, FallbackDecided):
            state.fallback_decisions.append(ev.decision)
        elif isinstance(ev, EvaluationRunStarted):
            state.open_evaluations[ev.run_id] = ev
        elif isinstance(ev, EvaluationRunFinished):
            state.open_evaluations.pop(ev.run_id, None)
        elif isinstance(ev, ImprovementRecorded) and isinstance(ev.record, ExperimentResult):
            if ev.record.run_id is not None:
                state.evaluation_results[ev.record.run_id] = ev.record
        elif isinstance(ev, ResourceObserved):
            state.resources[ev.observation.alias] = ev.observation
        elif isinstance(ev, AgentRunFinished):
            state.open_agent_runs.pop(ev.result.run_id, None)
            state.agent_runs[ev.result.run_id] = ev.result
            if ev.purpose == "conversation" and ev.result.response is not None:
                state._append(env.seq, ev.result.response)
        elif isinstance(ev, ModelCallCompleted):
            state.open_model_intents.pop(ev.call_id, None)
            if ev.purpose == "conversation":
                state._append(env.seq, Message.model_validate(ev.message))
        elif isinstance(ev, (ModelCallFailed, ModelCallCancelled, ModelCallAborted)):
            state.open_model_intents.pop(ev.call_id, None)
        elif isinstance(ev, ToolCallProposed):
            state.open_intents[ev.call_id] = env.seq
            if ev.purpose in {"agent-task", "context"}:
                state._agent_tool_calls.add(ev.call_id)
            if str(ev.tool) in ("read_file", "write_file"):
                fp = ev.args.get("file_path")
                if fp is not None:
                    state._read_intents[ev.call_id] = str(fp)
        elif isinstance(ev, DispatchResolved):
            if ev.kind == "tool" and str(ev.tool) in ("read_file", "write_file"):
                fp = (ev.args or {}).get("file_path")
                if fp is not None:
                    state._read_intents[ev.call_id] = str(fp)
        elif isinstance(ev, ToolCallCompleted):
            path = state._read_intents.pop(ev.call_id, None)
            if path is not None and not ev.is_error:
                state.read_paths.add(path)
            state.open_intents.pop(ev.call_id, None)
            if ev.call_id in state._agent_tool_calls:
                state._agent_tool_calls.discard(ev.call_id)
                continue
            state._append(
                env.seq,
                Message.tool_result(
                    ev.call_id, text=ev.result_text, blob=ev.result_blob, is_error=ev.is_error
                ),
            )
        elif isinstance(ev, (ToolCallCancelled, ToolCallAborted)):
            state.open_intents.pop(ev.call_id, None)
            if ev.call_id in state._agent_tool_calls:
                state._agent_tool_calls.discard(ev.call_id)
                continue
            state._append(
                env.seq,
                Message.tool_result(
                    ev.call_id, text=(ev.result_text if isinstance(ev, ToolCallCancelled)
                                      else "(call did not complete)"), is_error=True,
                ),
            )
        elif isinstance(ev, CompactionApplied):
            kept_msgs, kept_seqs = [], []
            for msg, seq in zip(state.messages, state._msg_seqs):
                if not (ev.from_seq <= seq <= ev.to_seq):
                    kept_msgs.append(msg)
                    kept_seqs.append(seq)
            summary = Message.system_text(f"Summary of earlier conversation: {ev.summary}")
            state.messages = [summary, *kept_msgs]
            state._msg_seqs = [env.seq, *kept_seqs]
        elif isinstance(ev, TodoListUpdated):
            state.todos = [dict(item) for item in ev.items]
    return state


def resume_repairs(state: FoldedState) -> list[Event]:
    """Close dangling tool and model intents in proposal order, without replaying work.

    Append these facts before continuing live. Only tool repairs add transcript
    results. A model repair also represents uncertain external-agent side effects.
    """
    repairs = [
        (seq, ToolCallAborted(call_id=call_id, reason="dangling intent at resume (crash?)"))
        for call_id, seq in state.open_intents.items()
    ] + [
        (seq, ModelCallAborted(call_id=call_id, reason="dangling intent at resume (crash?)"))
        for call_id, seq in state.open_model_intents.items()
    ]
    return [event for _, event in sorted(repairs, key=lambda pair: pair[0])] + [
        AgentRunFinished(result=AgentResult(
            task_id=run.task_id, run_id=run.run_id, status="aborted",
            reason="interrupted run at resume; side effects require reconciliation",
            remaining_criteria=run.acceptance_criteria,
        ))
        for run in reversed(tuple(state.open_agent_runs.values()))
    ] + [
        EvaluationRunFinished(run_id=run_id,
            status=state.evaluation_results[run_id].completion if run_id in state.evaluation_results else "aborted",
            result_id=state.evaluation_results[run_id].id if run_id in state.evaluation_results else None)
        for run_id in state.open_evaluations
    ]
