"""The event taxonomy. Intents vs facts; blocked attempts are recorded, not erased.

Every log line is an Envelope wrapping one event, discriminated on `type`.
Unknown event types parse to UnknownEvent (preserve-and-skip) so a rollback
never makes newer logs unreadable.
"""

from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from harness.blobs import BlobRef
from harness.agent import AgentResult
from harness.handoff import HandoffRecord
from harness.resources import ResourceObservation
from harness.context import ContextPolicy
from harness.improvement import ImprovementRecord
from harness.fallback import FallbackDecision, FallbackPolicy
from harness.scheduling import LocalRequestObservation
from harness.semantics import AssessmentObservation, SemanticObservation
from harness.tasks import RequirementEvidence, TaskDefinition, TaskRequirement
from harness.types import SCHEMA_VERSION, AgentId, CallId, ModelId, SessionId, ToolName
from harness.usage_budget import UsageLimits


class _Event(BaseModel):
    """Base event. Frozen.

    `is_intent` is class-level metadata, not a field: intent events record a
    proposed action whose side effect may already have run by the time of a
    crash, so the log writer fsyncs them. It never serializes and cannot be
    overridden at construction.

    Identity note: events are keyed by (session_id, seq) on the Envelope.
    Events with dict-valued fields are not hashable -- never key by event
    identity.
    """

    model_config = ConfigDict(frozen=True)
    is_intent: ClassVar[bool] = False


# --- session lifecycle ---


class SessionStarted(_Event):
    type: Literal["session_started"] = "session_started"
    parent_session_id: SessionId | None = None
    parent_seq: int | None = None
    default_model: ModelId | None = None


class SessionEnded(_Event):
    type: Literal["session_ended"] = "session_ended"


class SessionResumed(_Event):
    """A new process lifetime reopened this session. Timeline renderers pair
    runs as SessionStarted|SessionResumed ... SessionEnded; the fold ignores it."""

    type: Literal["session_resumed"] = "session_resumed"


class ExecutionConfigured(_Event):
    type: Literal["execution_configured"] = "execution_configured"
    is_intent: ClassVar[bool] = True
    limits: dict

    @field_validator("limits")
    @classmethod
    def validate_limits(cls, value):
        from harness.execution import ExecutionLimits
        ExecutionLimits.from_record(value)
        return value  # Preserve newer fields through serialization; fold only supported limits.


class TaskBudgetExtended(_Event):
    """Durable operator grant for one live run; never restored as authority."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    type: Literal["task_budget_extended"] = "task_budget_extended"
    is_intent: ClassVar[bool] = True
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    task_id: str = Field(min_length=1, max_length=128)
    previous_timeout_seconds: float = Field(gt=0, strict=True)
    timeout_seconds: float = Field(gt=0, strict=True)
    actor: Literal["operator"] = "operator"

    @model_validator(mode="after")
    def increasing(self):
        if self.timeout_seconds <= self.previous_timeout_seconds:
            raise ValueError("task extensions must increase the budget")
        return self


class UsageBudgetConfigured(_Event):
    type: Literal["usage_budget_configured"] = "usage_budget_configured"
    is_intent: ClassVar[bool] = True
    limits: UsageLimits
    untracked_prior_work: int = Field(default=0, ge=0, le=1, strict=True)


class UsageBudgetLinked(_Event):
    type: Literal["usage_budget_linked"] = "usage_budget_linked"
    is_intent: ClassVar[bool] = True
    root_session_id: SessionId


class UsageAttemptStarted(_Event):
    type: Literal["usage_attempt_started"] = "usage_attempt_started"
    is_intent: ClassVar[bool] = True
    id: str
    source_session_id: SessionId
    call_id: CallId
    model: ModelId
    purpose: str
    attempt: int = Field(ge=0, strict=True)
    pricing: dict[str, float]


class UsageAttemptFinished(_Event):
    type: Literal["usage_attempt_finished"] = "usage_attempt_finished"
    id: str
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    complete: bool = Field(strict=True)
    status: Literal["completed", "failed", "cancelled", "aborted"]


class UsageBudgetBlocked(_Event):
    type: Literal["usage_budget_blocked"] = "usage_budget_blocked"
    source_session_id: SessionId
    call_id: CallId
    reason: str


class ModelSelected(_Event):
    """The session's catalog preference, independent of routing/fallback calls."""

    type: Literal["model_selected"] = "model_selected"
    is_intent: ClassVar[bool] = True
    model: ModelId
    pinned: bool


class UserMessage(_Event):
    type: Literal["user_message"] = "user_message"
    text: str


class UserInterrupt(_Event):
    type: Literal["user_interrupt"] = "user_interrupt"


class ResourceObserved(_Event):
    type: Literal["resource_observed"] = "resource_observed"
    observation: ResourceObservation


class LocalRequestObserved(_Event):
    type: Literal["local_request_observed"] = "local_request_observed"
    is_intent: ClassVar[bool] = True
    observation: LocalRequestObservation


class LocalRuntimeRequested(_Event):
    type: Literal["local_runtime_requested"] = "local_runtime_requested"
    is_intent: ClassVar[bool] = True
    alias: str
    config_digest: str
    action: Literal["start", "stop"]


class ContextPolicyConfigured(_Event):
    type: Literal["context_policy_configured"] = "context_policy_configured"
    policy: ContextPolicy | None = None


class FallbackConfigured(_Event):
    type: Literal["fallback_configured"] = "fallback_configured"
    policy: FallbackPolicy | None = None


class FallbackDecided(_Event):
    type: Literal["fallback_decided"] = "fallback_decided"
    is_intent: ClassVar[bool] = True  # Persist the choice before dispatch can begin.
    decision: FallbackDecision


class ContextPrepared(_Event):
    type: Literal["context_prepared"] = "context_prepared"
    task_id: str = ""
    policy_digest: str = ""
    retained_turns: int = 0
    omitted_turns: int = 0
    omitted_messages: int = 0
    input_bytes: int = 0
    max_input_bytes: int = 0
    tools: tuple[str, ...] = ()


class ContextSourceObserved(_Event):
    type: Literal["context_source_observed"] = "context_source_observed"
    task_id: str = ""
    run_id: str = ""
    source_id: str = ""
    policy_digest: str = ""
    tool: str = ""
    call_id: CallId | None = None
    status: Literal["fetching", "ready", "unavailable", "timeout", "oversized", "cancelled"] = "unavailable"
    reason: str = ""
    result: BlobRef | None = None
    byte_count: int = 0
    duration_ms: float = 0


# --- dispatch: intents ---


class ToolCallProposed(_Event):
    type: Literal["tool_call_proposed"] = "tool_call_proposed"
    is_intent: ClassVar[bool] = True
    call_id: CallId
    tool: ToolName
    args: dict[str, Any]
    purpose: Literal["conversation", "agent-task", "context"] = "conversation"
    task_id: str | None = None
    agent_run_id: str | None = None


class ModelCallProposed(_Event):
    type: Literal["model_call_proposed"] = "model_call_proposed"
    is_intent: ClassVar[bool] = True
    call_id: CallId
    model: ModelId
    purpose: str = "conversation"
    execution_kind: Literal["legacy", "inference", "agent"] = "legacy"
    task_id: str | None = None
    agent_run_id: str | None = None


class HookDecided(_Event):
    type: Literal["hook_decided"] = "hook_decided"
    call_id: CallId
    hook: str
    decision: dict[str, Any]  # serialized DispatchDecision, incl. full rewrite payload


class DispatchResolved(_Event):
    type: Literal["dispatch_resolved"] = "dispatch_resolved"
    is_intent: ClassVar[bool] = True
    call_id: CallId
    kind: Literal["tool", "model"]
    tool: ToolName | None = None
    args: dict[str, Any] | None = None
    model: ModelId | None = None


# --- dispatch: facts ---


class ToolCallCompleted(_Event):
    type: Literal["tool_call_completed"] = "tool_call_completed"
    call_id: CallId
    result_text: str | None = None
    result_blob: BlobRef | None = None
    is_error: bool = False
    duration_ms: int = 0


class ToolCallCancelled(_Event):
    type: Literal["tool_call_cancelled"] = "tool_call_cancelled"
    call_id: CallId
    result_text: str = "(call did not complete)"


class ToolCallAborted(_Event):
    """Synthesized on resume for a dangling intent; the fold never guesses outcomes."""

    type: Literal["tool_call_aborted"] = "tool_call_aborted"
    call_id: CallId
    reason: str


class ModelCallStarted(_Event):
    type: Literal["model_call_started"] = "model_call_started"
    call_id: CallId
    model: ModelId
    execution_kind: Literal["legacy", "inference", "agent"] = "legacy"


class ModelCallCompleted(_Event):
    type: Literal["model_call_completed"] = "model_call_completed"
    call_id: CallId
    model: ModelId
    message: dict[str, Any]  # Message.model_dump(); assistant turn incl. tool-call blocks
    usage: dict[str, int | None]  # absent/None is unknown; zero must be measured
    stop_reason: str = "unknown"  # end_turn | tool_use | max_tokens | unknown (additive, default keeps old logs valid)
    pricing: dict[str, float] = Field(
        default_factory=dict
    )  # cost-per-token at call time; {} when unknown
    duration_ms: int = 0
    purpose: str = "conversation"
    execution_kind: Literal["legacy", "inference", "agent"] = "legacy"
    task_id: str | None = None
    agent_run_id: str | None = None


class ModelCallCancelled(_Event):
    type: Literal["model_call_cancelled"] = "model_call_cancelled"
    call_id: CallId
    reason: str = "cancelled"
    duration_ms: int = 0


class ModelCallFailed(_Event):
    type: Literal["model_call_failed"] = "model_call_failed"
    call_id: CallId
    model: ModelId | None = None
    error_type: str = "provider_error"
    message: str = ""
    retryable: bool = False
    duration_ms: int = 0


class ModelCorrectionRequested(_Event):
    """Durable feedback for a rejected inference response; never a tool result."""

    type: Literal["model_correction_requested"] = "model_correction_requested"
    is_intent: ClassVar[bool] = True
    failed_call_id: CallId | None = None
    task_id: str = ""
    agent_run_id: str = ""
    attempt: int = 0
    instruction: str = (
        "Harness rejected the previous response because it proposed more than one tool call. "
        "None of those tool calls ran. Propose exactly one next tool call and wait for its result. "
        "Read needed source information before constructing a write; do not guess its contents."
    )


class ModelCallAborted(_Event):
    """Resume-time repair; an interrupted external agent may have performed work."""

    type: Literal["model_call_aborted"] = "model_call_aborted"
    call_id: CallId
    reason: str


# --- permissions ---


class PermissionRequested(_Event):
    type: Literal["permission_requested"] = "permission_requested"
    is_intent: ClassVar[bool] = True
    call_id: CallId
    reason: str


class PermissionResolved(_Event):
    type: Literal["permission_resolved"] = "permission_resolved"
    call_id: CallId
    allowed: bool
    resolver: str


# --- subagents ---


class SubagentSpawned(_Event):
    type: Literal["subagent_spawned"] = "subagent_spawned"
    # the child session is a side effect: the spawn record (causal link) must
    # survive a crash, so it gets the intent fsync
    is_intent: ClassVar[bool] = True
    child_session_id: SessionId
    # the dispatch_agent / ensemble / consult_panel / escalate call that caused
    # this spawn. Additive-optional: absent in logs written before this field.
    call_id: CallId | None = None
    agent: AgentId | None = None
    model: ModelId | None = None


class SubagentFinished(_Event):
    type: Literal["subagent_finished"] = "subagent_finished"
    child_session_id: SessionId
    status: Literal["ok", "error", "cancelled", "incomplete"]
    run_id: str | None = None
    output: BlobRef | None = None  # Stored in child_session_id's blob directory.
    reason: str = ""
    truncated: bool = False


class CoordinationStarted(_Event):
    type: Literal["coordination_started"] = "coordination_started"
    id: str
    call_id: CallId | None = None
    strategy: str
    depth: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


class CoordinationFinished(_Event):
    type: Literal["coordination_finished"] = "coordination_finished"
    id: str
    call_id: CallId | None = None
    strategy: str
    status: Literal["completed", "incomplete", "failed", "blocked", "cancelled"]
    report: BlobRef


class AgentRunStarted(_Event):
    type: Literal["agent_run_started"] = "agent_run_started"
    is_intent: ClassVar[bool] = True
    task_id: str
    run_id: str
    parent_run_id: str | None = None
    runtime: str
    agent: AgentId | None = None
    model: ModelId | None = None
    acceptance_criteria: tuple[str, ...] = ()
    limits: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    handoff_id: str | None = None


class AgentRunFinished(_Event):
    type: Literal["agent_run_finished"] = "agent_run_finished"
    result: AgentResult
    purpose: Literal["task", "conversation"] = "task"


class TaskHandoffRecorded(_Event):
    type: Literal["task_handoff_recorded"] = "task_handoff_recorded"
    is_intent: ClassVar[bool] = True
    record: HandoffRecord


# --- transcript transforms ---


class CompactionApplied(_Event):
    """The full summary and exact replaced range are facts; a fold never re-summarizes."""

    type: Literal["compaction_applied"] = "compaction_applied"
    from_seq: int
    to_seq: int
    summary: str
    model: ModelId | None = None


# --- outcomes ---


class TaskCreated(_Event):
    type: Literal["task_created"] = "task_created"
    is_intent: ClassVar[bool] = True
    definition: TaskDefinition | None = None


class TaskSelected(_Event):
    type: Literal["task_selected"] = "task_selected"
    is_intent: ClassVar[bool] = True
    task_id: str | None = None


class TaskRequirementAdded(_Event):
    type: Literal["task_requirement_added"] = "task_requirement_added"
    is_intent: ClassVar[bool] = True
    task_id: str = ""
    requirement: TaskRequirement | None = None


class TaskChecked(_Event):
    type: Literal["task_checked"] = "task_checked"
    is_intent: ClassVar[bool] = True
    task_id: str = ""
    basis_seq: int = 0
    evidence: tuple[RequirementEvidence, ...] = ()


class TaskRequirementConfirmed(_Event):
    type: Literal["task_requirement_confirmed"] = "task_requirement_confirmed"
    is_intent: ClassVar[bool] = True
    task_id: str = ""
    basis_seq: int = 0
    requirement_id: str = ""
    note: str = ""


class TaskAccepted(_Event):
    type: Literal["task_accepted"] = "task_accepted"
    is_intent: ClassVar[bool] = True
    task_id: str = ""
    basis_seq: int = 0
    note: str = ""


class TaskOutcome(_Event):
    type: Literal["task_outcome"] = "task_outcome"
    status: Literal["ok", "fail", "abandoned"]
    score: float | None = None
    judge: str | None = None
    note: str = ""


class SessionOutcome(_Event):
    type: Literal["session_outcome"] = "session_outcome"
    status: Literal["ok", "fail", "abandoned"]
    score: float | None = None
    judge: str | None = None
    note: str = ""


# --- errors, retries, extensibility ---


class ErrorRaised(_Event):
    type: Literal["error_raised"] = "error_raised"
    where: str
    message: str


class RetryAttempted(_Event):
    type: Literal["retry_attempted"] = "retry_attempted"
    call_id: CallId
    attempt: int
    reason: str


class CustomEvent(_Event):
    """Plugin-emitted, namespaced. How anything becomes telemetry-visible."""

    type: Literal["custom"] = "custom"
    namespace: str
    name: str
    data: dict[str, Any]


class TodoListUpdated(_Event):
    """Whole-list replacement of the native todo state. Native (not a plugin CustomEvent):
    todo is a first-class kernel tool, so it earns a typed event. Folded last-write-wins."""

    type: Literal["todo_list_updated"] = "todo_list_updated"
    items: list[dict[str, Any]]


class EvaluationRunStarted(_Event):
    type: Literal["evaluation_run_started"] = "evaluation_run_started"
    is_intent: ClassVar[bool] = True
    run_id: str
    plan_id: str
    incumbent: BlobRef
    configuration: BlobRef


class EvaluationRunFinished(_Event):
    type: Literal["evaluation_run_finished"] = "evaluation_run_finished"
    run_id: str
    status: Literal["completed", "cancelled", "timed_out", "failed", "aborted"]
    result_id: str | None = None


class SemanticObserved(_Event):
    type: Literal["semantic_observed"] = "semantic_observed"
    observation: SemanticObservation


class AssessmentObserved(_Event):
    type: Literal["assessment_observed"] = "assessment_observed"
    observation: AssessmentObservation


class ImprovementRecorded(_Event):
    """Core evidence/candidate/experiment fact. It never authorizes activation."""

    type: Literal["improvement_recorded"] = "improvement_recorded"
    is_intent: ClassVar[bool] = True  # Plans must be durable before an experiment starts.
    record: ImprovementRecord


class UnknownEvent(_Event):
    """A type this binary doesn't know. Raw JSON retained; never dropped."""

    type: Literal["unknown"] = "unknown"
    raw: dict[str, Any]


Event = Annotated[
    Union[
        ExecutionConfigured,
        TaskBudgetExtended,
        UsageBudgetConfigured,
        UsageBudgetLinked,
        UsageAttemptStarted,
        UsageAttemptFinished,
        UsageBudgetBlocked,
        SessionStarted,
        SessionEnded,
        SessionResumed,
        ModelSelected,
        UserMessage,
        UserInterrupt,
        ToolCallProposed,
        ModelCallProposed,
        HookDecided,
        DispatchResolved,
        ToolCallCompleted,
        ToolCallCancelled,
        ToolCallAborted,
        ModelCallStarted,
        ModelCallCompleted,
        ModelCallCancelled,
        ModelCallFailed,
        ModelCorrectionRequested,
        ModelCallAborted,
        PermissionRequested,
        PermissionResolved,
        SubagentSpawned,
        SubagentFinished,
        CoordinationStarted,
        CoordinationFinished,
        AgentRunStarted,
        AgentRunFinished,
        TaskHandoffRecorded,
        ResourceObserved,
        LocalRequestObserved,
        LocalRuntimeRequested,
        ContextPolicyConfigured,
        FallbackConfigured,
        FallbackDecided,
        ContextPrepared,
        ContextSourceObserved,
        CompactionApplied,
        TaskCreated,
        TaskSelected,
        TaskRequirementAdded,
        TaskChecked,
        TaskRequirementConfirmed,
        TaskAccepted,
        TaskOutcome,
        SessionOutcome,
        ErrorRaised,
        RetryAttempted,
        CustomEvent,
        TodoListUpdated,
        ImprovementRecorded,
        SemanticObserved,
        AssessmentObserved,
        EvaluationRunStarted,
        EvaluationRunFinished,
        UnknownEvent,
    ],
    Field(discriminator="type"),
]


class Envelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    v: int = SCHEMA_VERSION
    session_id: SessionId
    seq: int
    ts: float
    event: Event


class _LaxEnvelope(BaseModel):
    """Fallback shape for preserve-and-skip parsing."""

    v: int = SCHEMA_VERSION
    session_id: SessionId
    seq: int
    ts: float
    event: dict[str, Any]


def parse_envelope_line(line: str) -> Envelope:
    """Parse one log line.

    Unknown EVENT types degrade to UnknownEvent (preserve-and-skip).
    A malformed ENVELOPE (invalid JSON, missing/invalid v/session_id/seq/ts)
    raises pydantic.ValidationError: envelope corruption must fail loudly,
    never be silently absorbed. Torn-tail handling lives in the log reader.
    """
    try:
        return Envelope.model_validate_json(line)
    except ValidationError:
        lax = _LaxEnvelope.model_validate_json(line)
        return Envelope(
            v=lax.v,
            session_id=lax.session_id,
            seq=lax.seq,
            ts=lax.ts,
            event=UnknownEvent(raw=lax.event),
        )
