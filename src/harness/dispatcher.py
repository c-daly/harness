# src/harness/dispatcher.py
"""The single enforcement component. Nothing executes except through here."""

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable

from harness.blobs import INLINE_THRESHOLD, BlobRef, BlobStore
from harness.agent import current_agent_run
from harness.errors import ProviderError
from harness.events import (
    DispatchResolved,
    HookDecided,
    ModelCallCancelled,
    ModelCallCompleted,
    ModelCallFailed,
    ModelCallProposed,
    ModelCallStarted,
    PermissionRequested,
    PermissionResolved,
    RetryAttempted,
    ToolCallCompleted,
    ToolCallCancelled,
    ToolCallProposed,
)
from harness.hooks import (
    HookBus,
    ProposedModelCall,
    ProposedToolCall,
    decision_to_payload,
)
from harness.execution import BudgetExceeded, ExecutionScope, current_scope
from harness.interaction import PermissionRequest, Resolver
from harness.inference import (
    InferenceRequest, InferenceResult, LegacyCompletionAdapter, check_input, collect_bounded, infer,
)
from harness.messages import Message, materialize_tool_results
from harness.provider import Chunk, ModelProvider, Usage
from harness.callctx import reset_current_call_id, set_current_call_id
from harness.redaction import StringRedactor, identity_redact
from harness.session import Session
from harness.tools import FilteredRegistry, ToolRegistry, ToolSpec, validate_arguments
from harness.types import ModelId, new_call_id

# errors inline (never blob-spilled) so they stay readable; cap keeps log lines bounded
_ERROR_TEXT_CAP = 4096

# Set for the duration of a provider call so a claude-code-backed subagent turn
# events its tool calls into the SUBAGENT session, not the parent dispatcher’s.
current_dispatch_tool: ContextVar = ContextVar("current_dispatch_tool", default=None)


class ModelDispatchBlocked(Exception):
    pass


@dataclass(frozen=True)
class ToolOutcome:
    text: str | None
    blob: BlobRef | None
    is_error: bool
    _blobs: BlobStore | None = field(default=None, repr=False, compare=False)

    def read_text(self) -> str:
        if self.blob is not None:
            if self._blobs is None:
                raise RuntimeError("tool result requires its session blob store")
            return self._blobs.get(self.blob).decode("utf-8")
        return self.text if self.text is not None else ""


class Dispatcher:
    def __init__(
        self,
        *,
        session: Session,
        registry: ToolRegistry | FilteredRegistry,
        hooks: HookBus,
        resolver: Resolver,
        retry_delays: tuple[float, ...] = (0.5, 2.0, 8.0),
        redact: StringRedactor = identity_redact,
        scope: ExecutionScope | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.hooks = hooks
        self.resolver = resolver
        self.retry_delays = retry_delays
        self._redact = redact
        self.scope = scope or ExecutionScope(session, registry)
        self.terminal_tools: dict = {}

    async def _run_chain(self, action) -> tuple[object | None, str | None]:
        """Run hooks + Ask resolution. Returns (effective_action, denial_reason)."""
        outcome = await self.hooks.run_dispatch(action)
        for name, decision in outcome.decisions:
            self.session.append(
                HookDecided(
                    call_id=action.call_id, hook=name, decision=decision_to_payload(decision)
                )
            )
        if outcome.blocked is not None:
            return None, f"blocked by policy: {outcome.blocked.reason}"
        if outcome.ask is not None:
            self.session.append(
                PermissionRequested(call_id=action.call_id, reason=outcome.ask.reason)
            )
            try:
                allowed = await self.resolver.resolve(
                    PermissionRequest(
                        call_id=action.call_id, action=outcome.effective, reason=outcome.ask.reason
                    )
                )
            except asyncio.CancelledError:
                self.session.append(
                    PermissionResolved(
                        call_id=action.call_id, allowed=False, resolver=self.resolver.name
                    )
                )
                raise
            except Exception as exc:
                self.session.append(
                    PermissionResolved(
                        call_id=action.call_id, allowed=False, resolver=self.resolver.name
                    )
                )
                return None, f"permission channel error: {exc} (denied)"
            self.session.append(
                PermissionResolved(
                    call_id=action.call_id, allowed=allowed, resolver=self.resolver.name
                )
            )
            if not allowed:
                return None, "denied by user"
        return outcome.effective, None

    async def dispatch_tool(self, call: ProposedToolCall) -> ToolOutcome:
        active_run = current_agent_run.get()
        lineage = {"task_id": active_run.task.id, "agent_run_id": active_run.run_id,
                   "purpose": "conversation" if active_run.runtime == "harness" else "agent-task"} if active_run else {}
        self.session.append(
            ToolCallProposed(call_id=call.call_id, tool=call.tool, args=dict(call.args), **lineage)
        )
        scope_token = current_scope.set(self.scope)
        try:
            try:
                self.scope.budget.reserve_call("tool")
            except BudgetExceeded as exc:
                result = ToolOutcome(text=str(exc), blob=None, is_error=True)
                self.session.append(ToolCallCompleted(
                    call_id=call.call_id, result_text=result.text, is_error=True,
                ))
            else:
                result = await self._dispatch_tool_body(call)
        except asyncio.CancelledError:
            text = "(call cancelled; side effects may have occurred)"
            self.session.append(ToolCallCancelled(call_id=call.call_id, result_text=text))
            self.terminal_tools[call.call_id] = ToolOutcome(
                text=text, blob=None, is_error=True,
            )
            raise
        else:
            self.terminal_tools[call.call_id] = result
            return result
        finally:
            current_scope.reset(scope_token)

    async def _dispatch_tool_body(self, call: ProposedToolCall) -> ToolOutcome:
        effective, denial = await self._run_chain(call)
        if denial is not None:
            self.session.append(
                ToolCallCompleted(call_id=call.call_id, result_text=denial, is_error=True)
            )
            return ToolOutcome(text=denial, blob=None, is_error=True)
        if not isinstance(effective, ProposedToolCall):
            # a hook rewrote tool -> model; fail closed rather than crash
            denial = "blocked by policy: rewrite changed action type \u2014 refused"
            self.session.append(
                ToolCallCompleted(call_id=call.call_id, result_text=denial, is_error=True)
            )
            return ToolOutcome(text=denial, blob=None, is_error=True)
        self.session.append(
            DispatchResolved(
                call_id=call.call_id, kind="tool", tool=effective.tool, args=dict(effective.args)
            )
        )
        started = time.monotonic()
        try:
            token = set_current_call_id(call.call_id)
            try:
                tool = self.registry.get(effective.tool)
                validate_arguments(tool.spec, dict(effective.args))
                raw = await tool(dict(effective.args))
            finally:
                reset_current_call_id(token)
            is_error = False
        except Exception as exc:
            raw, is_error = self._redact(f"tool error: {exc}"), True
            if len(raw) > _ERROR_TEXT_CAP:
                raw = raw[:_ERROR_TEXT_CAP] + " \u2026[truncated]"
        duration_ms = int((time.monotonic() - started) * 1000)
        if not is_error:
            raw = self._redact(raw)  # redact BEFORE the spill decision (L8)
        text: str | None = raw
        blob: BlobRef | None = None
        if not is_error and len(raw.encode()) > INLINE_THRESHOLD:
            blob, text = self.session.blobs.put(raw.encode()), None
        self.session.append(
            ToolCallCompleted(
                call_id=call.call_id,
                result_text=text,
                result_blob=blob,
                is_error=is_error,
                duration_ms=duration_ms,
            )
        )
        return ToolOutcome(text=text, blob=blob, is_error=is_error, _blobs=self.session.blobs)

    async def dispatch_model(
        self,
        *,
        provider: ModelProvider,
        model: ModelId,
        messages: list[Message],
        tools: tuple[ToolSpec, ...],
        pricing: dict[str, float] | None = None,
        pricing_for: Callable[[ModelId], dict[str, float]] | None = None,
        pinned: bool = False,
        on_chunk: Callable[[Chunk], None] | None = None,
        purpose: str = "conversation",
    ) -> tuple[Message, Usage]:
        """Compatibility entry for chat/native loops and legacy external agents.

        New internal callers use dispatch_inference. Agent runs are never
        retried here: a failed external process may have completed side effects.
        """
        result = await self.dispatch_response(
            provider=provider,
            request=InferenceRequest(model=model, messages=tuple(messages), tools=tools,
                                     purpose=purpose,
                                     tool_choice="auto" if purpose == "conversation" else "none"),
            pricing=pricing, pricing_for=pricing_for,
            pinned=pinned, on_chunk=on_chunk,
        )
        return result.message, result.usage

    async def dispatch_response(
        self, *, provider, request: InferenceRequest,
        pricing: dict[str, float] | None = None,
        pricing_for: Callable[[ModelId], dict[str, float]] | None = None,
        pinned: bool = False, on_chunk: Callable[[Chunk], None] | None = None,
    ) -> InferenceResult:
        """Conversation migration path preserving stop reasons for task control.

        External completion remains a legacy agent bridge. Internal inference
        uses dispatch_inference and cannot cross that runtime boundary.
        """
        return await self._dispatch_generation(
            provider=provider, request=request, allow_agent=request.purpose == "conversation",
            pricing=pricing, pricing_for=pricing_for, pinned=pinned, on_chunk=on_chunk,
        )

    async def dispatch_inference(
        self, *, provider, request: InferenceRequest,
        pricing: dict[str, float] | None = None,
        pricing_for: Callable[[ModelId], dict[str, float]] | None = None,
        pinned: bool = False, on_chunk: Callable[[Chunk], None] | None = None,
    ) -> InferenceResult:
        """Bounded, audited inference; routing cannot replace it with an agent."""
        return await self._dispatch_generation(
            provider=provider, request=request, allow_agent=False,
            pricing=pricing, pricing_for=pricing_for, pinned=pinned, on_chunk=on_chunk,
        )

    async def dispatch_agent_response(
        self, *, provider, request: InferenceRequest, runtime: str,
        pricing=None, pricing_for=None, pinned=False, on_chunk=None,
    ) -> InferenceResult:
        """Compatibility transport inside a bound task; hooks cannot change runtime."""
        if request.purpose != "agent-task":
            raise ValueError("agent transport requires agent-task purpose")
        return await self._dispatch_generation(
            provider=provider, request=request, allow_agent=True, required_runtime=runtime,
            pricing=pricing, pricing_for=pricing_for, pinned=pinned, on_chunk=on_chunk,
        )

    async def _dispatch_generation(
        self, *, provider, request: InferenceRequest, allow_agent: bool,
        pricing, pricing_for, pinned, on_chunk, required_runtime=None,
    ) -> InferenceResult:
        request = InferenceRequest.model_validate(request.model_dump())
        model, messages, tools, purpose = request.model, request.messages, request.tools, request.purpose

        def kind_for(alias):
            kind = getattr(provider, "execution_kind", None)
            if kind is not None:
                return kind(alias) if callable(kind) else kind
            return "inference" if hasattr(provider, "infer") else "legacy"

        def observed(chunk):
            if on_chunk is not None:
                try:
                    on_chunk(chunk)
                except Exception:
                    pass  # frontend failure must not break dispatch

        call = ProposedModelCall(call_id=new_call_id(), model=model, pinned=pinned)
        active_run = current_agent_run.get()
        lineage = {"task_id": active_run.task.id, "agent_run_id": active_run.run_id} if active_run else {}
        self.session.append(
            ModelCallProposed(call_id=call.call_id, model=model, purpose=purpose, **lineage)
        )
        effective_model = model
        started: int | None = None

        def elapsed() -> int:
            return (time.monotonic_ns() - started) // 1_000_000 if started is not None else 0

        try:
            effective, denial = await self._run_chain(call)
            if denial is not None:
                raise ModelDispatchBlocked(denial)
            if not isinstance(effective, ProposedModelCall):
                raise ModelDispatchBlocked("rewrite changed action type — refused")
            effective_model = effective.model
            execution_kind = kind_for(effective_model)
            if required_runtime is not None:
                describe = getattr(provider, "agent_runtime_info", None)
                info = describe(effective_model) if describe is not None else None
                if execution_kind != "agent" or info is None or info.runtime != required_runtime:
                    raise ProviderError("routing changed the bound agent runtime; select a new runtime explicitly")
            if execution_kind == "agent" and not allow_agent:
                raise ProviderError(
                    f"{effective_model!r} is an agent runtime; select an inference alias for {purpose}"
                )
            self.session.append(
                DispatchResolved(call_id=call.call_id, kind="model", model=effective_model)
            )
            self.session.append(ModelCallStarted(call_id=call.call_id, model=effective_model,
                                                 execution_kind=execution_kind))
            started = time.monotonic_ns()
            check_input(request)
            resolved_messages = materialize_tool_results(
                list(messages), self.session.blobs, max_bytes=request.max_input_bytes,
            )
            request = InferenceRequest.model_validate({
                **request.model_dump(), "model": effective_model, "messages": resolved_messages,
            })
            check_input(request)
            deadline = time.monotonic() + request.timeout_seconds
            attempt = 0
            token = current_dispatch_tool.set(self.dispatch_tool)
            scope_token = current_scope.set(self.scope)
            try:
                while True:
                    try:
                        self.scope.budget.reserve_call("model")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("generation deadline exceeded")
                        attempt_request = request.model_copy(update={"timeout_seconds": remaining})
                        if execution_kind == "agent":
                            source = provider.complete(
                                model=effective_model, messages=resolved_messages, tools=tools,
                            )
                            message, usage, stop_reason = await collect_bounded(
                                source, attempt_request, on_chunk=observed,
                            )
                            if required_runtime is not None and message.tool_calls():
                                raise ProviderError("agent runtime returned unexecuted tool proposals")
                            result = InferenceResult(message, usage, stop_reason)
                        else:
                            bounded_provider = provider if hasattr(provider, "infer") else LegacyCompletionAdapter(provider)
                            result = await infer(
                                bounded_provider, attempt_request,
                                on_chunk=observed,
                            )
                            message, usage, stop_reason = result.message, result.usage, result.stop_reason
                        break
                    except ProviderError as exc:
                        if execution_kind == "agent" or not exc.retryable or attempt >= len(self.retry_delays):
                            raise
                        delay = self.retry_delays[attempt]
                        attempt += 1
                        self.session.append(
                            RetryAttempted(
                                call_id=call.call_id, attempt=attempt,
                                reason=type(exc).__name__,
                            )
                        )
                        remaining = deadline - time.monotonic()
                        if delay >= remaining:
                            raise TimeoutError("inference deadline exceeded during retry") from exc
                        await asyncio.sleep(delay)
            finally:
                current_dispatch_tool.reset(token)
                current_scope.reset(scope_token)
            stamped_pricing = (
                pricing_for(effective_model) if pricing_for is not None else (pricing or {})
            )
        except asyncio.CancelledError:
            self.session.append(ModelCallCancelled(call_id=call.call_id, duration_ms=elapsed()))
            raise
        except Exception as exc:
            error_type = "policy_denied" if isinstance(exc, ModelDispatchBlocked) else type(exc).__name__
            self.session.append(
                ModelCallFailed(
                    call_id=call.call_id, model=effective_model, error_type=error_type,
                    # Provider exception strings may contain credentials or response bodies.
                    message=f"Model call failed ({error_type}).",
                    retryable=isinstance(exc, ProviderError) and exc.retryable,
                    duration_ms=elapsed(),
                )
            )
            raise
        # Outside the exception scope: a failing log write cannot emit a second terminal fact.
        self.session.append(
            ModelCallCompleted(
                call_id=call.call_id,
                model=effective_model,
                message=message.model_dump(),
                usage=usage.as_dict(),
                stop_reason=stop_reason,
                pricing=stamped_pricing,
                duration_ms=elapsed(),
                purpose=purpose,
                execution_kind=execution_kind,
                **lineage,
            )
        )
        return result
