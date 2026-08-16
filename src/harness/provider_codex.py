"""Codex on ChatGPT-subscription auth: one complete() = one `codex mcp-server`
child driven over MCP. The harness's tools reach codex via the `codex` tool
call's own `config.mcp_servers` argument, not a spawn-time `-c` override:
live verification against codex-cli 0.147.0 showed `-c mcp_servers=...` is
inert for conversation MCP servers, so the per-turn McpToolServer's url
travels inside the tool call arguments instead -- but that per-call config
MERGES with the user's own ~/.codex/config.toml rather than replacing it
(live-verified: the user's own configured MCP servers started too), so each
turn also spawns with an isolated, scratch CODEX_HOME (only auth.json
carried over) to keep those out of the conversation entirely. sandbox=
read-only, approval-policy=never, and a fresh, empty per-turn scratch `cwd`:
the harness permission engine is the only gate. Codex's built-in shell is
not disabled (tool parity is additive, documented in the guide) -- pointing
its cwd at an empty scratch directory rather than the real workspace is
what keeps the harness's MCP tools the only path back to real files. A
`base-instructions` argument orients the model to that fact explicitly:
live verification showed codex trusting its (intentionally empty) scratch
cwd at face value and declaring files missing without ever trying the
harness tools. Every codex-invoked MCP tool call also arrives at the
client as a server->client MCP "elicitation/create" request -- a SECOND,
codex-side approval gate that `approval-policy="never"` does NOT cover
(that flag only governs shell commands) -- which ClientSession leaves
unanswered by default, hanging the turn forever; the harness's own
permission engine is the only gate this design allows, so the
elicitation_callback wired below auto-accepts every one. The injected
harness url also carries a trailing slash: a bare "/mcp" path triggers a
per-request 307 redirect from the Starlette mount (live-verified);
"/mcp/" skips it.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import anyio
import mcp.types as mcp_types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError

from harness.dispatcher import current_dispatch_tool
from harness.errors import ProviderError
from harness.mcp_serve import McpToolServer
from harness.messages import Message
from harness.provider import Chunk, StreamStop, TextDelta, Usage, UsageReport
from harness.provider_claude_code import _render_prompt, _sanitized_env
from harness.tools import ToolSpec
from harness.types import ModelId

# Told once per turn via the "base-instructions" tool argument: live
# verification showed codex otherwise trusting its (intentionally empty)
# scratch cwd at face value -- e.g. declaring "pyproject.toml is missing"
# without ever calling a harness tool to check.
_BASE_INSTRUCTIONS = (
    "You are running as a model backend inside an agent harness, not as an "
    "interactive coding assistant with direct workspace access. All file, "
    "directory, and system access must go through the mcp__harness__* MCP "
    "tools; they are the only real view of the user's project. Your local "
    "cwd is an intentionally empty scratch directory and proves nothing "
    "about what files exist -- never conclude a file or directory is "
    "missing without checking via the harness tools first. Your own shell "
    "is for pure computation only, not for inspecting or locating project "
    "files."
)


async def _auto_accept_elicitation(
    context: RequestContext["ClientSession", Any],
    params: mcp_types.ElicitRequestParams,
) -> mcp_types.ElicitResult:
    """Answer every server->client elicitation (codex's per-tool-call
    approval ask) with an immediate accept. The harness permission engine
    already gated this call before it ever reached codex; a second,
    codex-side approval prompt is not part of the design and, left
    unanswered by ClientSession's default (no callback wired), hangs the
    turn forever -- live-verified as the root cause of turns hanging at
    mcp_tool_call_begin. `content={}` satisfies a schema-less/no-required-
    fields form (the only kind an approval ask plausibly sends); url-mode
    elicitations carry no content per the MCP spec, so it's omitted there."""
    if getattr(params, "mode", None) == "url":
        return mcp_types.ElicitResult(action="accept")
    return mcp_types.ElicitResult(action="accept", content={})


def _scratch_codex_home() -> str:
    """A fresh, isolated CODEX_HOME with only auth.json carried over from the
    user's real one ($CODEX_HOME if set, else ~/.codex) -- so the per-call
    `config` override above doesn't merge with the user's own
    config.toml/MCP servers (live-verified: it merges, it does not
    replace)."""
    home = tempfile.mkdtemp(prefix="harness-codex-home-")
    os.chmod(home, 0o700)
    override = os.environ.get("CODEX_HOME")
    source = Path(override) if override else Path.home() / ".codex"
    source_auth = source / "auth.json"
    if source_auth.is_file():
        shutil.copy2(source_auth, Path(home) / "auth.json")
    return home


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
        # A fresh, empty scratch dir for the turn's lifecycle: codex's own
        # built-in shell is not disabled (see module docstring), so its cwd
        # must not be the real workspace -- the harness's MCP tools are meant
        # to be the only path back to real files.
        scratch = tempfile.mkdtemp(prefix="harness-codex-")
        # An isolated CODEX_HOME (see _scratch_codex_home): the per-call
        # `config` injection merges with the user's own config.toml rather
        # than replacing it, so without this their own configured MCP
        # servers start alongside the harness's.
        codex_home = _scratch_codex_home()
        try:
            async for chunk in self._run_turn(
                model=model,
                messages=messages,
                url=server.url,
                cwd=scratch,
                codex_home=codex_home,
            ):
                yield chunk
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            shutil.rmtree(codex_home, ignore_errors=True)
            await server.stop()

    def _arguments(self, *, model: ModelId, prompt: str, url: str, cwd: str) -> dict:
        args = {
            "prompt": prompt,
            "sandbox": "read-only",
            "approval-policy": "never",
            "cwd": cwd,
            "base-instructions": _BASE_INSTRUCTIONS,
            # The spawn-time `-c mcp_servers=...` override is inert for
            # conversation MCP servers (live-verified against codex-cli
            # 0.147.0); the per-call config argument is the path that works.
            # Trailing slash: a bare "/mcp" path hits a per-request 307
            # redirect from the Starlette mount (live-verified); "/mcp/"
            # skips it. McpToolServer.url itself is left without one.
            "config": {"mcp_servers": {"harness": {"url": f"{url}/"}}},
        }
        suffix = str(model).split("/", 1)[-1]
        if suffix not in ("", "default", str(model)):
            args["model"] = suffix
        return args

    async def _run_turn(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        url: str,
        cwd: str,
        codex_home: str,
    ) -> AsyncIterator[Chunk]:
        env = _sanitized_env()
        env["CODEX_HOME"] = codex_home
        params = StdioServerParameters(
            command=self.binary,
            args=["mcp-server"],
            env=env,
        )
        try:
            async with asyncio.timeout(self.timeout_s):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(
                        read, write, elicitation_callback=_auto_accept_elicitation
                    ) as session:
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
                                            model=model,
                                            prompt=_render_prompt(messages),
                                            url=url,
                                            cwd=cwd,
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
