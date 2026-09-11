"""Bind task execution separately from inference, retaining dispatch authority.

The first migration is Codex. Its completion stream is a transport compatibility
layer, recorded once for accounting; the typed task result owns the conversation.
Descriptors declare adapter support, not installed-CLI qualification or authority.
"""

from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from harness.agent import AgentOutput, AgentProgress, AgentResult, AgentTask, current_agent_run, execute_task
from harness.dispatcher import Dispatcher
from harness.events import UserMessage
from harness.inference import InferenceRequest
from harness.messages import Message
from harness.provider import Chunk, Usage
from harness.types import ModelId


class AgentRuntimeInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    runtime: str
    task_api_version: Literal[1] = 1
    qualification: Literal["unverified"] = "unverified"
    resume: Literal[False] = False
    harness_tools: Literal["dispatcher"] = "dispatcher"
    native_tools: Literal["provider-controlled"] = "provider-controlled"
    internal_iteration_limit: Literal[False] = False
    output_token_limit: Literal["reported-usage-check"] = "reported-usage-check"
    response_limits: Literal["adapter-chunks"] = "adapter-chunks"


def bind_agent_runtime(
    provider, model: ModelId, dispatcher: Dispatcher, *,
    prepared_messages: tuple[Message, ...] | None = None,
    pricing: dict[str, float] | None = None,
    pricing_for: Callable[[ModelId], dict[str, float]] | None = None,
    pinned: bool = False, on_chunk: Callable[[Chunk], None] | None = None,
) -> "ExternalAgentRuntime | None":
    """Return None for adapters not yet migrated; never infer runtime from a name.

    prepared_messages is the internal conversation bridge: AgentLoop has already
    assembled context and recorded the user's message. It grants no capabilities.
    Standalone callers pass task context and prompt to run_task instead.
    """
    describe = getattr(provider, "agent_runtime_info", None)
    info = describe(model) if describe is not None else None
    if info is None:
        return None
    return ExternalAgentRuntime(provider, model, dispatcher, info, prepared_messages=prepared_messages,
                                pricing=pricing, pricing_for=pricing_for, pinned=pinned, on_chunk=on_chunk)


class ExternalAgentRuntime:
    def __init__(self, provider, model, dispatcher, info, *, prepared_messages=None,
                 pricing=None, pricing_for=None, pinned=False, on_chunk=None):
        self.provider = provider
        self.model = model
        self.dispatcher = dispatcher
        self.info = AgentRuntimeInfo.model_validate(info.model_dump())
        self._prepared_messages = prepared_messages
        self.pricing, self.pricing_for = pricing, pricing_for
        self.pinned, self.on_chunk = pinned, on_chunk
        self._active = False

    async def run_task(
        self, task: AgentTask, *, on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentResult:
        from harness.agent import bound_task
        task = bound_task(task, self.dispatcher.scope.budget.limits)
        policy = self.dispatcher.scope.context_policy
        if policy is not None:
            task = task.model_copy(update={"limits": task.limits.model_copy(update={
                "max_input_bytes": min(task.limits.max_input_bytes, policy.max_input_bytes)})})
        if self._active:
            raise RuntimeError("an agent task is already running")
        self._active = True
        try:
            from harness.handoff import capture_scope
            return await execute_task(
                self.dispatcher.session, task, runtime=self.info.runtime, model=self.model,
                activity=self.dispatcher.scope.budget.activity,
                run_budgets=self.dispatcher.scope.budget.runs,
                extension_blocked="external runtime timers cannot be extended",
                capabilities={**self.info.model_dump(), "handoff_scope": capture_scope(self.dispatcher)},
                purpose="conversation",
                execute=lambda: self._execute(task, on_progress),
            )
        finally:
            self._active = False

    async def _execute(self, task, on_progress):
        if self._prepared_messages is None:
            self.dispatcher.session.append(UserMessage(text=task.prompt))
            messages = (*task.context,
                        *([Message.system_text("Task acceptance criteria:\n" +
                                               "\n".join(f"- {c}" for c in task.acceptance_criteria))]
                          if task.acceptance_criteria else []), Message.user_text(task.prompt))
        else:
            messages = self._prepared_messages
        if task.limits.max_iterations == 0:
            return AgentOutput("[stopped: max iterations (0) reached]", "incomplete", "iteration_limit",
                               Usage(0, 0, 0, 0))
        if self._prepared_messages is None:
            from harness.resident import fetch_context
            messages = (*await fetch_context(self.dispatcher), *messages)
        active = current_agent_run.get()

        def progress(phase, chunk=None):
            from harness.activity import current_activity
            observation = current_activity.get()
            if observation is not None and phase != "stream":
                observation.update(phase=phase)
            if on_progress is not None:
                try:
                    on_progress(AgentProgress(task.id, active.run_id, phase, 1, chunk))
                except Exception:
                    pass  # observers cannot change execution

        def observe(chunk):
            progress("stream", chunk)
            if self.on_chunk is not None:
                self.on_chunk(chunk)

        progress("execution")
        response = await self.dispatcher.dispatch_agent_response(
            provider=self.provider, runtime=self.info.runtime,
            request=InferenceRequest(
                model=self.model, messages=messages, tools=tuple(self.dispatcher.registry.specs()),
                purpose="agent-task", tool_choice="auto",
                max_input_bytes=task.limits.max_input_bytes,
                max_output_bytes=task.limits.max_response_bytes,
                max_output_tokens=task.limits.max_output_tokens,
                max_stream_chunks=task.limits.max_stream_chunks,
                timeout_seconds=task.limits.timeout_seconds,
            ),
            pricing=self.pricing, pricing_for=self.pricing_for, pinned=self.pinned, on_chunk=observe,
        )
        complete = response.stop_reason == "end_turn"
        return AgentOutput(response.message.text(), "completed" if complete else "incomplete",
                           "" if complete else response.stop_reason, response.usage, response.message)
