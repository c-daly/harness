# src/harness/loop.py
"""The agent loop: build context, dispatch model, dispatch tools concurrently, repeat."""

import asyncio

from harness.dispatcher import Dispatcher
from harness.events import CustomEvent, ErrorRaised, SessionEnded, UserMessage
from harness.hooks import Annotate, Emit, HookBus, Inject, LifecyclePoint, ProposedToolCall
from harness.interaction import Resolver
from harness.messages import Message
from harness.provider import ModelProvider
from harness.session import Session
from harness.tools import ToolRegistry
from harness.types import ModelId


class AgentLoop:
    def __init__(
        self,
        *,
        session: Session,
        provider: ModelProvider,
        registry: ToolRegistry,
        hooks: HookBus,
        resolver: Resolver,
        model: ModelId,
        system_prompt: str,
        max_iterations: int = 20,
        history: list[Message] | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.registry = registry
        self.hooks = hooks
        self.model = model
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        # history seeds a RESUMED transcript (resume_session's fold output).
        # Never call start() on a resumed loop: the session already started
        # and SESSION_START hooks (memory briefs) must not double-inject.
        self.history: list[Message] = list(history) if history else []
        self.dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=resolver
        )
        self._ended = False

    async def _apply_contributions(self, point: LifecyclePoint, ctx: dict) -> None:
        contributions, warnings = await self.hooks.run_lifecycle(point, ctx)
        for c in contributions:
            match c:
                case Inject(text=text):
                    if point is LifecyclePoint.SESSION_START:
                        self.system_prompt = f"{self.system_prompt}\n\n{text}"
                    else:
                        self.session.append(ErrorRaised(
                            where=f"lifecycle:{point}",
                            message="Inject ignored outside session_start",
                        ))
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
        """Run one user turn to completion.

        Raises ModelDispatchBlocked (policy refused the model call) or
        infrastructure errors from dispatch. After any raise, this loop's
        history may hold an unanswered user message or unpaired tool calls:
        treat the loop as dead — start a new session rather than calling
        run_turn again.
        """
        self.session.append(UserMessage(text=user_text))
        self.history.append(Message.user_text(user_text))
        for _ in range(self.max_iterations):
            messages = [Message.system_text(self.system_prompt), *self.history]
            assistant, _usage = await self.dispatcher.dispatch_model(
                provider=self.provider, model=self.model,
                messages=messages, tools=self.registry.specs(),
            )
            self.history.append(assistant)
            calls = assistant.tool_calls()
            if not calls:
                return assistant.text()
            try:
                outcomes = await asyncio.gather(*[
                    self.dispatcher.dispatch_tool(
                        ProposedToolCall(call_id=c.call_id, tool=c.tool, args=c.args)
                    )
                    for c in calls
                ])
            except Exception as exc:
                # dispatch_tool converts tool errors to results; only infrastructure
                # failures (log write, blob store) reach here. Siblings are cancelled
                # by gather; history is left without tool results — callers must
                # treat this loop as dead (see docstring).
                try:
                    self.session.append(ErrorRaised(
                        where="loop:tool_dispatch",
                        message=f"{type(exc).__name__}: {exc}",
                    ))
                except Exception:
                    pass  # the log itself may be the failing infrastructure
                raise
            for call, outcome in zip(calls, outcomes):
                self.history.append(
                    Message.tool_result(
                        call.call_id, text=outcome.text, blob=outcome.blob,
                        is_error=outcome.is_error,
                    )
                )
        self.session.append(
            ErrorRaised(where="loop", message=f"max iterations ({self.max_iterations}) reached")
        )
        return f"[stopped: max iterations ({self.max_iterations}) reached]"

    async def end(self) -> None:
        if self._ended:
            raise RuntimeError("AgentLoop.end() already called")
        self._ended = True
        await self._apply_contributions(
            LifecyclePoint.SESSION_END, {"session_id": self.session.id}
        )
        self.session.append(SessionEnded())
