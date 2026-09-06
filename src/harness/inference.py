"""One bounded inference operation. Tool proposals are data, never execution.

Byte/frame bounds are enforced locally, including reasoning and tool arguments.
Token limits are sent to the adapter; a tokenizer-independent byte bound is not
a token estimate. Async cancellation closes the stream before control returns.
"""

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Callable, Literal, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.errors import ContextOverflow, MalformedStreamError, ProviderError, ToolCallLimitExceeded
from harness.messages import Message
from harness.provider import Chunk, StreamStop, Usage, UsageReport, collect
from harness.tools import ToolSpec, validate_schema
from harness.types import CallId, ModelId


class InferenceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    model: ModelId
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: Literal["none", "auto"] = "none"
    parallel_tool_calls: bool | None = Field(default=None, strict=True)
    purpose: str = Field(min_length=1, max_length=128)
    max_input_bytes: int = Field(default=4 * 1024 * 1024, gt=0, strict=True)
    max_output_bytes: int = Field(default=1024 * 1024, gt=0, strict=True)
    max_output_tokens: int = Field(default=4096, gt=0, strict=True)
    max_stream_chunks: int = Field(default=65536, gt=0, strict=True)
    timeout_seconds: float = Field(default=120, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    response_schema: dict[str, Any] | None = None

    @field_validator("response_schema")
    @classmethod
    def local_schema(cls, value):
        if value is not None:
            validate_schema(value)
        return value


class InferenceProvider(Protocol):
    def infer(self, request: InferenceRequest) -> AsyncIterator[Chunk]: ...


@dataclass(frozen=True)
class LegacyCompletionAdapter:
    """Migration for third-party complete() providers; only local bounds apply.

The dispatcher never uses this adapter to turn a catalog agent into inference.
Sampling and structured-output requests require an actual inference adapter.
"""

    provider: Any

    def infer(self, request: InferenceRequest) -> AsyncIterator[Chunk]:
        if request.temperature is not None or request.response_schema is not None:
            raise ProviderError("legacy completion provider does not support inference options")
        return self.provider.complete(model=request.model, messages=request.messages,
                                      tools=request.tools)


@dataclass(frozen=True)
class InferenceResult:
    message: Message
    usage: Usage
    stop_reason: str
    structured: Any = None
    model: ModelId | None = None
    call_id: CallId | None = None


def check_input(request: InferenceRequest) -> None:
    """Measure the complete owned payload, including schemas and metadata."""
    input_bytes(request.messages, request.tools, request.response_schema, limit=request.max_input_bytes)


def input_bytes(messages, tools, schema=None, *, limit):
    """Shared byte accounting for preparation and final dispatch validation."""
    size = 0
    payload = {"messages": [m.model_dump(mode="json") for m in messages],
               "tools": [asdict(t) for t in tools], "schema": schema}
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(payload):
        size += len(part.encode("utf-8"))
        if size > limit:
            raise ContextOverflow("inference input exceeds the configured byte limit")
    return size


async def collect_bounded(
    source: AsyncIterator[Chunk],
    request: InferenceRequest,
    *,
    on_chunk: Callable[[Chunk], None] | None = None,
) -> tuple[Message, Usage, str]:
    """Bound an owned response stream without changing its execution kind.

    Callers validate the request before opening the source. Reject excess bytes,
    frames, and reported tokens before observers or the collector see them, and
    close the source before publishing any terminal outcome. Unknown usage stays
    unknown; this cannot cap a CLI's generation before it reports token usage.
    """

    async def bounded():
        size = count = 0
        terminal = False
        async for chunk in source:
            count += 1
            # Include identifiers, signatures, metadata, and empty frames so an
            # adapter cannot evade limits by emitting many tiny/non-text chunks.
            from harness.provider import TextDelta, ThinkingDelta, ToolCallDelta
            match chunk:
                case TextDelta(text=text):
                    amount = len(text.encode("utf-8"))
                case ThinkingDelta(text=text, signature=sig):
                    amount = len(text.encode("utf-8")) + len((sig or "").encode("utf-8"))
                case ToolCallDelta(call_id=cid, tool=tool, args_json=raw):
                    amount = len((str(cid or "") + str(tool or "") + raw).encode("utf-8"))
                case _:
                    amount = len(json.dumps(asdict(chunk)).encode("utf-8"))
            size += amount
            if size > request.max_output_bytes or count > request.max_stream_chunks:
                raise ProviderError("response stream exceeds the configured output limit")
            if (isinstance(chunk, UsageReport) and chunk.usage.output_tokens is not None
                    and chunk.usage.output_tokens > request.max_output_tokens):
                raise ProviderError("response stream exceeds the requested token limit")
            if isinstance(chunk, StreamStop):
                if terminal:
                    raise MalformedStreamError("response stream received multiple terminal markers")
                terminal = True
            elif terminal and isinstance(chunk, (TextDelta, ThinkingDelta, ToolCallDelta)):
                raise MalformedStreamError("response stream received content after its terminal marker")
            if on_chunk is not None:
                on_chunk(chunk)
            yield chunk

    async with asyncio.timeout(request.timeout_seconds):
        try:
            message, usage, stop = await collect(bounded())
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
    if stop == "unknown":
        raise MalformedStreamError("response stream ended without a terminal marker")
    return message, usage, stop


async def infer(
    provider: InferenceProvider,
    request: InferenceRequest,
    *,
    on_chunk: Callable[[Chunk], None] | None = None,
) -> InferenceResult:
    # Copy and revalidate nested mutable values and model_copy bypasses at entry.
    request = InferenceRequest.model_validate(request.model_dump())
    check_input(request)
    method = getattr(provider, "infer", None)
    if method is None:
        raise ProviderError("provider has no bounded inference contract")
    message, usage, stop = await collect_bounded(method(request), request, on_chunk=on_chunk)
    if message.tool_calls() and request.tool_choice == "none":
        raise MalformedStreamError("inference proposed an unadvertised tool with tool_choice=none")
    if request.parallel_tool_calls is False and len(message.tool_calls()) > 1:
        raise ToolCallLimitExceeded(
            "inference must propose at most one tool call per response; "
            "no calls in this batch were executed"
        )
    # Native agents return bad names/arguments through ordinary tool dispatch,
    # preserving hooks, audit facts, and useful error feedback for correction.
    structured = None
    if request.response_schema is not None:
        if stop != "end_turn" or message.tool_calls():
            raise MalformedStreamError("structured inference did not complete its schema response")
        try:
            structured = json.loads(message.text(), parse_constant=_invalid_constant)
            Draft202012Validator(request.response_schema).validate(structured)
        except (ValueError, ValidationError):
            raise MalformedStreamError("inference response violates its JSON schema") from None
    return InferenceResult(message, usage, stop, structured)


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")
