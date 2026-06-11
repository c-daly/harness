"""ModelProvider protocol + owned chunk taxonomy + FakeProvider.

LiteLLM adapter and per-provider conformance suites are Phase 2 (spec item 3).
"""

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Sequence, runtime_checkable

from harness.messages import Message, Role, TextBlock, ThinkingBlock, ToolCallBlock
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
    signature: str | None = None  # arrives on the final thinking chunk when provider signs


@dataclass(frozen=True)
class ToolCallDelta:
    index: int                    # stream slot; stable across a call's fragments
    call_id: CallId | None        # set on the first fragment only
    tool: ToolName | None         # set on the first fragment only
    args_json: str                # fragment; concatenate per index


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

    Tool-call fragments accumulate per stream index (id/name from the first
    fragment); malformed accumulated JSON raises MalformedStreamError.
    Thinking deltas join into one ThinkingBlock (signature via provider_extras).
    stop_reason is "unknown" if the stream ends without a StreamStop."""
    from harness.errors import MalformedStreamError

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    thinking_signature: str | None = None
    pending: dict[int, dict[str, Any]] = {}   # index -> {call_id, tool, fragments}
    order: list[int] = []
    usage = Usage()
    stop_reason = "unknown"
    async for chunk in stream:
        match chunk:
            case TextDelta(text=t):
                text_parts.append(t)
            case ThinkingDelta(text=t, signature=sig):
                thinking_parts.append(t)
                if sig is not None:
                    thinking_signature = sig
            case ToolCallDelta(index=i, call_id=cid, tool=tool, args_json=raw):
                if i not in pending:
                    pending[i] = {"call_id": cid, "tool": tool, "fragments": []}
                    order.append(i)
                else:
                    if cid is not None:
                        pending[i]["call_id"] = cid
                    if tool is not None:
                        pending[i]["tool"] = tool
                pending[i]["fragments"].append(raw)
            case UsageReport(usage=u):
                usage = u  # last report wins
            case StreamStop(stop_reason=sr):
                stop_reason = sr
    blocks: list = []
    if thinking_parts:
        extras = {"signature": thinking_signature} if thinking_signature else {}
        blocks.append(ThinkingBlock(text="".join(thinking_parts), provider_extras=extras))
    if text_parts:
        blocks.append(TextBlock(text="".join(text_parts)))
    for i in order:
        slot = pending[i]
        joined = "".join(slot["fragments"])
        if slot["call_id"] is None or slot["tool"] is None:
            raise MalformedStreamError(f"tool call at index {i} never received id/name")
        try:
            args = json.loads(joined) if joined.strip() else {}
        except json.JSONDecodeError as exc:
            raise MalformedStreamError(
                f"tool call {slot['call_id']}: unparseable arguments: {exc}"
            ) from exc
        blocks.append(ToolCallBlock(call_id=slot["call_id"], tool=slot["tool"], args=args))
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
        ToolCallDelta(index=0, call_id=call_id or new_call_id(), tool=tool, args_json=json.dumps(args)),
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


class EchoProvider:
    """Infinite demo provider: echoes the last user text. Powers the TUI's
    no-model mode and multi-turn tests (FakeProvider scripts are finite)."""

    async def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[Chunk]:
        last = next(
            (m.text() for m in reversed(messages) if m.role == Role.USER and m.text()), ""
        )
        yield TextDelta(text=f"echo: {last}")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")
