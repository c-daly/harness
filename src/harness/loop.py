# src/harness/loop.py
"""The agent loop: build context, dispatch model, dispatch tools concurrently, repeat."""

import asyncio
from typing import Callable

from harness.dispatcher import Dispatcher, ToolOutcome
from harness.events import CustomEvent, ErrorRaised, SessionEnded, ToolCallCancelled, UserInterrupt, UserMessage
from harness.hooks import Annotate, Emit, HookBus, Inject, LifecyclePoint, ProposedToolCall
from harness.interaction import Resolver
from harness.messages import Message, Role, ToolResultBlock
from harness.provider import Chunk, ModelProvider
from harness.session import Session
from harness.tools import ToolRegistry
from harness.types import CallId, ModelId


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
        pricing: dict[str, float] | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.registry = registry
        self.hooks = hooks
        self.model = model  # mutable by design: reassigning between turns is the /model switch; the dispatcher receives it per call
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        # history seeds a RESUMED transcript (resume_session's fold output).
        # Never call start() on a resumed loop: the session already started
        # and SESSION_START hooks (memory briefs) must not double-inject.
        self.history: list[Message] = list(history) if history else []
        self.pricing = pricing
        self.dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=resolver
        )
        self.on_chunk: Callable[[Chunk], None] | None = None  # set by the frontend; root loop only
        self._ended = False
        # per-turn tool outcomes, recorded at completion so interrupt_turn can
        # tell completed-but-unpaired calls from truly-cancelled ones
        self._turn_outcomes: dict[CallId, ToolOutcome] = {}

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
                # recorded at completion so interrupt_turn can distinguish
                # completed-but-unpaired calls from truly-cancelled ones
                self._turn_outcomes[call.call_id] = outcome
                return outcome

            try:
                outcomes = await asyncio.gather(*[_run_one(c) for c in calls])
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

    def repair_turn(self) -> int:
        """Close unpaired tool_use blocks in the trailing assistant message
        (real recorded results for completed calls; ToolCallCancelled + the
        fold-matching synthetic for the rest). Returns the number repaired.
        Shared by interrupt_turn (Esc) and the TUI's exception path -- after
        repair the loop is safe to keep using."""
        repaired = 0
        for call in self._dangling_tool_calls():
            outcome = self._turn_outcomes.get(call.call_id)
            if outcome is not None:
                self.history.append(Message.tool_result(
                    call.call_id, text=outcome.text, blob=outcome.blob,
                    is_error=outcome.is_error,
                ))
            else:
                self.session.append(ToolCallCancelled(call_id=call.call_id))
                self.history.append(Message.tool_result(
                    call.call_id, text="(call did not complete)", is_error=True,
                ))
            repaired += 1
        return repaired

    def interrupt_turn(self) -> None:
        """In-process mirror of resume repair: call after cancelling run_turn.

        Delegates to repair_turn to close unpaired tool_use blocks in the
        trailing assistant message: calls that COMPLETED before cancellation
        get their real recorded result (the log already holds ToolCallCompleted);
        truly-incomplete calls get a ToolCallCancelled event + the synthetic
        error result fold renders for them. Repairs are idempotent (a second
        call finds everything paired); each call appends exactly one
        UserInterrupt. The loop stays alive."""
        self.repair_turn()
        self.session.append(UserInterrupt())

    def _dangling_tool_calls(self):
        """tool_use blocks in the last assistant message lacking a paired result.

        Walks history backwards collecting result call_ids until the last assistant
        message that has tool_calls(); returns its calls that lack paired results.
        Returns empty list when no trailing unpaired assistant-with-tools exists.
        Only the segment AFTER the last assistant message is inspected, so an old
        fully-paired assistant deeper in history is never re-repaired."""
        # Collect result call_ids and find the last assistant-with-tools
        paired_ids: set[str] = set()
        for msg in reversed(self.history):
            if msg.role == Role.TOOL:
                for block in msg.blocks:
                    if isinstance(block, ToolResultBlock):
                        paired_ids.add(block.call_id)
            elif msg.role == Role.ASSISTANT:
                calls = msg.tool_calls()
                if not calls:
                    # a trailing text-only assistant means the turn completed cleanly -- nothing to repair
                    return []
                # Return calls lacking a paired result
                return [c for c in calls if c.call_id not in paired_ids]
        return []

    async def end(self) -> None:
        if self._ended:
            raise RuntimeError("AgentLoop.end() already called")
        self._ended = True
        await self._apply_contributions(
            LifecyclePoint.SESSION_END, {"session_id": self.session.id}
        )
        self.session.append(SessionEnded())
