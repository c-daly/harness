"""The event taxonomy. Intents vs facts; blocked attempts are recorded, not erased.

Every log line is an Envelope wrapping one event, discriminated on `type`.
Unknown event types parse to UnknownEvent (preserve-and-skip) so a rollback
never makes newer logs unreadable.
"""

from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.blobs import BlobRef
from harness.types import SCHEMA_VERSION, AgentId, CallId, ModelId, SessionId, ToolName


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


class UserMessage(_Event):
    type: Literal["user_message"] = "user_message"
    text: str


class UserInterrupt(_Event):
    type: Literal["user_interrupt"] = "user_interrupt"


# --- dispatch: intents ---

class ToolCallProposed(_Event):
    type: Literal["tool_call_proposed"] = "tool_call_proposed"
    is_intent: ClassVar[bool] = True
    call_id: CallId
    tool: ToolName
    args: dict[str, Any]


class ModelCallProposed(_Event):
    type: Literal["model_call_proposed"] = "model_call_proposed"
    is_intent: ClassVar[bool] = True
    call_id: CallId
    model: ModelId


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


class ToolCallAborted(_Event):
    """Synthesized on resume for a dangling intent; the fold never guesses outcomes."""
    type: Literal["tool_call_aborted"] = "tool_call_aborted"
    call_id: CallId
    reason: str


class ModelCallStarted(_Event):
    type: Literal["model_call_started"] = "model_call_started"
    call_id: CallId
    model: ModelId


class ModelCallCompleted(_Event):
    type: Literal["model_call_completed"] = "model_call_completed"
    call_id: CallId
    model: ModelId
    message: dict[str, Any]  # Message.model_dump(); assistant turn incl. tool-call blocks
    usage: dict[str, int]    # input_tokens / output_tokens / cache_read_tokens / cache_write_tokens
    stop_reason: str = "unknown"  # end_turn | tool_use | max_tokens | unknown (additive, default keeps old logs valid)
    pricing: dict[str, float] = Field(default_factory=dict)  # cost-per-token at call time; {} when unknown
    duration_ms: int = 0


class ModelCallCancelled(_Event):
    type: Literal["model_call_cancelled"] = "model_call_cancelled"
    call_id: CallId


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
    agent: AgentId | None = None
    model: ModelId | None = None


class SubagentFinished(_Event):
    type: Literal["subagent_finished"] = "subagent_finished"
    child_session_id: SessionId
    status: Literal["ok", "error", "cancelled"]


# --- transcript transforms ---

class CompactionApplied(_Event):
    """The full summary and exact replaced range are facts; a fold never re-summarizes."""
    type: Literal["compaction_applied"] = "compaction_applied"
    from_seq: int
    to_seq: int
    summary: str
    model: ModelId | None = None


# --- outcomes ---

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


class UnknownEvent(_Event):
    """A type this binary doesn't know. Raw JSON retained; never dropped."""
    type: Literal["unknown"] = "unknown"
    raw: dict[str, Any]


Event = Annotated[
    Union[
        SessionStarted, SessionEnded, SessionResumed, UserMessage, UserInterrupt,
        ToolCallProposed, ModelCallProposed, HookDecided, DispatchResolved,
        ToolCallCompleted, ToolCallCancelled, ToolCallAborted,
        ModelCallStarted, ModelCallCompleted, ModelCallCancelled,
        PermissionRequested, PermissionResolved,
        SubagentSpawned, SubagentFinished,
        CompactionApplied, TaskOutcome, SessionOutcome,
        ErrorRaised, RetryAttempted, CustomEvent,
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
