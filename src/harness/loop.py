# src/harness/loop.py
"""The agent loop: build context, dispatch model, dispatch tools concurrently, repeat."""

import asyncio
from typing import Callable

from harness.dispatcher import Dispatcher, ToolOutcome
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
from harness.messages import Message, Role, ToolResultBlock
from harness.provider import Chunk, ModelProvider
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
        redact: StringRedactor = identity_redact,
    ) -> None:
        self.session = session
        self.provider = provider
        self.registry = registry
        self.hooks = hooks
        self.model = model
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.history: list[Message] = list(history) if history else []
        self.pricing = pricing
        self.dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=resolver, redact=redact
        )
        self.on_chunk: Callable[[Chunk], None] | None = None
        self._ended = False
        self._turn_outcomes: dict[CallId, ToolOutcome] = {}

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
        self.session.append(UserMessage(text=user_text))
        self.history.append(Message.user_text(user_text))
        for _ in range(self.max_iterations):
            messages = [Message.system_text(self.system_prompt), *self.history]
            assistant, _usage = await self.dispatcher.dispatch_model(
                provider=self.provider,
                model=self.model,
                messages=messages,
                tools=self.registry.specs(),
                pricing=self.pricing,
                on_chunk=self.on_chunk,
            )
            self.history.append(assistant)
            calls = assistant.tool_calls()
            if not calls:
                return assistant.text()
            self._turn_outcomes.clear()

            async def _run_one(call):
                outcome = await self.dispatcher.dispatch_tool(
                    ProposedToolCall(call_id=call.call_id, tool=call.tool, args=call.args)
                )
                self._turn_outcomes[call.call_id] = outcome
                return outcome

            try:
                outcomes = await asyncio.gather(*[_run_one(c) for c in calls])
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
            ErrorRaised(where="loop", message=f"max iterations ({self.max_iterations}) reached")
        )
        return f"[stopped: max iterations ({self.max_iterations}) reached]"

    def repair_turn(self) -> int:
        repaired = 0
        for call in self._dangling_tool_calls():
            outcome = self._turn_outcomes.get(call.call_id)
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
