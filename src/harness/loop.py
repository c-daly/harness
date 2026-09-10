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
from harness.errors import ToolCallLimitExceeded
from harness.fallback import FallbackPolicy, TaskFallback
from harness.events import (
    CustomEvent,
    ContextPolicyConfigured,
    ContextPrepared,
    FallbackConfigured,
    ErrorRaised,
    ModelCorrectionRequested,
    ModelSelected,
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
        fallback_policy: FallbackPolicy | None = None,
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
        self.fallback_policy = FallbackPolicy.model_validate(fallback_policy.model_dump()) if fallback_policy else None
        self.active_model: ModelId | None = None
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
        self.dispatcher.scope.budget.usage.attach(self.session)
        from harness.execution_controls import record_execution_limits
        record_execution_limits(self.dispatcher)
        self.record_model_selection()
        if self.dispatcher.scope.context_policy is not None:
            self.session.append(ContextPolicyConfigured(policy=self.dispatcher.scope.context_policy))
        if self.fallback_policy is not None:
            self.session.append(FallbackConfigured(policy=self.fallback_policy))
        await self._apply_contributions(
            LifecyclePoint.SESSION_START, {"session_id": self.session.id}
        )

    def record_model_selection(self) -> None:
        from harness.provider_litellm import CatalogProvider

        if isinstance(self.provider, CatalogProvider):
            self.session.append(ModelSelected(model=self.model, pinned=self.model_pinned))

    async def run_turn(self, user_text: str) -> str:
        result = await self.run_task(AgentTask(prompt=user_text, context=tuple(self.turn_context)))
        return result.read_text(self.session.blobs)

    async def run_task(
        self, task: AgentTask, *, on_progress: Callable[[AgentProgress], None] | None = None,
    ) -> AgentResult:
        """Execute one bounded task; completion leaves acceptance unverified."""
        from harness.agent import bound_task
        task = bound_task(task, self.dispatcher.scope.budget.limits)
        policy = self.dispatcher.scope.context_policy
        if policy is not None:
            limits = {"max_input_bytes": min(task.limits.max_input_bytes, policy.max_input_bytes)}
            if policy.response is not None:
                if policy.response.max_output_tokens is not None:
                    limits["max_output_tokens"] = min(task.limits.max_output_tokens, policy.response.max_output_tokens)
                if policy.response.max_output_bytes is not None:
                    limits["max_response_bytes"] = min(task.limits.max_response_bytes, policy.response.max_output_bytes)
            task = task.model_copy(update={"limits": task.limits.model_copy(update=limits)})
        if self._task_active:
            raise RuntimeError("an agent task is already running")
        self._task_active = True
        self.active_model = self.model
        try:
            from harness.handoff import capture_scope
            return await execute_task(
                self.session, task, runtime="harness", model=self.model,
                capabilities={"handoff_scope": capture_scope(self.dispatcher)},
                execute=lambda: self._run_task_body(task, on_progress),
            )
        finally:
            self._task_active = False
            self.active_model = None
            self.turn_context = []

    async def _run_task_body(self, task: AgentTask, on_progress) -> AgentOutput:
        user_text = task.prompt
        history_start = len(self.history) if task.handoff_id else 0
        self.session.append(UserMessage(text=user_text))
        self.history.append(Message.user_text(user_text))
        max_iterations = min(self.max_iterations, task.limits.max_iterations)
        usage = Usage(0, 0, 0, 0)
        corrections = 0
        active_run = current_agent_run.get()
        fallback = TaskFallback(self)

        def progress(phase, iteration, chunk=None):
            if on_progress is not None:
                try:
                    on_progress(AgentProgress(task.id, active_run.run_id, phase, iteration, chunk))
                except Exception:
                    pass  # observers do not control execution

        try:
            from harness.resident import fetch_context
            resident_context = await fetch_context(self.dispatcher) if max_iterations else ()
            for iteration in range(1, max_iterations + 1):
                prefix = [
                    Message.system_text(self.system_prompt),
                    *task.context,
                    *resident_context,
                    *([Message.system_text("Task acceptance criteria:\n" +
                                           "\n".join(f"- {c}" for c in task.acceptance_criteria))]
                      if task.acceptance_criteria else []),
                ]
                policy = self.dispatcher.scope.context_policy
                if policy is not None:
                    if policy.response is not None and policy.response.instructions is not None:
                        prefix.append(Message.system_text("Response instructions:\n" + policy.response.instructions))
                    from harness.context import prepare_context
                    prepared = prepare_context(prefix, self.history[history_start:], self.registry.specs(), policy,
                                               max_input_bytes=task.limits.max_input_bytes,
                                               blobs=self.session.blobs)
                    messages = prepared.messages
                    self.session.append(ContextPrepared(
                        task_id=task.id, policy_digest=policy.digest,
                        retained_turns=prepared.retained_turns, omitted_turns=prepared.omitted_turns,
                        omitted_messages=prepared.omitted_messages, input_bytes=prepared.input_bytes,
                        max_input_bytes=task.limits.max_input_bytes,
                        tools=tuple(str(t.name) for t in self.registry.specs()),
                    ))
                else:
                    messages = [*prefix, *self.history[history_start:]]
                runtime = bind_agent_runtime(
                    self.provider, fallback.model, self.dispatcher, prepared_messages=tuple(messages),
                    pricing=self.pricing, pricing_for=self.pricing_for, pinned=fallback.pinned,
                    on_chunk=self.on_chunk,
                )
                if runtime is not None:
                    from harness.errors import ProviderError
                    try:
                        child = await runtime.run_task(AgentTask(
                            prompt=task.prompt, agent=task.agent, acceptance_criteria=task.acceptance_criteria,
                            limits=task.limits,
                        ), on_progress=on_progress)
                    except ProviderError as exc:
                        fallback.hold_external(exc)
                        raise
                    if child.response is not None:
                        self.history.append(child.response)
                    return AgentOutput(child.read_text(self.session.blobs), child.status,
                                       child.reason, child.usage)
                progress("inference", iteration)

                def chunk_received(chunk):
                    progress("stream", iteration, chunk)
                    if self.on_chunk is not None:
                        self.on_chunk(chunk)

                try:
                    failures = fallback.failed_calls
                    response = await fallback.dispatch(
                        InferenceRequest(
                            model=fallback.model, messages=tuple(messages), tools=self.registry.specs(),
                            purpose="conversation", tool_choice="auto",
                            max_input_bytes=task.limits.max_input_bytes,
                            max_output_bytes=task.limits.max_response_bytes,
                            max_output_tokens=task.limits.max_output_tokens,
                            max_stream_chunks=task.limits.max_stream_chunks,
                            timeout_seconds=task.limits.timeout_seconds,
                        ),
                        before_work=iteration == 1,
                        on_chunk=chunk_received,
                        on_switch=lambda: progress("fallback", iteration),
                    )
                    if fallback.failed_calls != failures:
                        usage = add_usage(usage, Usage())
                except ToolCallLimitExceeded as exc:
                    if (policy is None or corrections >= policy.tool_recovery_attempts
                            or iteration >= max_iterations or exc.call_id is None):
                        raise
                    corrections += 1
                    correction = ModelCorrectionRequested(
                        failed_call_id=CallId(exc.call_id), task_id=task.id,
                        agent_run_id=active_run.run_id, attempt=corrections,
                    )
                    self.session.append(correction)
                    self.history.append(Message.system_text(correction.instruction))
                    progress("correction", iteration)
                    # The failed stream has no accepted usage result. Retain
                    # unknown accounting rather than implying that correction
                    # was free; its model reservation is never refunded.
                    usage = add_usage(usage, Usage())
                    continue
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
