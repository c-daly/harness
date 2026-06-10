"""ModelProvider protocol + owned chunk taxonomy + FakeProvider.

LiteLLM adapter and per-provider conformance suites are Phase 2 (spec item 3).
"""

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Sequence, runtime_checkable

from harness.messages import Message, Role, TextBlock, ToolCallBlock
from harness.tools import ToolSpec
from harness.types import CallId, ModelId, ToolName, new_call_id


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    call_id: CallId
    tool: ToolName | None
    args_json: str  # FakeProvider sends complete JSON; partial accumulation is Phase 2


@dataclass(frozen=True)
class UsageReport:
    usage: Usage


@dataclass(frozen=True)
class StreamStop:
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"


Chunk = TextDelta | ThinkingDelta | ToolCallDelta | UsageReport | StreamStop


# NOTE: implementations may be async generators; strict mypy may flag the def-vs-asyncgen mismatch — revisit with the LiteLLM adapter (Phase 2)
@runtime_checkable
class ModelProvider(Protocol):
    def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> AsyncIterator[Chunk]: ...


async def collect(stream: AsyncIterator[Chunk]) -> tuple[Message, Usage, str]:
    """Accumulate a chunk stream into (assistant message, usage, stop_reason).

    stop_reason is "unknown" if the stream ends without a StreamStop —
    truncation must never masquerade as a clean end_turn."""
    text_parts: list[str] = []
    tool_blocks: list[ToolCallBlock] = []
    usage = Usage()
    stop_reason = "unknown"
    async for chunk in stream:
        match chunk:
            case TextDelta(text=t):
                text_parts.append(t)
            case ToolCallDelta(call_id=cid, tool=tool, args_json=raw):
                # early fail with context; Pydantic re-validates unconditionally
                assert tool is not None, f"ToolCallDelta {cid} arrived with tool=None"
                tool_blocks.append(
                    ToolCallBlock(call_id=cid, tool=tool, args=json.loads(raw))
                )
            case UsageReport(usage=u):
                usage = u  # last report wins; incremental accumulation is Phase 2
            case StreamStop(stop_reason=sr):
                stop_reason = sr
            case ThinkingDelta():
                pass  # no ThinkingBlock in the message model yet; dropped by design
    blocks: list = []
    if text_parts:
        blocks.append(TextBlock(text="".join(text_parts)))
    blocks.extend(tool_blocks)
    return Message(role=Role.ASSISTANT, blocks=tuple(blocks)), usage, stop_reason


# --- scripted fake ---

def text_turn(text: str, *, usage: Usage = Usage()) -> list[Chunk]:
    return [TextDelta(text=text), UsageReport(usage=usage), StreamStop(stop_reason="end_turn")]


def tool_call_turn(
    text: str, tool: ToolName, args: dict[str, Any], *,
    call_id: CallId | None = None, usage: Usage = Usage(),
) -> list[Chunk]:
    return [
        TextDelta(text=text),
        ToolCallDelta(call_id=call_id or new_call_id(), tool=tool, args_json=json.dumps(args)),
        UsageReport(usage=usage),
        StreamStop(stop_reason="tool_use"),
    ]


@dataclass
class FakeProvider:
    script: list[list[Chunk]]
    calls: list[Sequence[Message]] = field(default_factory=list)

    async def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[Chunk]:
        self.calls.append(tuple(messages))
        if not self.script:
            raise RuntimeError("FakeProvider script exhausted")
        for chunk in self.script.pop(0):
            yield chunk
