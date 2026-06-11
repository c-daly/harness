"""The LiteLLM adapter: owned Message/Chunk types <-> litellm's OpenAI dialect.

Everything provider-specific is contained here. The kernel never imports
litellm except through catalog (cost map) and this module.
"""

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence

from harness.errors import (
    AuthFailed,
    ContextOverflow,
    NetworkFailed,
    Overloaded,
    ProviderError,
    RateLimited,
)
from harness.messages import (
    ImageBlock,
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from harness.provider import (
    Chunk,
    StreamStop,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageReport,
)
from harness.tools import ToolSpec
from harness.types import CallId, ModelId, ToolName

_FINISH_REASON = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def map_exception(exc: Exception) -> ProviderError:
    import litellm

    mapping = (
        (litellm.RateLimitError, RateLimited),
        (litellm.ContextWindowExceededError, ContextOverflow),
        (litellm.AuthenticationError, AuthFailed),
        (litellm.ServiceUnavailableError, Overloaded),
        (litellm.InternalServerError, Overloaded),
        (litellm.APIConnectionError, NetworkFailed),
        (litellm.Timeout, NetworkFailed),
    )
    for litellm_type, ours in mapping:
        if isinstance(exc, litellm_type):
            return ours(str(exc))
    return ProviderError(str(exc))


def _blocks_to_content(message: Message) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for block in message.blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            raise ProviderError("image blocks not yet supported by the litellm adapter")
    if content and message.cache_hint:
        content[-1]["cache_control"] = {"type": "ephemeral"}
    return content


def _messages_to_openai(messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role in (Role.SYSTEM, Role.USER):
            out.append({"role": message.role.value, "content": _blocks_to_content(message)})
        elif message.role is Role.ASSISTANT:
            entry: dict[str, Any] = {"role": "assistant"}
            content = _blocks_to_content(message)
            if content:
                entry["content"] = content
            tool_calls = [
                {
                    "id": str(b.call_id),
                    "type": "function",
                    "function": {"name": str(b.tool), "arguments": json.dumps(b.args)},
                }
                for b in message.blocks
                if isinstance(b, ToolCallBlock)
            ]
            if tool_calls:
                entry["tool_calls"] = tool_calls
            thinking = [
                {"type": "thinking", "thinking": b.text, **b.provider_extras}
                for b in message.blocks
                if isinstance(b, ThinkingBlock)
            ]
            if thinking:
                entry["thinking_blocks"] = thinking  # litellm round-trips signatures
            out.append(entry)
        elif message.role is Role.TOOL:
            for block in message.blocks:
                if isinstance(block, ToolResultBlock):
                    out.append({
                        "role": "tool",
                        "tool_call_id": str(block.call_id),
                        # TODO Phase 5: dereference the blob (BlobStore access decision) instead of this placeholder
                        "content": block.text or "(result in blob sidecar)",
                    })
    return out


def _tools_to_openai(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": str(t.name),
                "description": t.description,
                "parameters": t.parameters or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _normalize_chunk(chunk: Any) -> list[Chunk]:
    """One litellm stream chunk -> zero or more owned chunks. Pure; testable."""
    out: list[Chunk] = []
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        # Anthropic surfaces cache tokens top-level; OpenAI nests read-cache
        # under prompt_tokens_details.cached_tokens (empirically verified via
        # recorded fixtures). Read both; top-level wins when present.
        details = getattr(usage, "prompt_tokens_details", None)
        nested_cached = (getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        out.append(UsageReport(usage=Usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_read_tokens=(getattr(usage, "cache_read_input_tokens", 0) or 0) or nested_cached,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )))
    if not getattr(chunk, "choices", None):
        return out
    choice = chunk.choices[0]
    delta = choice.delta
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        out.append(ThinkingDelta(text=reasoning))
    # Anthropic-via-litellm may surface structured thinking blocks (carrying the
    # signature required for round-trip) instead of / alongside reasoning_content.
    # UNVERIFIED against real streams until Anthropic conformance fixtures exist;
    # this defensive read captures a signature wherever it appears and avoids
    # double-counting text mirrored in both fields.
    for tb in getattr(delta, "thinking_blocks", None) or ():
        sig = getattr(tb, "signature", None)
        text = getattr(tb, "thinking", None)
        if sig and not text:
            out.append(ThinkingDelta(text="", signature=sig))
        elif text and not reasoning:
            out.append(ThinkingDelta(text=text, signature=sig))
    if getattr(delta, "content", None):
        out.append(TextDelta(text=delta.content))
    for tc in getattr(delta, "tool_calls", None) or ():
        fn = tc.function
        out.append(ToolCallDelta(
            index=tc.index,
            call_id=CallId(tc.id) if getattr(tc, "id", None) else None,
            tool=ToolName(fn.name) if getattr(fn, "name", None) else None,
            args_json=getattr(fn, "arguments", None) or "",
        ))
    finish = getattr(choice, "finish_reason", None)
    if finish:
        out.append(StreamStop(stop_reason=_FINISH_REASON.get(finish, finish)))
    return out


@dataclass
class LiteLLMProvider:
    api_base: str | None = None
    api_key_env: str | None = None  # resolved by litellm from env; recorded for diagnostics

    async def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[Chunk]:
        import litellm

        kwargs: dict[str, Any] = {
            "model": str(model),
            "messages": _messages_to_openai(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = _tools_to_openai(tools)
        if self.api_base:
            kwargs["api_base"] = self.api_base
        try:
            stream = await litellm.acompletion(**kwargs)
            async for raw in stream:
                for chunk in _normalize_chunk(raw):
                    yield chunk
        except ProviderError:
            raise
        except Exception as exc:
            raise map_exception(exc) from exc
