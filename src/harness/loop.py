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
    ) -> None:
        self.session = session
        self.provider = provider
        self.registry = registry
        self.hooks = hooks
        self.model = model
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.history: list[Message] = []
        self.dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=resolver
        )

    async def start(self) -> None:
        self.session.start()
        contributions, warnings = await self.hooks.run_lifecycle(
            LifecyclePoint.SESSION_START, ctx={"session_id": self.session.id}
        )
        for c in contributions:
            match c:
                case Inject(text=text):
                    self.system_prompt = f"{self.system_prompt}\n\n{text}"
                case Emit(namespace=ns, name=name, data=data):
                    self.session.append(CustomEvent(namespace=ns, name=name, data=dict(data)))
                case Annotate(note=note):
                    self.session.append(
                        CustomEvent(namespace="annotation", name="note", data={"text": note})
                    )
        for warning in warnings:
            self.session.append(ErrorRaised(where="lifecycle:session_start", message=warning))

    async def run_turn(self, user_text: str) -> str:
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
            outcomes = await asyncio.gather(*[
                self.dispatcher.dispatch_tool(
                    ProposedToolCall(call_id=c.call_id, tool=c.tool, args=c.args)
                )
                for c in calls
            ])
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
        await self.hooks.run_lifecycle(
            LifecyclePoint.SESSION_END, ctx={"session_id": self.session.id}
        )
        self.session.append(SessionEnded())
