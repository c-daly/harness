"""Claude on subscription auth: spawn headless `claude -p` per turn.

The harness never touches claude.ai credentials — it invokes the user's own
installed, logged-in Claude Code binary (the documented headless mode). Tools
are served to CC over McpToolServer; CC's built-ins and user-scope settings
are disabled so the harness's registry is the only tool surface. One
complete() call == one CC agent turn (stateless v1: full history re-rendered;
--resume is a follow-up)."""

import asyncio
import contextlib
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import AsyncIterator, Sequence

from harness.errors import MalformedStreamError, ProviderError
from harness.mcp_serve import McpToolServer
from harness.messages import Message
from harness.provider import Chunk, StreamStop, TextDelta, Usage, UsageReport
from harness.tools import ToolSpec
from harness.types import ModelId

# Built-ins captured from `claude mcp serve` / stream-json init on CC v2.1.233.
# CC accepts one comma-joined argument. Keep sorted for diff stability.
DISALLOWED_BUILTINS = ",".join(
    sorted(
        [
            "Task", "Bash", "Edit", "Read", "Write", "NotebookEdit", "WebFetch",
            "WebSearch", "Skill", "TaskOutput", "TaskStop", "EnterWorktree",
            "ExitWorktree", "SendMessage", "ListAgents", "Workflow", "CronCreate",
            "CronDelete", "CronList", "ScheduleWakeup", "RemoteTrigger", "Monitor",
            "PushNotification", "DesignSync", "ReportFindings", "ToolSearch",
        ]
    )
)


def _render_prompt(messages: Sequence[Message]) -> str:
    """Stateless transcript render. Only text blocks carry over (a CC-backed
    turn does its tool work inside CC; other models' tool records are elided)."""
    lines = []
    for m in messages:
        text = m.text()
        if text:
            lines.append(f"[{m.role.value}]: {text}")
    lines.append("[assistant]:")
    return "\n".join(lines)


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Best-effort SIGKILL to the whole process group. proc was spawned with
    start_new_session=True, so its pgid equals its own pid; this reaches any
    subprocesses CC itself may have spawned, not just the claude binary."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        proc.kill()


class ClaudeCodeProvider:
    def __init__(self, *, binary: str = "claude", timeout_s: float = 600.0) -> None:
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
        if self._dispatch is None:
            raise ProviderError(
                "claude-code backend has no dispatcher bound; "
                "build_kernel wires this via bind_dispatcher"
            )
        server = McpToolServer(specs=tools, dispatch=self._dispatch)
        await server.start()
        try:
            with tempfile.TemporaryDirectory(prefix="harness-cc-") as tmp:
                cfg = Path(tmp) / "mcp.json"
                cfg.write_text(
                    json.dumps({"mcpServers": {"harness": {"type": "http", "url": server.url}}})
                )
                async for chunk in self._run_turn(model=model, messages=messages, cfg=cfg):
                    yield chunk
        finally:
            await server.stop()

    def _argv(self, *, model: ModelId, prompt: str, cfg: Path) -> list[str]:
        argv = [
            self.binary, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", str(cfg),
            "--disallowedTools", DISALLOWED_BUILTINS,
            "--allowedTools", "mcp__harness",
            "--no-session-persistence",
        ]
        suffix = str(model).split("/", 1)[-1]
        if suffix not in ("", "default", str(model)):
            argv += ["--model", suffix]
        return argv

    async def _run_turn(
        self, *, model: ModelId, messages: Sequence[Message], cfg: Path
    ) -> AsyncIterator[Chunk]:
        proc = await asyncio.create_subprocess_exec(
            *self._argv(model=model, prompt=_render_prompt(messages), cfg=cfg),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # own process group: timeout/abandonment kills the whole tree
        )
        assert proc.stderr is not None
        # Drain stderr concurrently: with --verbose the child can fill the pipe
        # and block mid-turn on a full buffer, which would otherwise masquerade
        # as a spurious timeout while we are only reading stdout.
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
                            continue  # CC may interleave non-JSON noise
                        if event.get("type") == "assistant":
                            for block in event.get("message", {}).get("content", []):
                                if block.get("type") == "text" and block.get("text"):
                                    yield TextDelta(text=block["text"])
                        elif event.get("type") == "result":
                            saw_result = True
                            if event.get("is_error"):
                                raise ProviderError(
                                    f"claude-code turn failed: "
                                    f"{event.get('result') or event.get('subtype')}"
                                )
                            u = event.get("usage") or {}
                            yield UsageReport(
                                usage=Usage(
                                    input_tokens=u.get("input_tokens", 0),
                                    output_tokens=u.get("output_tokens", 0),
                                    cache_read_tokens=u.get("cache_read_input_tokens", 0),
                                    cache_write_tokens=u.get("cache_creation_input_tokens", 0),
                                )
                            )
                            yield StreamStop(stop_reason=event.get("stop_reason") or "end_turn")
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
            raise ProviderError(f"claude-code turn timed out after {self.timeout_s}s")
        if proc.returncode not in (0, None) and not saw_result:
            stderr = b""
            if stderr_task.done() and not stderr_task.cancelled():
                with contextlib.suppress(Exception):
                    stderr = stderr_task.result()
            raise ProviderError(
                f"claude-code exited {proc.returncode}: "
                f"{stderr.decode('utf-8', errors='replace')[-500:].strip() or 'no stderr'}"
            )
        if not saw_result:
            raise MalformedStreamError("claude-code stream ended without a result event")
