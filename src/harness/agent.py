"""Owned agent tasks, execution outcomes, and durable run boundaries.

An execution returning an answer does not verify the user's acceptance criteria.
Runtime implementations bind their own dispatcher/scope; task input grants no
tools, permissions, or budget beyond that existing binding.
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from harness.blobs import BlobRef, BlobStore
from harness.execution import BudgetExceeded
from harness.messages import Message
from harness.provider import Chunk, Usage
from harness.types import AgentId, ModelId

if TYPE_CHECKING:
    from harness.session import Session


class TaskLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    max_iterations: int = Field(default=20, ge=0, strict=True)
    timeout_seconds: float = Field(default=600, gt=0)
    max_input_bytes: int = Field(default=4 * 1024 * 1024, gt=0, strict=True)
    max_response_bytes: int = Field(default=1024 * 1024, gt=0, strict=True)
    max_output_tokens: int = Field(default=4096, gt=0, strict=True)
    max_stream_chunks: int = Field(default=65536, gt=0, strict=True)


class AgentTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=128_000)
    context: tuple[Message, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    agent: AgentId | None = None
    limits: TaskLimits = Field(default_factory=TaskLimits)


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    run_id: str
    status: Literal["completed", "incomplete", "failed", "cancelled", "aborted"]
    reason: str = ""
    output: BlobRef | None = None
    # Optional bounded assistant response for transcript replay, including reasoning.
    response: Message | None = None
    usage: Usage = Field(default_factory=Usage)
    acceptance: Literal["unverified"] = "unverified"
    remaining_criteria: tuple[str, ...] = ()

    def read_text(self, blobs: BlobStore) -> str:
        return blobs.get(self.output).decode("utf-8") if self.output is not None else ""


@dataclass(frozen=True)
class AgentProgress:
    task_id: str
    run_id: str
    phase: Literal["inference", "execution", "tools", "stream", "correction"]
    iteration: int
    chunk: Chunk | None = None


class AgentRuntime(Protocol):
    async def run_task(
        self, task: AgentTask, *, on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentResult: ...


@dataclass(frozen=True)
class ActiveAgentRun:
    task: AgentTask
    run_id: str
    runtime: str = "harness"


current_agent_run: ContextVar[ActiveAgentRun | None] = ContextVar("agent_run", default=None)


@dataclass(frozen=True)
class AgentOutput:
    text: str
    status: Literal["completed", "incomplete"] = "completed"
    reason: str = ""
    usage: Usage = Usage()
    response: Message | None = None


def add_usage(total: Usage, increment: Usage) -> Usage:
    return Usage(**{key: None if value is None or increment.as_dict()[key] is None
                    else value + increment.as_dict()[key] for key, value in total.as_dict().items()})


async def execute_task(
    session: "Session", task: AgentTask, *, runtime: str, model: ModelId | None,
    execute: Callable[[], Awaitable[AgentOutput]],
    purpose: Literal["task", "conversation"] = "task",
    capabilities: dict | None = None,
) -> AgentResult:
    """Record one run. Failures/cancellation propagate after recording their outcome.

    Runtime cleanup belongs inside execute and must settle before it returns or
    raises. Publication of the final output precedes the terminal fact; failure
    of the terminal write is not misreported as a second terminal outcome.
    """
    from harness.events import AgentRunFinished, AgentRunStarted
    from harness.tasks import TaskService
    task = AgentTask.model_validate(task.model_dump())
    TaskService(session).validate_run(task)
    run_id = uuid4().hex
    parent = current_agent_run.get()
    session.append(AgentRunStarted(task_id=task.id, run_id=run_id, runtime=runtime,
                                   parent_run_id=parent.run_id if parent else None,
                                   agent=task.agent, model=model,
                                   acceptance_criteria=task.acceptance_criteria,
                                   limits=task.limits.model_dump(), capabilities=capabilities or {}))
    token = current_agent_run.set(ActiveAgentRun(task, run_id, runtime))

    def terminal(status, reason="", **kwargs):
        return AgentResult(task_id=task.id, run_id=run_id, status=status, reason=reason,
                           remaining_criteria=task.acceptance_criteria, **kwargs)

    try:
        try:
            async with asyncio.timeout(task.limits.timeout_seconds):
                output = await execute()
            payload = output.text.encode("utf-8")
            if len(payload) > task.limits.max_response_bytes:
                raise BudgetExceeded("agent result exceeds its output limit")
            response = Message.model_validate(output.response.model_dump()) if output.response else None
            if response is not None and (response.role != "assistant" or response.text() != output.text):
                raise ValueError("agent response must match its assistant output")
            result = terminal(output.status, output.reason, output=session.blobs.put(payload),
                              usage=output.usage, response=response)
        except asyncio.CancelledError:
            session.append(AgentRunFinished(result=terminal("cancelled", "cancelled")))
            raise
        except (TimeoutError, BudgetExceeded) as exc:
            reason = "deadline" if isinstance(exc, TimeoutError) else "budget"
            session.append(AgentRunFinished(result=terminal("incomplete", reason)))
            raise
        except Exception as exc:
            session.append(AgentRunFinished(result=terminal("failed", type(exc).__name__)))
            raise
        session.append(AgentRunFinished(result=result, purpose=purpose))
        return result
    finally:
        current_agent_run.reset(token)
