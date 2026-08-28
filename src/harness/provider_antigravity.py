"""Gemini on Google-account subscription auth: one complete() == one `agy`
headless print-mode turn.

The Antigravity CLI (`agy`) is Google's successor to gemini-cli, which was
server-side-killed for individual accounts. `agy` authenticates via Google
OAuth (subscription creds, no per-token billing -- the binding constraint)
and exposes Gemini's flash/pro tiers under the Antigravity brand.

Wire facts below were live-probed 2026-08-28 against agy 1.1.22, including one
full round-trip turn through a real McpToolServer in Task 1's own live
verification (see the task report for the transcript).

Transport is `agy`'s headless print mode (`-p`), not a persistent session:
`--output-format stream-json` emits one JSON object per stdout line, each
shaped `{"event": "<kind>", "<kind>": {...payload...}}` -- the discriminator
value names its own nested payload key (e.g. `{"event": "step_update",
"step_update": {"step_type": "agent_response", "text_delta": "..."}}`); there
is no flat top-level "type" field the way codex/claude-code have one. Harness
tools reach agy through a per-turn `McpToolServer`, registered via a separate
`agy mcp add -t http harness <url>/` subprocess before the turn runs (agy has
no dotted-config-override flag the way codex does; the CLI is the only stable
registration surface, and it writes only into the scratch HOME below -- the
real ~/.gemini is never touched). `--dangerously-skip-permissions` replaces
the config-seeding codex needs for headless MCP tool approval: it is a flag,
not a config key.

agy has no config-dir override env var, so the whole child HOME is scratch
for the turn's lifetime: a fresh 0700 tempdir carrying over COPIES of exactly
the auth files agy needs (`.gemini/{oauth_creds.json, google_accounts.json,
installation_id, settings.json, projects.json, state.json}` and
`.gemini/antigravity-cli/{antigravity-oauth-token, installation_id,
settings.json}` -- the oauth token is the operative credential). Missing
source files are skipped silently; agy recreates what it needs. The harness
never reads, stores, or transmits credential contents beyond this mechanical
per-turn copy, removed in finally -- the same ruling as codex's auth.json.

Prompt delivery departs from the codex/claude-code idiom in form only: the
rendered transcript still goes over stdin (never argv -- MAX_ARG_STRLEN and
`ps` visibility apply here too), but `-p` (agy's required flag to enter print
mode at all -- an empty value is rejected outright with "Error: empty
prompt", live-verified) carries a short orientation string rather than being
omitted or given `-`. `-p`'s value is appended DIRECTLY after stdin with no
separator agy inserts itself, confirmed by Task 1's live verification --
which also caught a real bug the first time through: `_render_prompt`'s
trailing "[assistant]:" completion cue (written for codex/claude's raw
completion-style CLIs) glued straight onto the -p text made agy read the
orientation paragraph as an assistant turn already in progress, and it
replied to THAT instead of the user's request. `_render_stdin_prompt` below
strips the cue before it reaches agy's stdin; nothing else about the
codex/claude idiom needed to change, and the stdin-vs-argv question itself
resolved in stdin's favor -- no fallback needed.

Trust model (the codex lesson, applied from day one): agy's ~57 built-in
tools (run_command, write_to_file, browser_*, view_file, ...) are NOT
disabled here -- tool parity with the harness's own tools is additive, exactly
like codex's shell. The scratch cwd and scratch HOME steer; they do not
confine. `--sandbox` and `--mode plan` exist but their interaction with MCP
tool calls is unprobed -- v1 ships without them. Unlike codex's read-only
sandbox, agy's built-ins can potentially WRITE outside the scratch dirs, not
just read. A `--sandbox`/`--mode plan` investigation is a named fast-follow;
see the user guide for the full trust-model paragraph.

The provider emits NO ThinkingDelta in v1: agy exposes thinking token COUNTS
but never thought text, and an empty ThinkingDelta would render a useless
"(thought for Ns * 0 chars)" line in the TUI. Thought text must never enter
loop history or assembled message text.
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
from harness.provider_claude_code import _kill_process_group, _render_prompt
from harness.provider_claude_code import _SECRET_ENV_KEYS as _CLI_SECRET_ENV_KEYS
from harness.tools import ToolSpec
from harness.types import ModelId

# Prepended (as the -p argument value, NOT stdin) to every turn. Mirrors the
# codex/claude-code orientation prefix: without it, live verification of the
# codex backend showed a CLI otherwise trusting its (intentionally empty)
# scratch cwd at face value -- e.g. declaring a file missing without ever
# calling a harness tool to check. agy appends -p's value DIRECTLY after
# whatever is on stdin (live-verified), so this text is the LAST thing agy
# reads before generating -- it must say "preceding", never "follows": an
# earlier draft claiming "the conversation transcript follows" was live-
# verified to make agy wait for a transcript that was already behind it
# (already on stdin) rather than act on it, replying "please provide the
# details of your task" to a turn that had already stated one.
_PROMPT_PREFIX = (
    "The preceding message is the actual request -- address it now. All "
    "file, directory, and system access must go through the tools exposed "
    "by the 'harness' MCP server, which are the only real view of the "
    "project. Your local cwd is an intentionally empty scratch directory "
    "and proves nothing about what exists -- never conclude that a file or "
    "directory is missing without checking through the harness tools first."
)

# Model-provider secrets the child never needs, extended with the Google
# Generative AI SDK's own API-key env var: subscription auth lives inside
# agy's OAuth token, not in env vars, and GOOGLE_API_KEY could otherwise let
# a Google-SDK-based tool inside agy silently talk to a different account.
_SECRET_ENV_KEYS = (
    _CLI_SECRET_ENV_KEYS
    if "GOOGLE_API_KEY" in _CLI_SECRET_ENV_KEYS
    else _CLI_SECRET_ENV_KEYS + ("GOOGLE_API_KEY",)
)

# _render_prompt's trailing "[assistant]:" cue exists for codex/claude's raw
# completion-style CLIs, where it tells the model "your turn starts here".
# Live verification showed it breaks agy: -p's value is appended DIRECTLY
# after whatever is on stdin, with no separator agy inserts itself, so a
# stdin ending "...[assistant]:" glued straight onto the -p orientation text
# reads as an assistant turn that already started mid-orientation-paragraph
# -- agy replied to THAT instead of the user's actual request. agy's
# turn-taking is native (it is a real chat CLI, not a completion endpoint),
# so the cue is not merely unneeded here, it is actively wrong; strip it.
_ASSISTANT_CUE_SUFFIX = "\n[assistant]:"


def _render_stdin_prompt(messages: Sequence[Message]) -> str:
    rendered = _render_prompt(messages)
    if rendered.endswith(_ASSISTANT_CUE_SUFFIX):
        rendered = rendered[: -len(_ASSISTANT_CUE_SUFFIX)]
    return rendered

# Exactly the files a per-turn scratch HOME needs, copied (never moved) out of
# the real ~/.gemini so a turn never picks up unrelated user state and the
# real credential files are never touched. Keyed by path relative to HOME.
# Missing source files are skipped silently -- agy recreates what it needs.
_AUTH_FILES: dict[str, tuple[str, ...]] = {
    ".gemini": (
        "oauth_creds.json",
        "google_accounts.json",
        "installation_id",
        "settings.json",
        "projects.json",
        "state.json",
    ),
    ".gemini/antigravity-cli": (
        "antigravity-oauth-token",
        "installation_id",
        "settings.json",
    ),
}

# Registering the harness MCP server (`agy mcp add`) is a short, separate
# subprocess ahead of the turn -- not expected to ever approach agy's own
# 5-minute print-mode default, so a much shorter local guard is enough to
# turn a hang here into a loud error instead of a stuck generator.
_MCP_ADD_TIMEOUT_S = 30.0


def _sanitized_env(home: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in _SECRET_ENV_KEYS:
        env.pop(key, None)
    env["HOME"] = home
    return env


def _scratch_home() -> str:
    """A fresh, isolated HOME carrying over copies of exactly the auth files
    agy needs. agy has no config-dir override env var (unlike codex's
    CODEX_HOME), so the whole HOME is scratch for the turn's lifetime."""
    home = tempfile.mkdtemp(prefix="harness-antigravity-home-")
    try:
        os.chmod(home, 0o700)
        source = Path.home()
        for rel_dir, filenames in _AUTH_FILES.items():
            src_dir = source / rel_dir
            for filename in filenames:
                src_file = src_dir / filename
                if src_file.is_file():
                    dest_file = Path(home) / rel_dir / filename
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest_file)
    except Exception:
        shutil.rmtree(home, ignore_errors=True)
        raise
    return home


class AntigravityProvider:
    def __init__(self, *, binary: str = "agy", timeout_s: float = 600.0) -> None:
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
        # An antigravity-backed subagent turn runs inside dispatch_model, which
        # sets this ContextVar to the dispatch_tool of whichever dispatcher is
        # doing the dispatching, for the duration of the provider call. That
        # lets a subagent turn event its tool calls into the subagent session
        # instead of the top-level one bound via bind_dispatcher below.
        dispatch = current_dispatch_tool.get() or self._dispatch
        if dispatch is None:
            raise ProviderError(
                "antigravity backend has no dispatcher bound; "
                "build_kernel wires this via bind_dispatcher"
            )
        server = McpToolServer(specs=tools, dispatch=dispatch)
        await server.start()
        scratch_home = None
        scratch_cwd = None
        gen = None
        try:
            # Created inside this try so a failure here (e.g. ENOSPC) still
            # reaches the finally below and stops the McpToolServer already
            # started above.
            scratch_home = _scratch_home()
            scratch_cwd = tempfile.mkdtemp(prefix="harness-antigravity-cwd-")
            try:
                gen = self._run_turn(
                    model=model,
                    messages=messages,
                    url=server.url,
                    cwd=scratch_cwd,
                    home=scratch_home,
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
            if scratch_cwd is not None:
                shutil.rmtree(scratch_cwd, ignore_errors=True)
            if scratch_home is not None:
                shutil.rmtree(scratch_home, ignore_errors=True)
            await server.stop()

    def _argv(self, *, model: ModelId) -> list[str]:
        argv = [
            self.binary,
            "-p", _PROMPT_PREFIX,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            # Go duration syntax (agy's own default is "5m0s"); whole seconds
            # is always valid regardless of magnitude.
            "--print-timeout", f"{int(self.timeout_s)}s",
        ]
        suffix = str(model).split("/", 1)[-1]
        if suffix not in ("", "default", str(model)):
            argv += ["--model", suffix]
        return argv

    async def _register_mcp(self, *, url: str, env: dict[str, str]) -> None:
        """`agy mcp add -t http harness <url>/`, run in the scratch HOME env
        so it writes only scratch config -- the real ~/.gemini is never
        touched. NEVER a blocking subprocess.run: this runs while the
        McpToolServer above is already live in this same process, and a
        blocking call here would self-deadlock the event loop it depends on.
        """
        argv = [self.binary, "mcp", "add", "-t", "http", "harness", url + "/"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise ProviderError(f"antigravity mcp registration spawn failed: {exc}") from exc
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_MCP_ADD_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise ProviderError("antigravity mcp registration timed out")
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace")[-500:].strip() or "no stderr"
            raise ProviderError(f"antigravity mcp add exited {proc.returncode}: {tail}")

    async def _run_turn(
        self,
        *,
        model: ModelId,
        messages: Sequence[Message],
        url: str,
        cwd: str,
        home: str,
    ) -> AsyncIterator[Chunk]:
        env = _sanitized_env(home)
        await self._register_mcp(url=url, env=env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(model=model),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group: the kill reaches the tree
                cwd=cwd,
                env=env,
            )
        except OSError as exc:
            raise ProviderError(f"antigravity spawn failed: {exc}") from exc

        # Prompt travels over stdin, never argv: a long transcript can overflow
        # MAX_ARG_STRLEN, and argv is visible to any local user via ps. -p
        # above carries only the short orientation prefix.
        assert proc.stdin is not None
        try:
            proc.stdin.write(_render_stdin_prompt(messages).encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except BrokenPipeError:
            pass  # child may already be gone; the normal error paths below catch it

        assert proc.stderr is not None
        # Drain stderr concurrently: the child can otherwise fill the pipe and
        # block mid-turn on a full buffer, which would masquerade as a
        # spurious timeout while we are only reading stdout.
        stderr_task = asyncio.create_task(proc.stderr.read())
        saw_result = False
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
                            continue  # agy may interleave non-JSON noise
                        if not isinstance(event, dict):
                            continue
                        # agy's envelope is {"event": "<kind>", "<kind>": {...}}:
                        # the discriminator names its own payload key, live-
                        # verified against agy 1.1.22 -- there is no top-level
                        # "type" field the way codex/claude-code have one.
                        kind = event.get("event") or ""
                        if kind == "step_update":
                            step = event.get("step_update") or {}
                            # Pure-thinking and tool steps carry no text_delta
                            # (tool calls themselves arrive via the MCP server
                            # -> dispatcher, not this stream) and yield nothing.
                            if step.get("step_type") == "agent_response":
                                text = step.get("text_delta")
                                if text:
                                    yield TextDelta(text=text)
                        elif kind == "result":
                            result = event.get("result") or {}
                            status = result.get("status")
                            if status == "SUCCESS":
                                saw_result = True
                                u = result.get("usage") or {}
                                yield UsageReport(
                                    usage=Usage(
                                        input_tokens=u.get("input_tokens", 0),
                                        output_tokens=u.get("output_tokens", 0),
                                    )
                                )
                                yield StreamStop(stop_reason="end_turn")
                            elif status == "ERROR":
                                raise ProviderError(f"antigravity: {result.get('error')}")
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
            raise ProviderError(f"antigravity turn timed out after {self.timeout_s}s")
        if proc.returncode not in (0, None) and not saw_result:
            stderr = b""
            if stderr_task.done() and not stderr_task.cancelled():
                with contextlib.suppress(Exception):
                    stderr = stderr_task.result()
            tail = stderr.decode("utf-8", errors="replace")[-500:].strip() or "no stderr"
            raise ProviderError(f"antigravity exited {proc.returncode}: {tail}")
        if not saw_result:
            raise MalformedStreamError("antigravity stream ended without a result event")
