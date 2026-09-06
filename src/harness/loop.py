# src/harness/loop.py
"""The agent loop: build context, dispatch model, dispatch tools concurrently, repeat."""

import asyncio
from typing import Callable

from harness.agent import (
    AgentOutput, AgentProgress, AgentResult, AgentTask, add_usage, current_agent_run, execute_task,
)
from harness.agent_runtime import bind_agent_runtime
from harness.dispatcher import Dispatcher, ToolOutcome
from harness.execution import ExecutionScope
from harness.events import (
    CustomEvent,
    ErrorRaised,
    SessionEnded,
    ToolCallCancelled,
    UserInterrupt,
    UserMessage,
)
from harness.hooks import Annotate, Emit, HookBus, Inject, LifecyclePoint, ProposedToolCall
from harness.interaction import Resolver
from harness.inference import InferenceRequest
from harness.messages import Message, Role, ToolResultBlock
from harness.provider import Chunk, ModelProvider, Usage
from harness.redaction import StringRedactor, identity_redact
from harness.session import Session
from harness.tools import FilteredRegistry, ToolRegistry
from harness.types import CallId, ModelId


class AgentLoop:
    def __init__(
        self,
        *,
        session: Session,
        provider: ModelProvider,
        registry: ToolRegistry | FilteredRegistry,
        hooks: HookBus,
        resolver: Resolver,
        model: ModelId,
        system_prompt: str,
        max_iterations: int = 20,
        history: list[Message] | None = None,
        pricing: dict[str, float] | None = None,
        pricing_for: Callable[[ModelId], dict[str, float]] | None = None,
        pinned: bool = False,
        redact: StringRedactor = identity_redact,
        scope: ExecutionScope | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.registry = registry
        self.hooks = hooks
        self.model = model
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.history: list[Message] = list(history) if history else []
        # Per-turn, non-persisted context (e.g. @-mention file contents from the
        # TUI): consumed at model-call assembly in run_turn and cleared once the
        # turn ends, win or lose -- never written to history or the session log,
        # so the persisted user message always stays the literal text the user
        # typed (task-6 contract: mentions expand for the model, not the log).
        self.turn_context: list[Message] = []
        self.pricing = pricing
        self.pricing_for = pricing_for
        self.model_pinned = pinned
        self.dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=resolver, redact=redact,
            scope=scope,
        )
        self.on_chunk: Callable[[Chunk], None] | None = None
        self._ended = False
        self._task_active = False
        self._turn_outcomes: dict[CallId, ToolOutcome] = {}

    def set_turn_context(self, messages: list[Message]) -> None:
        """Install extra context for the NEXT run_turn call. Read at the start of
        every dispatch_model within that turn, then cleared -- a later turn (or a
        turn with nothing to inject) never sees a stale carry-over."""
        self.turn_context = list(messages)

    async def _apply_contributions(self, point: LifecyclePoint, ctx: dict) -> None:
        contributions, warnings = await self.hooks.run_lifecycle(point, ctx)
        for c in contributions:
            match c:
                case Inject(text=text):
                    if point is LifecyclePoint.SESSION_START:
                        self.system_prompt = f"{self.system_prompt}\n\n{text}"
                    else:
                        self.session.append(
                            ErrorRaised(
                                where=f"lifecycle:{point}",
                                message="Inject ignored outside session_start",
                            )
                        )
                case Emit(namespace=ns, name=name, data=data):
                    self.session.append(CustomEvent(namespace=ns, name=name, data=dict(data)))
                case Annotate(note=note):
                    self.session.append(
                        CustomEvent(namespace="annotation", name="note", data={"text": note})
                    )
        for warning in warnings:
            self.session.append(ErrorRaised(where=f"lifecycle:{point}", message=warning))

    async def start(self) -> None:
        self.session.start()
        await self._apply_contributions(
            LifecyclePoint.SESSION_START, {"session_id": self.session.id}
        )

    async def run_turn(self, user_text: str) -> str:
        result = await self.run_task(AgentTask(prompt=user_text, context=tuple(self.turn_context)))
        return result.read_text(self.session.blobs)

    async def run_task(
        self, task: AgentTask, *, on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentResult:
        """Execute one bounded task; completion leaves acceptance unverified."""
        task = AgentTask.model_validate(task.model_dump())
        if self._task_active:
            raise RuntimeError("an agent task is already running")
        self._task_active = True
        try:
            return await execute_task(
                self.session, task, runtime="harness", model=self.model,
                execute=lambda: self._run_task_body(task, on_progress),
            )
        finally:
            self._task_active = False
            self.turn_context = []

    async def _run_task_body(self, task: AgentTask, on_progress) -> AgentOutput:
        user_text = task.prompt
        self.session.append(UserMessage(text=user_text))
        self.history.append(Message.user_text(user_text))
        max_iterations = min(self.max_iterations, task.limits.max_iterations)
        usage = Usage(0, 0, 0, 0)
        active_run = current_agent_run.get()

        def progress(phase, iteration, chunk=None):
            if on_progress is not None:
                try:
                    on_progress(AgentProgress(task.id, active_run.run_id, phase, iteration, chunk))
                except Exception:
                    pass  # observers do not control execution

        try:
            for iteration in range(1, max_iterations + 1):
                messages = [
                    Message.system_text(self.system_prompt),
                    *task.context,
                    *([Message.system_text("Task acceptance criteria:\n" +
                                           "\n".join(f"- {c}" for c in task.acceptance_criteria))]
                      if task.acceptance_criteria else []),
                    *self.history,
                ]
                runtime = bind_agent_runtime(
                    self.provider, self.model, self.dispatcher, prepared_messages=tuple(messages),
                    pricing=self.pricing, pricing_for=self.pricing_for, pinned=self.model_pinned,
                    on_chunk=self.on_chunk,
                )
                if runtime is not None:
                    child = await runtime.run_task(AgentTask(
                        prompt=task.prompt, agent=task.agent, acceptance_criteria=task.acceptance_criteria,
                        limits=task.limits,
                    ), on_progress=on_progress)
                    if child.response is not None:
                        self.history.append(child.response)
                    return AgentOutput(child.read_text(self.session.blobs), child.status,
                                       child.reason, child.usage)
                progress("inference", iteration)

                def chunk_received(chunk):
                    progress("stream", iteration, chunk)
                    if self.on_chunk is not None:
                        self.on_chunk(chunk)

                response = await self.dispatcher.dispatch_response(
                    provider=self.provider,
                    request=InferenceRequest(
                        model=self.model, messages=tuple(messages), tools=self.registry.specs(),
                        purpose="conversation", tool_choice="auto",
                        max_input_bytes=task.limits.max_input_bytes,
                        max_output_bytes=task.limits.max_response_bytes,
                        max_output_tokens=task.limits.max_output_tokens,
                        max_stream_chunks=task.limits.max_stream_chunks,
                        timeout_seconds=min(120, task.limits.timeout_seconds),
                    ),
                    pricing=self.pricing,
                    pricing_for=self.pricing_for,
                    pinned=self.model_pinned,
                    on_chunk=chunk_received,
                )
                assistant = response.message
                usage = add_usage(usage, response.usage)
                self.history.append(assistant)
                if response.stop_reason not in ("end_turn", "tool_use"):
                    self.repair_turn()
                    return AgentOutput(assistant.text(), "incomplete", response.stop_reason, usage)
                calls = assistant.tool_calls()
                if not calls:
                    if response.stop_reason == "tool_use":
                        return AgentOutput(assistant.text(), "incomplete", "missing_tool_call", usage)
                    return AgentOutput(assistant.text(), usage=usage)
                self._turn_outcomes.clear()
                progress("tools", iteration)

                async def _run_one(call):
                    outcome = await self.dispatcher.dispatch_tool(
                        ProposedToolCall(call_id=call.call_id, tool=call.tool, args=call.args)
                    )
                    self._turn_outcomes[call.call_id] = outcome
                    return outcome

                tasks = [asyncio.create_task(_run_one(c)) for c in calls]
                try:
                    try:
                        outcomes = await asyncio.gather(*tasks)
                    except BaseException:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                except Exception as exc:
                    try:
                        self.session.append(
                            ErrorRaised(
                                where="loop:tool_dispatch",
                                message=f"{type(exc).__name__}: {exc}",
                            )
                        )
                    except Exception:
                        pass
                    raise
                for call, outcome in zip(calls, outcomes):
                    self.history.append(
                        Message.tool_result(
                            call.call_id,
                            text=outcome.text,
                            blob=outcome.blob,
                            is_error=outcome.is_error,
                        )
                    )
            self.session.append(
                ErrorRaised(where="loop", message=f"max iterations ({max_iterations}) reached")
            )
            return AgentOutput(f"[stopped: max iterations ({max_iterations}) reached]",
                               "incomplete", "iteration_limit", usage)
        except BaseException:
            self.repair_turn()
            raise
        finally:
            self.turn_context = []

    def repair_turn(self) -> int:
        repaired = 0
        for call in self._dangling_tool_calls():
            outcome = self._turn_outcomes.get(call.call_id) or self.dispatcher.terminal_tools.get(call.call_id)
            if outcome is not None:
                self.history.append(
                    Message.tool_result(
                        call.call_id,
                        text=outcome.text,
                        blob=outcome.blob,
                        is_error=outcome.is_error,
                    )
                )
            else:
                self.session.append(ToolCallCancelled(call_id=call.call_id))
                self.history.append(
                    Message.tool_result(
                        call.call_id,
                        text="(call did not complete)",
                        is_error=True,
                    )
                )
            repaired += 1
        return repaired

    def interrupt_turn(self) -> None:
        self.repair_turn()
        self.session.append(UserInterrupt())

    def _dangling_tool_calls(self):
        paired_ids: set[str] = set()
        for msg in reversed(self.history):
            if msg.role == Role.TOOL:
                for block in msg.blocks:
                    if isinstance(block, ToolResultBlock):
                        paired_ids.add(block.call_id)
            elif msg.role == Role.ASSISTANT:
                calls = msg.tool_calls()
                if not calls:
                    return []
                return [c for c in calls if c.call_id not in paired_ids]
        return []

    async def end(self) -> None:
        if self._ended:
            raise RuntimeError("AgentLoop.end() already called")
        self._ended = True
        await self._apply_contributions(LifecyclePoint.SESSION_END, {"session_id": self.session.id})
        self.session.append(SessionEnded())
