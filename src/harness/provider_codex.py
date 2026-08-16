"""Codex on ChatGPT-subscription auth: one complete() == one `codex exec --json` child.

The transport is the headless `exec` subcommand, not `codex mcp-server`.
Live verification against codex-cli 0.147.0 showed the mcp-server path is
unusable headlessly: before running any MCP tool, codex emits a CUSTOM
`codex/event` elicitation notification -- never a standard MCP
`elicitation/create` request, whatever capabilities the client declares --
and then stops writing to stdout, waiting for an answer on an undocumented
channel. `codex exec --json` runs the same turn, with the same harness MCP
tools, and asks nothing: one JSON object per stdout line, ending in a
`turn.completed` event that carries real token usage.

Harness tools reach codex through a spawn-time dotted
`-c mcp_servers.harness.url=...` override, which merges into the config
table rather than replacing it. The url carries a trailing slash: a bare
"/mcp" path costs a per-request 307 redirect from the Starlette mount
(live-verified), and "/mcp/" skips it.

Each turn runs with `-s read-only`, a fresh empty scratch cwd, and an
isolated scratch CODEX_HOME (only auth.json carried over from the real
one, plus the one-key config.toml of _SCRATCH_CONFIG_TOML), so that user-configured MCP servers and profile settings stay out of
the conversation. The codex built-in shell is not disabled -- tool parity
here is additive, documented in the guide. The read-only sandbox blocks
writes and network but NOT reads: the shell can read arbitrary absolute
paths (live-verified on codex-cli 0.147.0, which has no read-root
confinement config), so the empty cwd is steering plus defense-in-depth,
not enforcement -- see the trust-model paragraph in the user guide. A short
orientation prefix on the prompt directs tool use through the harness:
live verification showed codex otherwise trusting that empty cwd at face
value and declaring project files missing without ever calling a harness
tool.
"""

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator, Sequence

from harness.dispatcher import current_dispatch_tool
from harness.errors import MalformedStreamError, ProviderError
from harness.mcp_serve import McpToolServer
from harness.messages import Message
from harness.provider import Chunk, StreamStop, TextDelta, Usage, UsageReport
from harness.provider_claude_code import _kill_process_group, _render_prompt, _sanitized_env
from harness.tools import ToolSpec
from harness.types import ModelId

# Prepended to every rendered prompt. Live verification showed codex
# otherwise trusting its (intentionally empty) scratch cwd at face value --
# e.g. declaring "pyproject.toml is missing" without ever calling a harness
# tool to check.
_PROMPT_PREFIX = (
    "You are running as a model backend inside an agent harness: all file, "
    "directory, and system access must go through the mcp__harness__* tools, "
    "which are the only real view of the project. Your local cwd is an "
    "intentionally empty scratch directory and proves nothing about what "
    "exists -- never conclude that a file or directory is missing without "
    "checking through the harness tools first."
)


# Seeded into every scratch CODEX_HOME: `codex exec` auto-declines every MCP
# tool call ("user cancelled MCP tool call") when the home it runs under names
# no approvals reviewer, and this one key is what makes a headless approval
# resolve affirmatively (bisected against codex-cli 0.147.0).
_SCRATCH_CONFIG_TOML = """approvals_reviewer = "auto_review"
"""


def _scratch_codex_home() -> str:
    """A fresh, isolated CODEX_HOME carrying over only auth.json from the real
    one ($CODEX_HOME if set, else ~/.codex), so a turn never picks up
    user-configured MCP servers or profile settings -- plus the one config key
    that lets headless MCP tool approvals resolve affirmatively."""
    home = tempfile.mkdtemp(prefix="harness-codex-home-")
    try:
        os.chmod(home, 0o700)
        override = os.environ.get("CODEX_HOME")
        source = Path(override) if override else Path.home() / ".codex"
        source_auth = source / "auth.json"
        if source_auth.is_file():
            shutil.copy2(source_auth, Path(home) / "auth.json")
        (Path(home) / "config.toml").write_text(_SCRATCH_CONFIG_TOML)
    except Exception:
        shutil.rmtree(home, ignore_errors=True)
        raise
    return home


def _error_text(event: dict) -> str:
    """Best-effort message out of a failure event: codex has more than one
    error shape and none of them is a documented contract."""
    err = event.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)
    if isinstance(err, str):
        return err
    message = event.get("message")
    if isinstance(message, str):
        return message
    return json.dumps(event)[:500]


class CodexProvider:
    def __init__(self, *, binary: str = "codex", timeout_s: float = 600.0) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        self._dispatch = None  # bound post-kernel-build: dispatcher.dispatch_tool

    def bind_dispatcher(self, dispatcher) -> None:
        self._dispatch = dispatcher.dispatch_tool

    async def complete(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[Chunk]:
        # A codex-backed subagent turn runs inside dispatch_model, which sets
        # this ContextVar to the dispatch_tool of whichever dispatcher is doing
        # the dispatching, for the duration of the provider call. That lets a
        # subagent turn event its tool calls into the subagent session instead
        # of the top-level one bound via bind_dispatcher below.
        dispatch = current_dispatch_tool.get() or self._dispatch
        if dispatch is None:
            raise ProviderError(
                "codex backend has no dispatcher bound; "
                "build_kernel wires this via bind_dispatcher"
            )
        server = McpToolServer(specs=tools, dispatch=dispatch)
        await server.start()
        scratch = None
        codex_home = None
        gen = None
        try:
            # A fresh, empty scratch dir for the lifetime of the turn: the codex
            # built-in shell is not disabled (see module docstring), so its cwd
            # must not be the real workspace -- the harness MCP tools are meant to
            # be the only path back to real files. Created inside this try so a
            # failure here (e.g. ENOSPC) still reaches the finally below and
            # stops the McpToolServer already started above.
            scratch = tempfile.mkdtemp(prefix="harness-codex-")
            codex_home = _scratch_codex_home()
            try:
                gen = self._run_turn(
                    model=model,
                    messages=messages,
                    url=server.url,
                    cwd=scratch,
                    codex_home=codex_home,
                )
                async for chunk in gen:
                    yield chunk
            finally:
                # Kill-then-clean, deterministically: closing gen here (rather
                # than letting an abandoned async generator fall to GC
                # finalization) guarantees the subprocess is gone before the
                # scratch dirs it is living in, and the MCP server it was
                # talking to, disappear out from under it. Nested so that if
                # gen.aclose() itself raises, the outer finally below still
                # tears down the scratch dirs and the server.
                if gen is not None:
                    await gen.aclose()
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            if codex_home is not None:
                shutil.rmtree(codex_home, ignore_errors=True)
            await server.stop()

    def _argv(self, *, model: ModelId, url: str) -> list[str]:
        argv = [
            self.binary, "exec", "--json",
            "--skip-git-repo-check",
            "-s", "read-only",
            # Dotted key: merges into the mcp_servers table instead of
            # replacing it. json.dumps writes the TOML-compatible quoted
            # string the -c parser expects. Trailing slash skips the Starlette
            # mount 307 (McpToolServer.url itself is left without one).
            "-c", "mcp_servers.harness.url=" + json.dumps(url + "/"),
        ]
        suffix = str(model).split("/", 1)[-1]
        if suffix not in ("", "default", str(model)):
            argv += ["-m", suffix]
        argv.append("-")  # read the prompt from stdin
        return argv

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
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(model=model, url=url),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group: the kill reaches the tree
                cwd=cwd,
                env=env,
            )
        except OSError as exc:
            raise ProviderError(f"codex spawn failed: {exc}") from exc

        # Prompt travels over stdin, never argv: a long transcript can overflow
        # MAX_ARG_STRLEN, and argv is visible to any local user via ps.
        assert proc.stdin is not None
        try:
            prompt = _PROMPT_PREFIX + "\n\n" + _render_prompt(messages)
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except BrokenPipeError:
            pass  # child may already be gone; the normal error paths below catch it

        assert proc.stderr is not None
        # Drain stderr concurrently: the child can otherwise fill the pipe and
        # block mid-turn on a full buffer, which would masquerade as a spurious
        # timeout while we are only reading stdout.
        stderr_task = asyncio.create_task(proc.stderr.read())
        saw_completed = False
        timed_out = False
        try:
            try:
                async with asyncio.timeout(self.timeout_s):
                    assert proc.stdout is not None
                    async for raw in proc.stdout:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # codex may interleave non-JSON noise
                        if not isinstance(event, dict):
                            continue
                        kind = event.get("type") or ""
                        if kind == "item.completed":
                            item = event.get("item") or {}
                            # mcp_tool_call and reasoning items arrive here too;
                            # only the agent message is turn output.
                            if item.get("type") == "agent_message" and item.get("text"):
                                yield TextDelta(text=item["text"])
                        elif kind == "turn.completed":
                            saw_completed = True
                            u = event.get("usage") or {}
                            yield UsageReport(
                                usage=Usage(
                                    input_tokens=u.get("input_tokens", 0),
                                    output_tokens=u.get("output_tokens", 0),
                                    cache_read_tokens=u.get("cached_input_tokens", 0),
                                    cache_write_tokens=u.get("cache_write_input_tokens", 0),
                                )
                            )
                            yield StreamStop(stop_reason="end_turn")
                        elif kind == "turn.failed" or "error" in kind:
                            raise ProviderError(f"codex turn failed: {_error_text(event)}")
                    await proc.wait()
            except TimeoutError:
                timed_out = True
        finally:
            # Runs on normal completion, on timeout, AND on GeneratorExit (the
            # consumer abandoning the stream mid-turn): the only path that
            # guarantees the child, and anything it spawned, does not leak.
            if proc.returncode is None:
                _kill_process_group(proc)
                await proc.wait()
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task

        if timed_out:
            raise ProviderError(f"codex turn timed out after {self.timeout_s}s")
        if proc.returncode not in (0, None) and not saw_completed:
            stderr = b""
            if stderr_task.done() and not stderr_task.cancelled():
                with contextlib.suppress(Exception):
                    stderr = stderr_task.result()
            tail = stderr.decode("utf-8", errors="replace")[-500:].strip() or "no stderr"
            raise ProviderError(f"codex exited {proc.returncode}: {tail}")
        if not saw_completed:
            raise MalformedStreamError("codex stream ended without a turn.completed event")
