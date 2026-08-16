"""Codex on ChatGPT-subscription auth: one complete() = one `codex mcp-server`
child driven over MCP. The harness's tools reach codex by REPLACING its
mcp_servers config table at spawn (isolates the user's codex config AND mounts
the per-turn McpToolServer in one flag). sandbox=read-only + approval-policy=
never: the harness permission engine is the only gate. Codex's built-in
read-only shell remains -- tool parity is additive, documented in the guide.
"""

import asyncio
from typing import AsyncIterator, Sequence

import anyio
import mcp.types as mcp_types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from harness.dispatcher import current_dispatch_tool
from harness.errors import ProviderError
from harness.mcp_serve import McpToolServer
from harness.messages import Message
from harness.provider import Chunk, StreamStop, TextDelta, Usage, UsageReport
from harness.provider_claude_code import _render_prompt, _sanitized_env
from harness.tools import ToolSpec
from harness.types import ModelId


class CodexProvider:
    def __init__(self, *, binary: str = "codex", timeout_s: float = 600.0) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        self._dispatch = None

    def bind_dispatcher(self, dispatcher) -> None:
        self._dispatch = dispatcher.dispatch_tool

    async def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[Chunk]:
        dispatch = current_dispatch_tool.get() or self._dispatch
        if dispatch is None:
            raise ProviderError(
                "codex backend has no dispatcher bound; "
                "build_kernel wires this via bind_dispatcher"
            )
        server = McpToolServer(specs=tools, dispatch=dispatch)
        await server.start()
        try:
            async for chunk in self._run_turn(model=model, messages=messages, url=server.url):
                yield chunk
        finally:
            await server.stop()

    def _arguments(self, *, model: ModelId, prompt: str) -> dict:
        args = {"prompt": prompt, "sandbox": "read-only", "approval-policy": "never"}
        suffix = str(model).split("/", 1)[-1]
        if suffix not in ("", "default", str(model)):
            args["model"] = suffix
        return args

    async def _run_turn(
        self, *, model: ModelId, messages: Sequence[Message], url: str
    ) -> AsyncIterator[Chunk]:
        params = StdioServerParameters(
            command=self.binary,
            args=["mcp-server", "-c", f'mcp_servers={{harness={{url="{url}"}}}}'],
            env=_sanitized_env(),
        )
        try:
            async with asyncio.timeout(self.timeout_s):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        # Call the tools/call primitive directly rather than via
                        # session.call_tool(): the high-level wrapper additionally
                        # issues a tools/list round-trip to validate structuredContent
                        # against an output schema whenever the tool isn't already
                        # cached, which real codex mcp-server answers but is outside
                        # this adapter's wire contract to depend on.
                        result = await session.send_request(
                            mcp_types.ClientRequest(
                                mcp_types.CallToolRequest(
                                    params=mcp_types.CallToolRequestParams(
                                        name="codex",
                                        arguments=self._arguments(
                                            model=model, prompt=_render_prompt(messages)
                                        ),
                                    )
                                )
                            ),
                            mcp_types.CallToolResult,
                        )
        except TimeoutError:
            raise ProviderError(f"codex turn timed out after {self.timeout_s}s") from None
        except (OSError, McpError, anyio.BrokenResourceError) as exc:
            raise ProviderError(f"codex mcp-server failed: {exc}") from exc
        text = ""
        for block in result.content or []:
            if getattr(block, "type", None) == "text":
                text += block.text
        if getattr(result, "isError", False):
            raise ProviderError(f"codex turn failed: {text or 'unknown error'}")
        if text:
            yield TextDelta(text=text)
        yield UsageReport(usage=Usage())  # flat-rate subscription; codex usage TBD via events
        yield StreamStop(stop_reason="end_turn")
