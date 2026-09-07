"""Frontend-independent prompt ownership and recovery. No inference on the input path.

The initial queue is deliberately in memory. Pending text becomes a UserMessage
only when the executor starts its turn; queue operations never fabricate history.
"""

import asyncio
from dataclasses import dataclass, replace
from typing import Awaitable, Callable
from harness.agent import AgentResult


@dataclass(frozen=True)
class PendingPrompt:
    id: int
    text: str
    expand_mentions: bool = True


class InteractionController:
    def __init__(self, *, max_pending: int = 16, max_prompt_chars: int = 128_000):
        if max_pending <= 0 or max_prompt_chars <= 0:
            raise ValueError("queue limits must be positive")
        self.max_pending = max_pending
        self.max_prompt_chars = max_prompt_chars
        self._pending: list[PendingPrompt] = []
        self._next_id = 1
        self.active: PendingPrompt | None = None
        self.last_failed: PendingPrompt | None = None
        self.paused = False
        self.paused_by_user = False
        self.phase = "idle"
        self.last_result: AgentResult | None = None

    @property
    def pending(self) -> tuple[PendingPrompt, ...]:
        return tuple(self._pending)

    def _validate(self, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("prompt is empty")
        if len(text) > self.max_prompt_chars:
            raise ValueError(f"prompt exceeds {self.max_prompt_chars} characters")
        return text

    def submit(self, text: str, *, expand_mentions: bool = True) -> PendingPrompt:
        text = self._validate(text)
        if len(self._pending) >= self.max_pending:
            raise ValueError(f"prompt queue is full ({self.max_pending}); draft preserved")
        prompt = PendingPrompt(self._next_id, text, expand_mentions)
        self._pending.append(prompt)
        self._next_id += 1
        return prompt

    def edit(self, prompt_id: int, text: str) -> None:
        text = self._validate(text)
        for index, prompt in enumerate(self._pending):
            if prompt.id == prompt_id:
                self._pending[index] = replace(prompt, text=text)
                return
        raise KeyError(prompt_id)

    def remove(self, prompt_id: int) -> PendingPrompt:
        for index, prompt in enumerate(self._pending):
            if prompt.id == prompt_id:
                return self._pending.pop(index)
        raise KeyError(prompt_id)

    def clear(self) -> None:
        self._pending.clear()

    def pause(self, *, user_requested: bool = True) -> None:
        self.paused = True
        self.paused_by_user |= user_requested

    def resume(self) -> None:
        self.paused = False
        self.paused_by_user = False
        if self.active is None:
            self.phase = "idle"

    async def run_next(self, execute: Callable[[PendingPrompt], Awaitable[AgentResult | None]]) -> bool:
        if self.active is not None or self.paused or not self._pending:
            return False
        self.active = self._pending.pop(0)
        self.phase = "preparing"
        self.last_result = None
        try:
            self.last_result = await execute(self.active)
        except asyncio.CancelledError:
            self.phase = "interrupted"
            self.last_failed = self.active
            self.paused = True
            raise
        except Exception:
            self.phase = "failed"
            self.last_failed = self.active
            self.paused = True
            raise
        else:
            if self.last_result is not None and self.last_result.status != "completed":
                self.phase = self.last_result.status
                self.paused = True
                self.last_failed = self.active
            else:
                self.phase = "idle"
            return True
        finally:
            self.active = None
