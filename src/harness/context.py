"""Deterministic request windows over the canonical session and registered tools.

Profiles only narrow input and authority. They neither summarize nor store memory.
Pinned instructions, supplied context, and the current user/tool turn stay intact.
"""

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.errors import ContextOverflow
from harness.inference import input_bytes
from harness.messages import Message, Role, materialize_tool_results


class ContextPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    history_turns: int = Field(default=4, ge=1, le=128, strict=True)
    max_input_bytes: int = Field(default=32768, gt=0, le=4 * 1024 * 1024, strict=True)
    tools: tuple[str, ...] | None = Field(default=None, max_length=256)

    @field_validator("tools")
    @classmethod
    def exact_tool_names(cls, value):
        if value is not None and (len(set(value)) != len(value) or any(
                not name or any(c.isspace() or c in "\0*?[]" for c in name) for name in value)):
            raise ValueError("context tools must be unique exact tool names")
        return value

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate(tomllib.loads(path.read_text()))

    @property
    def digest(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True)
class PreparedContext:
    messages: tuple[Message, ...]
    input_bytes: int
    retained_turns: int
    omitted_turns: int
    omitted_messages: int


def prepare_context(prefix, history, tools, policy: ContextPolicy, *, max_input_bytes, blobs=None):
    """Drop only complete old user turns, first by count and then by byte budget.

    Leading history (for example a compaction summary) is pinned. Oversized
    current work fails explicitly rather than removing instructions or tool pairs.
    Only selected tool blobs are materialized, bounded before reading them.
    """
    starts = [i for i, message in enumerate(history) if message.role == Role.USER]
    pinned = list(prefix) + list(history[:starts[0]] if starts else history)
    groups = [history[start:end] for start, end in zip(starts, [*starts[1:], len(history)])]
    dropped = max(0, len(groups) - policy.history_turns)
    limit = min(max_input_bytes, policy.max_input_bytes)
    while True:
        selected = [m for group in groups[dropped:] for m in group]
        notice = ([Message.system_text(
            f"Context window: {dropped} earlier turn(s) omitted. Full history remains in the session."
        )] if dropped else [])
        messages = tuple(pinned + notice + selected)
        try:
            if blobs is not None:
                messages = tuple(materialize_tool_results(list(messages), blobs, max_bytes=limit))
            size = input_bytes(messages, tools, limit=limit)
        except ContextOverflow:
            if dropped >= len(groups) - 1:
                raise ContextOverflow(
                    "pinned context and current turn exceed the context profile byte limit; "
                    "reduce supplied context or compact the session"
                ) from None
            dropped += 1
            continue
        return PreparedContext(messages, size, len(groups) - dropped, dropped,
                               sum(len(group) for group in groups[:dropped]))


def render_context_policy(policy, tools):
    if policy is None:
        return "No context profile configured; full conversation history is used."
    names = ", ".join(str(tool.name) for tool in tools) or "none"
    return (f"Context profile: up to {policy.history_turns} recent turns, "
            f"{policy.max_input_bytes} input bytes. Tools: {names}. "
            "Full session history is retained; byte limits are not token limits.")
