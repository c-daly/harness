# Subscription-CLI Model Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude becomes a first-class harness model on Claude Code Max subscription auth: a `claude-code` catalog backend spawns headless `claude -p`, with the harness's own tools served to it over an in-process MCP HTTP server that routes every call through the existing dispatcher.

**Architecture:** `CatalogProvider` multiplexes on a new `backend` field per catalog entry — absent means the existing LiteLLM path, `"claude-code"` selects `ClaudeCodeProvider`. That provider spawns one `claude -p --output-format stream-json` subprocess per `complete()` call, isolated from user settings, with built-in tools disabled and a per-turn `McpToolServer` (streamable-HTTP, in-process, ephemeral port) serving exactly the turn's `ToolSpec`s; tool calls arrive over MCP and go through `Dispatcher.dispatch_tool`, so permissions and the event log behave identically to native tool calls.

**Tech Stack:** Python 3.12, asyncio, `mcp>=1.24` (already a dependency; ships starlette+uvicorn), pytest with `asyncio_mode = "auto"`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-15-subscription-cli-model-backends-design.md`

## Global Constraints

- The harness never implements claude.ai OAuth and never sends subscription tokens to any API itself; it only spawns the user's locally installed, self-authenticated `claude` binary (spec constraint 2).
- Adapter invocations must disable the CLI's built-in tools and user-scope settings: `--setting-sources "" --strict-mcp-config --disallowedTools <built-ins>` (spec constraint 3).
- `requires-python = ">=3.12"`, ruff line-length 100, tests under `tests/`, pytest asyncio mode auto (no `@pytest.mark.asyncio` needed).
- v1 is **stateless per turn** (full history re-rendered each `complete()`); CC session `--resume` is a follow-up optimization. Rationale: `ModelProvider.complete()` carries no session identity, and stateless is correct for concurrent subagent turns.
- v1 wires the claude backend for the **main loop only** (bind point in `build_kernel`); claude-backed *subagents* are a documented follow-up.

**Captured ground truth (CC v2.1.233, live 2026-08-15)** — the stream-json contract the parser and fixtures pin:

```
{"type":"system","subtype":"init","cwd":"...","session_id":"01400b44-...","tools":[...],...}
{"type":"assistant","message":{"model":"claude-opus-5","content":[{"type":"text","text":"pong"}],"usage":{...}},"session_id":"..."}
{"type":"result","subtype":"success","is_error":false,"num_turns":1,"stop_reason":"end_turn","result":"pong","session_id":"...","total_cost_usd":0.075,"usage":{"input_tokens":2,"cache_creation_input_tokens":6646,"cache_read_input_tokens":16241,"output_tokens":4,...}}
```

(Field order varies; `"type":"result"` is not necessarily first key. `--verbose` is required for stream-json output with `-p`.)

---

### Task 1: Catalog `backend` field

**Files:**
- Modify: `src/harness/catalog.py`
- Test: `tests/test_catalog_backend.py` (create)

**Interfaces:**
- Consumes: existing `Catalog.resolve(alias) -> ResolvedModel`, `UnknownAliasError`.
- Produces: `ResolvedModel.backend: str | None` (new field, default `None`); `KNOWN_BACKENDS: frozenset[str] = frozenset({"claude-code"})`; `UnknownBackendError(Exception)` raised by `resolve()` for any other non-None value. Task 4 reads `resolved.backend`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_catalog_backend.py
"""Catalog `backend` field: selects a non-LiteLLM provider implementation."""

import pytest

from harness.catalog import Catalog, UnknownBackendError


def _catalog(tmp_path, body: str) -> Catalog:
    p = tmp_path / "models.toml"
    p.write_text(body)
    return Catalog.load(p)


def test_backend_defaults_to_none(tmp_path):
    cat = _catalog(tmp_path, '[models.gpt]\nroute = "openai/gpt-5"\n')
    assert cat.resolve("gpt").backend is None


def test_backend_claude_code_resolves(tmp_path):
    cat = _catalog(
        tmp_path,
        '[models.claude]\nbackend = "claude-code"\nroute = "claude-code/default"\n'
        "input_cost_per_token = 0.0\noutput_cost_per_token = 0.0\n",
    )
    resolved = cat.resolve("claude")
    assert resolved.backend == "claude-code"
    assert resolved.route == "claude-code/default"


def test_unknown_backend_errors_at_resolve(tmp_path):
    cat = _catalog(tmp_path, '[models.x]\nbackend = "frobnicator"\nroute = "x/y"\n')
    with pytest.raises(UnknownBackendError, match="frobnicator"):
        cat.resolve("x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_catalog_backend.py -v`
Expected: FAIL — `ImportError: cannot import name 'UnknownBackendError'`

- [ ] **Step 3: Implement in `src/harness/catalog.py`**

Add after `UnknownAliasError`:

```python
class UnknownBackendError(Exception):
    pass


KNOWN_BACKENDS: frozenset[str] = frozenset({"claude-code"})
```

Add to `ResolvedModel` (after `route: ModelId`):

```python
    backend: str | None
```

In `Catalog.resolve`, after `route = entry["route"]`:

```python
        backend = entry.get("backend")
        if backend is not None and backend not in KNOWN_BACKENDS:
            raise UnknownBackendError(
                f"model {alias!r}: unknown backend {backend!r}; known: {sorted(KNOWN_BACKENDS)}"
            )
```

and pass `backend=backend,` in the `ResolvedModel(...)` constructor call (after `route=`).

- [ ] **Step 4: Run the new tests and the full suite**

Run: `.venv/bin/python -m pytest tests/test_catalog_backend.py -v && .venv/bin/python -m pytest tests/ -q`
Expected: new tests PASS; full suite green. If any existing test constructs `ResolvedModel(...)` positionally, fix that call site by inserting `backend=None` (keyword construction is unaffected because the field has no default — check with `grep -rn "ResolvedModel(" tests/ src/`).

- [ ] **Step 5: Commit**

```bash
git add src/harness/catalog.py tests/test_catalog_backend.py
git commit -m "feat(catalog): backend field selects provider implementation, validated at resolve"
```

---

### Task 2: `McpToolServer` — the harness registry served over MCP

**Files:**
- Create: `src/harness/mcp_serve.py`
- Test: `tests/test_mcp_serve.py` (create)

**Interfaces:**
- Consumes: `ToolSpec` (`tools.py`), `ProposedToolCall` (`hooks.py`), `ToolOutcome` (`dispatcher.py`), `new_call_id` (`types.py`).
- Produces (Task 3 relies on these exact names):
  - `class McpToolServer` with `__init__(self, *, specs: Sequence[ToolSpec], dispatch: Callable[[ProposedToolCall], Awaitable[ToolOutcome]])`
  - `async def start(self) -> None` (binds an ephemeral 127.0.0.1 port)
  - `url: str` property (e.g. `"http://127.0.0.1:49213/mcp"`), valid after `start()`
  - `async def stop(self) -> None`

The server dispatches through a *callable*, not a Dispatcher instance, so tests inject fakes and Task 4 passes the real `dispatcher.dispatch_tool` bound method.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_serve.py
"""McpToolServer: harness ToolSpecs served over streamable-HTTP MCP; every
call routes through the injected dispatch callable (the dispatcher seam)."""

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from harness.dispatcher import ToolOutcome
from harness.mcp_serve import McpToolServer
from harness.tools import ToolSpec
from harness.types import ToolName

ECHO = ToolSpec(
    name=ToolName("echo"),
    description="Echo the text back.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
)


def _dispatch_recorder(outcome: ToolOutcome):
    calls: list = []

    async def dispatch(call):
        calls.append(call)
        return outcome

    return dispatch, calls


async def test_lists_given_specs_only():
    dispatch, _ = _dispatch_recorder(ToolOutcome(text="", blob=None, is_error=False))
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        assert names == ["echo"]
        assert tools.tools[0].description == "Echo the text back."
        assert tools.tools[0].inputSchema["properties"]["text"]["type"] == "string"
    finally:
        await server.stop()


async def test_call_routes_through_dispatch_and_returns_text():
    dispatch, calls = _dispatch_recorder(ToolOutcome(text="echoed: hi", blob=None, is_error=False))
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("echo", {"text": "hi"})
        assert result.isError is False
        assert result.content[0].text == "echoed: hi"
        assert len(calls) == 1
        assert str(calls[0].tool) == "echo"
        assert dict(calls[0].args) == {"text": "hi"}
        assert calls[0].call_id  # dispatcher requires a call id
    finally:
        await server.stop()


async def test_denied_outcome_surfaces_as_mcp_error():
    dispatch, _ = _dispatch_recorder(
        ToolOutcome(text="denied by permission engine", blob=None, is_error=True)
    )
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("echo", {"text": "hi"})
        assert result.isError is True
        assert "denied by permission engine" in result.content[0].text
    finally:
        await server.stop()


async def test_unknown_tool_is_error_without_dispatch():
    dispatch, calls = _dispatch_recorder(ToolOutcome(text="", blob=None, is_error=False))
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("not_a_tool", {})
        assert result.isError is True
        assert calls == []  # never reached the dispatcher
    finally:
        await server.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_serve.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.mcp_serve'`

- [ ] **Step 3: Implement `src/harness/mcp_serve.py`**

```python
"""Serve the harness tool surface over MCP (streamable HTTP, in-process).

The inverse of mcp_host: external MCP clients (a headless `claude -p` turn)
see exactly the ToolSpecs they were given, and every tools/call routes through
the injected dispatch callable — the dispatcher seam — so permissions, hooks,
and the event log apply identically to native and MCP-arriving tool calls.
HTTP (not stdio) because stdio MCP servers are spawned as the CLIENT's child
process, which would not share this process's registry and dispatcher.
"""

import asyncio
import contextlib
from typing import Any, Awaitable, Callable, Sequence

import mcp.types as mcp_types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from harness.dispatcher import ToolOutcome
from harness.hooks import ProposedToolCall
from harness.tools import ToolSpec
from harness.types import ToolName, new_call_id

Dispatch = Callable[[ProposedToolCall], Awaitable[ToolOutcome]]


class McpToolServer:
    def __init__(self, *, specs: Sequence[ToolSpec], dispatch: Dispatch) -> None:
        self._specs = tuple(specs)
        self._by_name = {str(s.name): s for s in self._specs}
        self._dispatch = dispatch
        self._uvicorn: uvicorn.Server | None = None
        self._serve_task: asyncio.Task | None = None
        self._port: int | None = None

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("McpToolServer not started")
        return f"http://127.0.0.1:{self._port}/mcp"

    def _build_app(self) -> Starlette:
        server: Server = Server("harness")

        @server.list_tools()
        async def _list_tools() -> list[mcp_types.Tool]:
            return [
                mcp_types.Tool(
                    name=str(s.name), description=s.description, inputSchema=s.parameters
                )
                for s in self._specs
            ]

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict[str, Any] | None):
            if name not in self._by_name:
                raise ValueError(f"unknown tool {name!r}")
            outcome = await self._dispatch(
                ProposedToolCall(
                    call_id=new_call_id(), tool=ToolName(name), args=arguments or {}
                )
            )
            if outcome.is_error:
                raise ValueError(outcome.text)
            return [mcp_types.TextContent(type="text", text=outcome.text)]

        manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        return Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)

    async def start(self) -> None:
        config = uvicorn.Config(
            self._build_app(), host="127.0.0.1", port=0, log_level="error", lifespan="on"
        )
        self._uvicorn = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(self._uvicorn.serve())
        while not self._uvicorn.started:
            if self._serve_task.done():
                self._serve_task.result()  # raise the startup failure
            await asyncio.sleep(0.01)
        self._port = self._uvicorn.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._serve_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._serve_task
        self._uvicorn = None
        self._serve_task = None
        self._port = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_serve.py -v`
Expected: PASS (all 4). Two known flex points if the installed `mcp` version differs from the code above — fix in the implementation, not the tests: (a) the lowlevel `call_tool` decorator may pass exceptions through as `isError` results only when raised as `ValueError`/`McpError` — if `result.isError` is False on the denied test, consult `mcp.server.lowlevel` for the error convention and adapt; (b) `StreamableHTTPSessionManager` kwargs (`json_response`, `stateless`) — drop any kwarg the installed version rejects.

- [ ] **Step 5: Commit**

```bash
git add src/harness/mcp_serve.py tests/test_mcp_serve.py
git commit -m "feat: McpToolServer — harness tool surface over streamable-HTTP MCP, dispatching through the dispatcher seam"
```

---

### Task 3: `ClaudeCodeProvider`

**Files:**
- Create: `src/harness/provider_claude_code.py`
- Test: `tests/test_provider_claude_code.py` (create)

**Interfaces:**
- Consumes: `McpToolServer` (Task 2), `Chunk`/`TextDelta`/`UsageReport`/`StreamStop`/`Usage` (`provider.py`), `Message`/`Role` (`messages.py`), `ProviderError`/`MalformedStreamError`/`AuthFailed` (`errors.py`), `ToolSpec`, `Dispatch` type from `mcp_serve`.
- Produces (Task 4 relies on these exact names):
  - `class ClaudeCodeProvider` with `__init__(self, *, binary: str = "claude", timeout_s: float = 600.0)`
  - `def bind_dispatcher(self, dispatcher) -> None` (stores `dispatcher.dispatch_tool`)
  - `async def complete(self, *, model, messages, tools=()) -> AsyncIterator[Chunk]` (the `ModelProvider` protocol; `model` is the catalog **route**, e.g. `claude-code/default` — suffix after `/` other than `default` becomes `--model <suffix>`)
  - module constant `DISALLOWED_BUILTINS: str` (comma-joined)

- [ ] **Step 1: Write the failing tests (fake `claude` executables — no subscription use in CI)**

```python
# tests/test_provider_claude_code.py
"""ClaudeCodeProvider against fake `claude` executables emitting captured
stream-json shapes (CC v2.1.233). No real CLI, no subscription use."""

import json
import os
import stat

import pytest

from harness.dispatcher import ToolOutcome
from harness.errors import ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import StreamStop, TextDelta, UsageReport, collect
from harness.provider_claude_code import DISALLOWED_BUILTINS, ClaudeCodeProvider
from harness.types import ModelId


def _fake_claude(tmp_path, script_body: str) -> str:
    path = tmp_path / "claude"
    path.write_text("#!/usr/bin/env python3\n" + script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


HAPPY = r"""
import json, sys
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1", "tools": []}))
print(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5",
    "content": [{"type": "text", "text": "pong"}],
    "usage": {"input_tokens": 2, "output_tokens": 1}}, "session_id": "s-1"}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
    "num_turns": 1, "stop_reason": "end_turn", "result": "pong", "session_id": "s-1",
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 6646,
              "cache_read_input_tokens": 16241, "output_tokens": 4}}))
# also record argv so tests can assert the flag contract
open(sys.argv[0] + ".argv", "w").write(json.dumps(sys.argv[1:]))
"""

ERROR_RESULT = r"""
import json
print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
    "result": "Not logged in", "session_id": "s-2", "num_turns": 0}))
"""

CRASH = r"""
import sys
sys.stderr.write("boom: something broke\n")
sys.exit(2)
"""

USER = [Message(role=Role.USER, blocks=(TextBlock(text="say pong"),))]


class _Dispatcher:
    async def dispatch_tool(self, call):  # never called in these tests
        return ToolOutcome(text="", blob=None, is_error=False)


def _provider(binary: str, timeout_s: float = 30.0) -> ClaudeCodeProvider:
    p = ClaudeCodeProvider(binary=binary, timeout_s=timeout_s)
    p.bind_dispatcher(_Dispatcher())
    return p


async def test_happy_turn_maps_chunks(tmp_path):
    binary = _fake_claude(tmp_path, HAPPY)
    provider = _provider(binary)
    message, usage, stop = await collect(
        provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
    )
    assert message.text() == "pong"
    assert stop == "end_turn"
    assert usage.input_tokens == 2
    assert usage.output_tokens == 4
    assert usage.cache_read_tokens == 16241
    assert usage.cache_write_tokens == 6646


async def test_flag_contract(tmp_path):
    binary = _fake_claude(tmp_path, HAPPY)
    provider = _provider(binary)
    await collect(provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=()))
    argv = json.loads(open(binary + ".argv").read())
    assert "-p" in argv
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" in argv
    assert "--disallowedTools" in argv
    assert argv[argv.index("--disallowedTools") + 1] == DISALLOWED_BUILTINS
    assert "--model" not in argv  # "default" suffix means no override


async def test_model_suffix_becomes_override(tmp_path):
    binary = _fake_claude(tmp_path, HAPPY)
    provider = _provider(binary)
    await collect(provider.complete(model=ModelId("claude-code/opus"), messages=USER, tools=()))
    argv = json.loads(open(binary + ".argv").read())
    assert "--model" in argv and argv[argv.index("--model") + 1] == "opus"


async def test_error_result_raises_provider_error(tmp_path):
    binary = _fake_claude(tmp_path, ERROR_RESULT)
    provider = _provider(binary)
    with pytest.raises(ProviderError, match="Not logged in"):
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )


async def test_crash_raises_provider_error_with_stderr(tmp_path):
    binary = _fake_claude(tmp_path, CRASH)
    provider = _provider(binary)
    with pytest.raises(ProviderError, match="boom"):
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )


async def test_unbound_dispatcher_is_loud(tmp_path):
    binary = _fake_claude(tmp_path, HAPPY)
    provider = ClaudeCodeProvider(binary=binary)  # no bind_dispatcher
    with pytest.raises(ProviderError, match="dispatcher"):
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )


async def test_timeout_kills_subprocess(tmp_path):
    binary = _fake_claude(tmp_path, "import time\ntime.sleep(60)\n")
    provider = _provider(binary, timeout_s=1.0)
    with pytest.raises(ProviderError, match="timed out"):
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_provider_claude_code.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.provider_claude_code'`

- [ ] **Step 3: Implement `src/harness/provider_claude_code.py`**

```python
"""Claude on subscription auth: spawn headless `claude -p` per turn.

The harness never touches claude.ai credentials — it invokes the user's own
installed, logged-in Claude Code binary (the documented headless mode). Tools
are served to CC over McpToolServer; CC's built-ins and user-scope settings
are disabled so the harness's registry is the only tool surface. One
complete() call == one CC agent turn (stateless v1: full history re-rendered;
--resume is a follow-up)."""

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from harness.errors import MalformedStreamError, ProviderError
from harness.mcp_serve import McpToolServer
from harness.messages import Message, Role
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
        )
        saw_result = False
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
                                f"claude-code turn failed: {event.get('result') or event.get('subtype')}"
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
            proc.kill()
            await proc.wait()
            raise ProviderError(f"claude-code turn timed out after {self.timeout_s}s") from None
        if proc.returncode not in (0, None) and not saw_result:
            stderr = b""
            if proc.stderr is not None:
                stderr = await proc.stderr.read()
            raise ProviderError(
                f"claude-code exited {proc.returncode}: "
                f"{stderr.decode('utf-8', errors='replace')[-500:].strip() or 'no stderr'}"
            )
        if not saw_result:
            raise MalformedStreamError("claude-code stream ended without a result event")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_provider_claude_code.py -v`
Expected: PASS (all 7). Note the happy fixture writes its argv file *after* the result line; the provider drains stdout to EOF, so the file exists by the time the test reads it. Check `Role.USER.value == "user"` (`messages.py`) — if the enum value differs, fix `_render_prompt` to match, not the enum.

- [ ] **Step 5: Commit**

```bash
git add src/harness/provider_claude_code.py tests/test_provider_claude_code.py
git commit -m "feat: ClaudeCodeProvider — headless claude -p turns with harness tools over MCP"
```

---

### Task 4: `CatalogProvider` backend dispatch + kernel wiring + docs

**Files:**
- Modify: `src/harness/provider_litellm.py` (CatalogProvider, lines ~258-288)
- Modify: `src/harness/cli.py` (after `loop = AgentLoop(**loop_kwargs)`, line ~177; CatalogProvider construction sites, lines ~413 and ~438)
- Modify: `docs/user-guide.md` (new section)
- Test: `tests/test_backend_dispatch.py` (create)

**Interfaces:**
- Consumes: `ResolvedModel.backend` (Task 1), `ClaudeCodeProvider` (Task 3).
- Produces: `CatalogProvider(catalog, claude_code=None)` with `bind_dispatcher(dispatcher)`; `build_kernel` calls `provider.bind_dispatcher(loop.dispatcher)` when the provider has that attribute.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backend_dispatch.py
"""CatalogProvider routes backend='claude-code' entries to ClaudeCodeProvider
and everything else down the existing LiteLLM path."""

import pytest

from harness.catalog import Catalog
from harness.errors import ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import TextDelta, collect
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId

USER = [Message(role=Role.USER, blocks=(TextBlock(text="hi"),))]


def _catalog(tmp_path):
    p = tmp_path / "models.toml"
    p.write_text(
        '[models.claude]\nbackend = "claude-code"\nroute = "claude-code/default"\n'
        "input_cost_per_token = 0.0\noutput_cost_per_token = 0.0\n"
    )
    return Catalog.load(p)


class _FakeClaudeBackend:
    def __init__(self):
        self.calls = []
        self.bound = None

    def bind_dispatcher(self, dispatcher):
        self.bound = dispatcher

    async def complete(self, *, model, messages, tools=()):
        self.calls.append((str(model), len(tuple(messages)), len(tuple(tools))))
        yield TextDelta(text="from-claude-backend")


async def test_backend_entry_routes_to_claude_code(tmp_path):
    fake = _FakeClaudeBackend()
    provider = CatalogProvider(_catalog(tmp_path), claude_code=fake)
    message, _, _ = await collect(
        provider.complete(model=ModelId("claude"), messages=USER, tools=())
    )
    assert message.text() == "from-claude-backend"
    assert fake.calls == [("claude-code/default", 1, 0)]  # route passed through


async def test_backend_entry_without_wiring_is_loud(tmp_path):
    provider = CatalogProvider(_catalog(tmp_path))  # claude_code=None
    with pytest.raises(ProviderError, match="claude-code"):
        await collect(provider.complete(model=ModelId("claude"), messages=USER, tools=()))


def test_bind_dispatcher_forwards(tmp_path):
    fake = _FakeClaudeBackend()
    provider = CatalogProvider(_catalog(tmp_path), claude_code=fake)
    sentinel = object()
    provider.bind_dispatcher(sentinel)
    assert fake.bound is sentinel


def test_bind_dispatcher_noop_without_backend(tmp_path):
    provider = CatalogProvider(_catalog(tmp_path))
    provider.bind_dispatcher(object())  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_backend_dispatch.py -v`
Expected: FAIL — `TypeError: CatalogProvider.__init__() got an unexpected keyword argument 'claude_code'`

- [ ] **Step 3: Modify `CatalogProvider` in `src/harness/provider_litellm.py`**

Add the field and method, and the backend branch at the top of the resolved path (before the api_key line):

```python
@dataclass
class CatalogProvider:
    """Resolves endpoint + key per call from the model string via the catalog,
    so any catalog model is reachable in one session. The model string is an
    ALIAS; an unknown alias falls back to a literal route on ambient env.
    Entries with backend="claude-code" route to the subscription-CLI provider."""

    catalog: "Catalog"
    claude_code: "ClaudeCodeProvider | None" = None

    def bind_dispatcher(self, dispatcher) -> None:
        if self.claude_code is not None:
            self.claude_code.bind_dispatcher(dispatcher)
```

and inside `complete`, after `resolved = self.catalog.resolve(str(model))` succeeds:

```python
        if resolved.backend == "claude-code":
            if self.claude_code is None:
                raise ProviderError(
                    f"model {model!r} needs the claude-code backend, which is not wired"
                )
            async for chunk in self.claude_code.complete(
                model=resolved.route, messages=messages, tools=tools
            ):
                yield chunk
            return
```

Imports: `from harness.errors import ProviderError` is already imported via `map_exception`'s module — check top of file; add `from typing import TYPE_CHECKING` guard for `ClaudeCodeProvider` if needed:

```python
if TYPE_CHECKING:
    from harness.provider_claude_code import ClaudeCodeProvider
```

- [ ] **Step 4: Wire construction and binding in `src/harness/cli.py`**

At both CatalogProvider construction sites (lines ~413 and ~438), replace `CatalogProvider(catalog)` with:

```python
        from harness.provider_claude_code import ClaudeCodeProvider

        provider = CatalogProvider(catalog, claude_code=ClaudeCodeProvider())
```

(keep the `provider: ModelProvider =` annotation at the first site). In `build_kernel`, immediately after `loop = AgentLoop(**loop_kwargs)` (line ~177):

```python
    if hasattr(provider, "bind_dispatcher"):
        provider.bind_dispatcher(loop.dispatcher)
```

- [ ] **Step 5: Run new tests and full suite**

Run: `.venv/bin/python -m pytest tests/test_backend_dispatch.py -v && .venv/bin/python -m pytest tests/ -q`
Expected: all PASS, full suite green (the bind is a no-op for providers without the attribute — FakeProvider/EchoProvider paths unaffected).

- [ ] **Step 6: Document in `docs/user-guide.md`**

Append a section (match the file's existing heading style):

```markdown
## Claude on your Claude Code subscription

Entries with `backend = "claude-code"` run turns through your locally
installed, logged-in Claude Code CLI (headless `claude -p`) instead of an
API. The harness serves its own tools to Claude over MCP and disables
Claude Code's built-ins, so permissions and the event log behave exactly as
with API models. The harness never handles claude.ai credentials — log in
with `claude` once and the backend uses that.

```toml
[models.claude]
backend = "claude-code"
route = "claude-code/default"   # "claude-code/<model>" passes --model <model>
input_cost_per_token = 0.0      # subscription: flat-rate, no per-token cost
output_cost_per_token = 0.0
tags = ["anthropic", "subscription", "tool-calling", "frontier"]
```

Requirements: `claude` on PATH and logged in (Pro/Max). One harness turn is
one Claude Code agent turn; Max-plan rate limits apply.
```

- [ ] **Step 7: Commit**

```bash
git add src/harness/provider_litellm.py src/harness/cli.py docs/user-guide.md tests/test_backend_dispatch.py
git commit -m "feat: catalog backend dispatch — claude-code entries route to ClaudeCodeProvider, wired in build_kernel"
```

---

### Task 5: End-to-end verification (manual, subscription — not CI)

**Files:**
- Modify: `~/.config/harness/models.toml` (user config, not repo)

No code. This is the repo's `verified = true` bar from the spec. Run each step and check the described evidence before stamping.

- [ ] **Step 1: Add the `[models.claude]` entry** from Task 4's doc block to `~/.config/harness/models.toml`.

- [ ] **Step 2: One real tool-calling turn**

Run: `harness --model claude "Use the read_file tool to read pyproject.toml and tell me the project name."` (adjust to the harness CLI's actual prompt form; `harness --help` shows it)
Expected: answer names `harness`; the session event log (`harness events` or the events file under the session dir) shows a `ToolCallDispatched`/tool event for `read_file` and a `ModelCallCompleted` for alias `claude` — the tool call went through the dispatcher, not CC's built-ins.

- [ ] **Step 3: Permission behavior spot-check**

Run the same prompt but ask it to write a file, with a permission config that denies writes (or interactively deny).
Expected: CC receives the denial text as a tool error and reports it; the event log records the denial. No file written.

- [ ] **Step 4: Mixed-backend session**

Run a turn where `claude` (parent) uses the `dispatch_agent` tool to hand a subtask to a `local`-pinned subagent (llama.cpp up via `scripts/serve-local.sh`).
Expected: event log shows model calls for both `claude` and `local` in one session.

- [ ] **Step 5: Stamp verification**

Add to the `[models.claude]` entry: `verified = true` and a dated comment, matching the `local` entry's convention. Commit nothing (user config); optionally note the verification date in `docs/user-guide.md`.
