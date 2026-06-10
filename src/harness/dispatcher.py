# src/harness/dispatcher.py
"""The single enforcement component. Nothing executes except through here."""

import time
from dataclasses import dataclass

from harness.blobs import INLINE_THRESHOLD, BlobRef
from harness.events import (
    DispatchResolved,
    HookDecided,
    ModelCallCompleted,
    ModelCallProposed,
    ModelCallStarted,
    PermissionRequested,
    PermissionResolved,
    ToolCallCompleted,
    ToolCallProposed,
)
from harness.hooks import (
    HookBus,
    ProposedModelCall,
    ProposedToolCall,
    decision_to_payload,
)
from harness.interaction import PermissionRequest, Resolver
from harness.messages import Message
from harness.provider import ModelProvider, Usage, collect
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import ModelId, new_call_id


class ModelDispatchBlocked(Exception):
    pass


@dataclass(frozen=True)
class ToolOutcome:
    text: str | None
    blob: BlobRef | None
    is_error: bool


class Dispatcher:
    def __init__(
        self,
        *,
        session: Session,
        registry: ToolRegistry,
        hooks: HookBus,
        resolver: Resolver,
    ) -> None:
        self.session = session
        self.registry = registry
        self.hooks = hooks
        self.resolver = resolver

    async def _run_chain(self, action) -> tuple[object | None, str | None]:
        """Run hooks + Ask resolution. Returns (effective_action, denial_reason)."""
        outcome = await self.hooks.run_dispatch(action)
        for name, decision in outcome.decisions:
            self.session.append(
                HookDecided(call_id=action.call_id, hook=name,
                            decision=decision_to_payload(decision))
            )
        if outcome.blocked is not None:
            return None, f"blocked by policy: {outcome.blocked.reason}"
        if outcome.ask is not None:
            self.session.append(
                PermissionRequested(call_id=action.call_id, reason=outcome.ask.reason)
            )
            allowed = await self.resolver.resolve(
                PermissionRequest(call_id=action.call_id, action=outcome.effective,
                                  reason=outcome.ask.reason)
            )
            self.session.append(
                PermissionResolved(call_id=action.call_id, allowed=allowed,
                                   resolver=self.resolver.name)
            )
            if not allowed:
                return None, "denied by user"
        return outcome.effective, None

    async def dispatch_tool(self, call: ProposedToolCall) -> ToolOutcome:
        self.session.append(
            ToolCallProposed(call_id=call.call_id, tool=call.tool, args=dict(call.args))
        )
        effective, denial = await self._run_chain(call)
        if denial is not None:
            self.session.append(
                ToolCallCompleted(call_id=call.call_id, result_text=denial, is_error=True)
            )
            return ToolOutcome(text=denial, blob=None, is_error=True)
        if not isinstance(effective, ProposedToolCall):
            # a hook rewrote tool -> model; fail closed rather than crash
            denial = "blocked by policy: rewrite changed action type — refused"
            self.session.append(
                ToolCallCompleted(call_id=call.call_id, result_text=denial, is_error=True)
            )
            return ToolOutcome(text=denial, blob=None, is_error=True)
        self.session.append(
            DispatchResolved(call_id=call.call_id, kind="tool",
                             tool=effective.tool, args=dict(effective.args))
        )
        started = time.monotonic()
        try:
            raw = await self.registry.get(effective.tool)(dict(effective.args))
            is_error = False
        except Exception as exc:
            raw, is_error = f"tool error: {exc}", True
        duration_ms = int((time.monotonic() - started) * 1000)
        text: str | None = raw
        blob: BlobRef | None = None
        if not is_error and len(raw.encode()) > INLINE_THRESHOLD:
            blob, text = self.session.blobs.put(raw.encode()), None
        self.session.append(
            ToolCallCompleted(call_id=call.call_id, result_text=text, result_blob=blob,
                              is_error=is_error, duration_ms=duration_ms)
        )
        return ToolOutcome(text=text, blob=blob, is_error=is_error)

    async def dispatch_model(
        self,
        *,
        provider: ModelProvider,
        model: ModelId,
        messages: list[Message],
        tools: tuple[ToolSpec, ...],
    ) -> tuple[Message, Usage]:
        call = ProposedModelCall(call_id=new_call_id(), model=model)
        self.session.append(ModelCallProposed(call_id=call.call_id, model=model))
        effective, denial = await self._run_chain(call)
        if denial is not None:
            raise ModelDispatchBlocked(denial)
        if not isinstance(effective, ProposedModelCall):
            # a hook rewrote model -> tool; fail closed rather than crash
            raise ModelDispatchBlocked("rewrite changed action type — refused")
        self.session.append(
            DispatchResolved(call_id=call.call_id, kind="model", model=effective.model)
        )
        self.session.append(ModelCallStarted(call_id=call.call_id, model=effective.model))
        started = time.monotonic()
        message, usage, stop_reason = await collect(
            provider.complete(model=effective.model, messages=messages, tools=tools)
        )
        self.session.append(
            ModelCallCompleted(
                call_id=call.call_id, model=effective.model,
                message=message.model_dump(), usage=usage.as_dict(),
                stop_reason=stop_reason,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
        return message, usage
