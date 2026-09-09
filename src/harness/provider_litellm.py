"""The LiteLLM adapter: owned Message/Chunk types <-> litellm's OpenAI dialect.

Everything provider-specific is contained here. The kernel never imports
litellm except through catalog (cost map) and this module.
"""

import ipaddress
import json
import os
import time
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Sequence
from urllib.parse import urlsplit

from harness.catalog import Catalog, UnknownAliasError
from harness.errors import (
    AuthFailed,
    ContextOverflow,
    MalformedStreamError,
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

if TYPE_CHECKING:
    from harness.inference import InferenceRequest
    from harness.provider_antigravity import AntigravityProvider
    from harness.provider_claude_code import ClaudeCodeProvider
    from harness.provider_codex import CodexProvider

_FINISH_REASON = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}

_QUIETED = False


def _quiet(litellm_module) -> None:
    """Suppress litellm's stdout banners (they pollute -p output). The attribute
    set differs across versions -- set what exists, ignore what doesn't."""
    global _QUIETED
    if _QUIETED:
        return
    for attr, value in (("suppress_debug_info", True), ("set_verbose", False)):
        try:
            setattr(litellm_module, attr, value)
        except Exception:
            pass
    _QUIETED = True


def _network_failure(exc: Exception, api_base: str | None) -> NetworkFailed:
    try:
        endpoint = urlsplit(api_base or "")
        host = endpoint.hostname
    except ValueError:
        return NetworkFailed(
            "Inference connection failed or timed out. Check the configured endpoint."
        )
    # Route-only aliases need not satisfy the stricter owned-runtime validator.
    # Identify loopback independently so credentials/query strings cannot send
    # a local failure back to the raw SDK diagnostic.
    try:
        address = ipaddress.ip_address("127.0.0.1" if host in ("localhost", "localhost.") else host)
    except ValueError:
        return NetworkFailed(str(exc))
    if not address.is_loopback:
        return NetworkFailed(str(exc))
    # Describe the configured local transport, not the SDK's OpenAI branding.
    # Reconstruct only a numeric origin; omit credentials, paths, query/fragment
    # data, IPv6 scope identifiers, and upstream bodies. Invalid ports/schemes
    # still get local guidance without echoing the malformed authority.
    origin = "the configured local endpoint"
    try:
        port = endpoint.port
    except ValueError:
        pass
    else:
        if endpoint.scheme in ("http", "https"):
            host = str(address).split("%", 1)[0]
            authority = f"[{host}]" if address.version == 6 else host
            if port is not None:
                authority += f":{port}"
            origin = f"{endpoint.scheme}://{authority}"
    return NetworkFailed(
        f"Local inference connection failed or timed out at {origin}. "
        "Check that the local model server is running and responsive. "
        "A catalog alias alone does not start a server; configure a local runtime "
        "profile for on-demand startup."
    )


def map_exception(exc: Exception, *, api_base: str | None = None) -> ProviderError:
    import litellm

    _quiet(litellm)
    # litellm mis-raises missing-credentials as InternalServerError; sniff the
    # message so auth failures stay non-retryable (verified empirically via a
    # keyless --model run)
    lowered = str(exc).lower()
    if any(s in lowered for s in ("missing credentials", "api key", "authentication")):
        return AuthFailed(str(exc))
    if isinstance(exc, litellm.BadRequestError):
        # llama.cpp's typed context rejection can arrive as a generic SDK 400.
        # Inspect structured evidence, not phrases that a bad argument may quote.
        cause, seen = exc, set()
        while cause is not None and id(cause) not in seen and len(seen) < 16:
            seen.add(id(cause))
            body = getattr(cause, "body", None)
            response = getattr(cause, "response", None)
            if body is None and response is not None:
                try:
                    if len(response.content) <= 16384:
                        body = response.json()
                except (ValueError, AttributeError, RuntimeError):
                    pass
            if isinstance(body, dict):
                error = body.get("error", body)
                if isinstance(error, dict) and error.get("type") == "exceed_context_size_error":
                    return ContextOverflow(
                        "Model context limit exceeded. Use /compact <inference-alias> "
                        "or select a model with a larger context window."
                    )
            cause = cause.__cause__ or cause.__context__
    if isinstance(exc, litellm.InternalServerError):
        import httpx

        # LiteLLM can wrap an SDK transport failure in a synthetic HTTP 500.
        # Preserve the typed cause rather than guessing from provider prose.
        cause, seen = exc, set()
        while cause is not None and id(cause) not in seen and len(seen) < 16:
            seen.add(id(cause))
            if isinstance(cause, (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)):
                return _network_failure(exc, api_base)
            cause = cause.__cause__ or cause.__context__
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
            if ours is NetworkFailed:
                return _network_failure(exc, api_base)
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
                    if block.blob is not None and block.text is None:
                        raise ProviderError("tool result blob must be resolved before inference")
                    out.append({
                        "role": "tool",
                        "tool_call_id": str(block.call_id),
                        "content": block.text if block.text is not None else "",
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
        nested_cached = getattr(details, "cached_tokens", None) if details is not None else None
        cached = getattr(usage, "cache_read_input_tokens", None)
        out.append(UsageReport(usage=Usage(
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            cache_read_tokens=cached if cached is not None else nested_cached,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
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


async def _acomplete(
    *,
    model: ModelId,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
    api_base: str | None = None,
    api_key: str | None = None,
    request: "InferenceRequest | None" = None,
) -> AsyncIterator[Chunk]:
    """The shared litellm streaming core. Endpoint + key are per-call locals so
    a single provider instance is safe under concurrent (asyncio.gather) calls."""
    import litellm
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper

    _quiet(litellm)
    kwargs: dict[str, Any] = {
        "model": str(model),
        "messages": _messages_to_openai(messages),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    if request is not None:
        if tools:
            kwargs["tool_choice"] = request.tool_choice
            if request.parallel_tool_calls is not None:
                kwargs["parallel_tool_calls"] = request.parallel_tool_calls
        kwargs["max_tokens"] = request.max_output_tokens
        kwargs["timeout"] = request.timeout_seconds
        # SDK retries must not evade the dispatcher's shared budget/deadline.
        kwargs["num_retries"] = 0
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "harness_response", "strict": True,
                                "schema": request.response_schema},
            }
    stream = None
    owned_http = None
    try:
        if (str(model).startswith("openai/") and api_base and api_key
                and litellm.aclient_session is None):
            from litellm.llms.openai.common_utils import BaseOpenAILLM
            from openai import AsyncOpenAI

            # Explicit endpoints (including local catalogs) need no ambient
            # credential/endpoint resolution. Give each request its own client:
            # LiteLLM's client cache otherwise grows with changing deadlines.
            # Use the SDK's transport factory to preserve TLS/proxy settings.
            # A caller-supplied global transport remains borrowed, never closed.
            owned_http = BaseOpenAILLM._get_async_http_client()
            kwargs["client"] = AsyncOpenAI(
                api_key=api_key, base_url=api_base, http_client=owned_http,
                timeout=request.timeout_seconds if request is not None else 120,
                max_retries=0,
            )
        stream = await litellm.acompletion(**kwargs)
        async for raw in stream:
            chunks = _normalize_chunk(raw)
            # LiteLLM's OpenAI wrapper synthesizes a stop (including tool_calls)
            # on EOF without a provider finish_reason. Never turn a severed
            # response into completed work or executable partial tool arguments.
            if (isinstance(stream, CustomStreamWrapper) and stream.custom_llm_provider == "openai"
                    and any(isinstance(chunk, StreamStop) for chunk in chunks)
                    and not (stream.received_finish_reason or stream.intermittent_finish_reason)):
                raise MalformedStreamError("OpenAI-compatible response ended without a provider finish reason")
            for chunk in chunks:
                yield chunk
    except ProviderError:
        raise
    except Exception as exc:
        raise map_exception(exc, api_base=api_base) from exc
    finally:
        from anyio import CancelScope

        # AnyIO cancellation can otherwise interrupt every cleanup await. Keep
        # transport closure inside the request, including stream-close failure.
        with CancelScope(shield=True):
            try:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            finally:
                if owned_http is not None:
                    await owned_http.aclose()


@dataclass
class LiteLLMProvider:
    api_base: str | None = None
    api_key_env: str | None = None  # resolved per call; never stored as a literal credential

    def infer(self, request: "InferenceRequest") -> AsyncIterator[Chunk]:
        return _acomplete(model=request.model, messages=request.messages, tools=request.tools,
                          api_base=self.api_base,
                          api_key=os.environ.get(self.api_key_env) if self.api_key_env else None,
                          request=request)

    async def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[Chunk]:
        async with aclosing(_acomplete(
            model=model, messages=messages, tools=tools, api_base=self.api_base,
            api_key=os.environ.get(self.api_key_env) if self.api_key_env else None,
        )) as source:
            async for chunk in source:
                yield chunk


@dataclass
class CatalogProvider:
    """Resolves endpoint + key per call from the model string via the catalog,
    so any catalog model is reachable in one session. The model string is an
    ALIAS; an unknown alias falls back to a literal route on ambient env.
    Entries with backend="claude-code", backend="codex", or
    backend="antigravity" route to the matching subscription-CLI provider."""

    catalog: "Catalog"
    claude_code: "ClaudeCodeProvider | None" = None
    codex: "CodexProvider | None" = None
    antigravity: "AntigravityProvider | None" = None

    def agent_runtime_info(self, model: ModelId):
        try:
            resolved = self.catalog.resolve(str(model))
        except UnknownAliasError:
            return None
        if resolved.backend == "codex" and self.codex is not None:
            describe = getattr(self.codex, "agent_runtime_info", None)
            return describe(ModelId(resolved.route)) if describe is not None else None
        return None

    def execution_kind(self, model: ModelId) -> str:
        try:
            return self.catalog.resolve(str(model)).execution_kind
        except UnknownAliasError:
            return "inference"

    async def infer(self, request: "InferenceRequest") -> AsyncIterator[Chunk]:
        try:
            resolved = self.catalog.resolve(str(request.model))
        except UnknownAliasError:
            resolved = None
        if resolved is not None and resolved.execution_kind != "inference":
            raise ProviderError(f"{request.model!r} is an agent runtime, not a model inference route")
        from contextlib import nullcontext
        from harness.execution import current_model_call_id, current_scope
        from harness.scheduling import request_priority
        readiness = nullcontext()
        started = time.monotonic()
        if resolved is not None and resolved.local is not None:
            scope = current_scope.get()
            if scope is None:
                raise ProviderError("local profiles require bounded inference through a dispatcher")
            readiness = scope.resources.use(resolved, emit=scope.session.append,
                priority=request_priority(request.purpose, scope.depth), timeout_seconds=request.timeout_seconds,
                call_id=current_model_call_id.get())
        key = os.environ.get(resolved.api_key_env) if resolved and resolved.api_key_env else None
        if resolved and resolved.api_base and key is None:
            key = "local-no-key"
        async with readiness:
            remaining = request.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("local admission exceeded the inference deadline")
            effective_request = request.model_copy(update={"timeout_seconds": remaining})
            async with aclosing(_acomplete(
                model=resolved.route if resolved else request.model,
                messages=request.messages, tools=request.tools,
                api_base=resolved.api_base if resolved else None, api_key=key, request=effective_request,
            )) as source:
                async for chunk in source:
                    yield chunk

    def bind_dispatcher(self, dispatcher) -> None:
        if self.claude_code is not None:
            self.claude_code.bind_dispatcher(dispatcher)
        if self.codex is not None:
            self.codex.bind_dispatcher(dispatcher)
        if self.antigravity is not None:
            self.antigravity.bind_dispatcher(dispatcher)

    async def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[Chunk]:
        try:
            resolved = self.catalog.resolve(str(model))
        except UnknownAliasError:
            # not a catalog alias: treat the string as a literal route on ambient env
            async with aclosing(_acomplete(model=model, messages=messages, tools=tools)) as source:
                async for chunk in source:
                    yield chunk
            return
        if resolved.backend == "claude-code":
            if self.claude_code is None:
                raise ProviderError(
                    f"model {model!r} needs the claude-code backend, which is not wired"
                )
            async with aclosing(self.claude_code.complete(
                model=resolved.route, messages=messages, tools=tools
            )) as source:
                async for chunk in source:
                    yield chunk
            return
        if resolved.backend == "codex":
            if self.codex is None:
                raise ProviderError(
                    f"model {model!r} needs the codex backend, which is not wired"
                )
            async with aclosing(self.codex.complete(
                model=resolved.route, messages=messages, tools=tools
            )) as source:
                async for chunk in source:
                    yield chunk
            return
        if resolved.backend == "antigravity":
            if self.antigravity is None:
                raise ProviderError(
                    f"model {model!r} needs the antigravity backend, which is not wired"
                )
            async with aclosing(self.antigravity.complete(
                model=resolved.route, messages=messages, tools=tools
            )) as source:
                async for chunk in source:
                    yield chunk
            return
        if resolved.local is not None:
            raise ProviderError("local profiles require bounded inference through a dispatcher")
        api_key = os.environ.get(resolved.api_key_env) if resolved.api_key_env else None
        if resolved.api_base and api_key is None:
            # litellm openai/* routes refuse to run without SOME api_key, even
            # against a local api_base; local servers ignore the value. Local
            # endpoints must not require an unrelated real credential.
            api_key = "local-no-key"
        async with aclosing(_acomplete(
            model=resolved.route,
            messages=messages,
            tools=tools,
            api_base=resolved.api_base,
            api_key=api_key,
        )) as source:
            async for chunk in source:
                yield chunk
