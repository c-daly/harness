# Tool Surface + Codex Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The tool surface becomes invariant and deliberately selectable across models: standard MCP servers configurable with per-server tool filtering and lossless enable/disable, a session-start checklist in the TUI, a context-cost toast for small-context models, a `codex` subscription backend over `codex mcp-server`, and the local model upgraded to Qwen3.6-35B-A3B.

**Architecture:** Extend `mcp.toml` with `tools_allow` (fnmatch globs) and `default_enabled` per server; the TUI shows a checkbox modal before `McpHost.start()` so unchecked servers never start (off = zero presence, on = full capability). `CodexProvider` mirrors `ClaudeCodeProvider`: one `complete()` = one `codex mcp-server` child spoken to via the `mcp` client library, with the harness's per-turn `McpToolServer` injected by replacing codex's `mcp_servers` config table at spawn, ChatGPT-subscription auth, `OPENAI_API_KEY` stripped.

**Tech Stack:** Python 3.12, asyncio, `mcp>=1.24` (client + server, already a dependency), Textual (TUI), pytest asyncio_mode auto. No new dependencies.

**Spec:** Design agreed in-session 2026-08-15 (this plan is its record); the codex backend implements the "Follow-up adapters" section of `docs/superpowers/specs/2026-08-15-subscription-cli-model-backends-design.md` and inherits its constraints and as-built conventions.

## Global Constraints

- **Lossless toggles (user requirement, verbatim):** an integration "has to be able to be cleanly disabled without sacrificing its functionality when it is enabled." OFF = never started, zero context cost, no residue. ON = full capability.
- **No per-token billing on subscription backends:** the codex child env must not contain `OPENAI_API_KEY` (or the other secret keys `_SECRET_ENV_KEYS` strips) — ChatGPT auth from `~/.codex/auth.json` only. The harness never handles provider credentials.
- **The harness engine is the only gate:** codex runs `sandbox="read-only"`, `approval-policy="never"`; harness tools flow through `Dispatcher.dispatch_tool` via `McpToolServer`.
- **All construction sites, grep-derived:** `CatalogProvider(` appears at exactly THREE sites: `src/harness/cli.py` (two, in `_run_main`) and `src/harness/tui.py` (`_switch_model`). Every task touching its signature updates all three. Re-run `grep -rn "CatalogProvider(" src/` before committing such a task.
- TDD; tests under `tests/`; pytest `asyncio_mode = "auto"`; ruff line-length 100; full suite green before each commit, run as its own step with exit code checked (never piped).
- **Docs land with the change** (user requirement): each task's commit includes its user-guide/architecture updates.
- **Captured codex ground truth (live, codex-cli 0.147.0, 2026-08-15):** `tools/call codex` result = `{"structuredContent": {"threadId": "...", "content": "<final text>"}, "content": [{"type": "text", "text": "<final text>"}]}`; `codex/event` notifications stream during the call (incl. `token_count` with `info.total_token_usage.input_tokens/output_tokens` and `task_complete`); codex starts its OWN configured MCP servers unless `mcp_servers` is overridden at spawn; `codex mcp-server -c key=value` accepts TOML-inline config overrides; probe ran clean with `OPENAI_API_KEY` removed (subscription auth).

---

### Task 1: `tools_allow` + `default_enabled` in mcp.toml, filtered tool exposure

**Files:**
- Modify: `src/harness/mcp_config.py` (`_KNOWN_KEYS`, `McpServerSpec`, `_parse_server`)
- Modify: `src/harness/mcp_host.py` (tool listing/registration point)
- Modify: `docs/user-guide.md` (MCP section: document both keys)
- Test: `tests/test_tools_allow.py` (create)

**Interfaces:**
- Consumes: existing `McpServerSpec`, `_parse_server`, `McpConfigError`; `McpHost` internals (read the file: the point where `_list_all_tools` results become `mcp__<server>__<tool>` registry entries).
- Produces: `McpServerSpec.tools_allow: tuple[str, ...] = ()` (empty = expose all) and `McpServerSpec.default_enabled: bool = True`. Task 2 reads `default_enabled`; the host exposes only tools whose name fnmatches any `tools_allow` glob.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools_allow.py
"""Per-server tool filtering (tools_allow globs) and checklist defaults
(default_enabled) in mcp.toml."""

import pytest

from harness.mcp_config import McpConfigError, load_mcp_file


def _load(tmp_path, body: str):
    p = tmp_path / "mcp.toml"
    p.write_text(body)
    return load_mcp_file(p, source="user")


def test_tools_allow_parses_and_defaults_empty(tmp_path):
    specs = _load(
        tmp_path,
        '[servers.router]\ncommand = "/bin/mcp-router"\n'
        'tools_allow = ["workflow__*", "experiment__*"]\n'
        "\n[servers.plain]\ncommand = \"/bin/other\"\n",
    )
    by_name = {s.name: s for s in specs}
    assert by_name["router"].tools_allow == ("workflow__*", "experiment__*")
    assert by_name["plain"].tools_allow == ()


def test_default_enabled_parses_and_defaults_true(tmp_path):
    specs = _load(
        tmp_path,
        '[servers.a]\ncommand = "/bin/a"\ndefault_enabled = false\n'
        "\n[servers.b]\ncommand = \"/bin/b\"\n",
    )
    by_name = {s.name: s for s in specs}
    assert by_name["a"].default_enabled is False
    assert by_name["b"].default_enabled is True


def test_tools_allow_rejects_non_string_entries(tmp_path):
    with pytest.raises(McpConfigError, match="tools_allow"):
        _load(tmp_path, '[servers.x]\ncommand = "/bin/x"\ntools_allow = [1]\n')


def test_default_enabled_rejects_non_bool(tmp_path):
    with pytest.raises(McpConfigError, match="default_enabled"):
        _load(tmp_path, '[servers.x]\ncommand = "/bin/x"\ndefault_enabled = "yes"\n')
```

(If `load_mcp_file`'s signature differs — check `tests/test_cli.py:451` for the real call shape `load_mcp_file(home / "mcp.toml", source="user")` — keep the tests aligned with the real API, not the other way around.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tools_allow.py -v`
Expected: FAIL — `AssertionError`/`AttributeError` (no `tools_allow` attribute) and no `McpConfigError` raised.

- [ ] **Step 3: Implement the config side in `src/harness/mcp_config.py`**

Add to `_KNOWN_KEYS`: `"tools_allow", "default_enabled"`. Add fields to `McpServerSpec` (after `tool_timeout_s`):

```python
    tools_allow: tuple[str, ...] = ()  # fnmatch globs on server tool names; empty = all
    default_enabled: bool = True  # pre-checked in the session-start checklist
```

In `_parse_server`, before the `return`:

```python
    tools_allow = body.get("tools_allow", [])
    if not isinstance(tools_allow, list) or not all(isinstance(t, str) for t in tools_allow):
        raise McpConfigError(f"server {name!r}: tools_allow must be an array of strings")
    default_enabled = body.get("default_enabled", True)
    if not isinstance(default_enabled, bool):
        raise McpConfigError(f"server {name!r}: default_enabled must be a boolean")
```

and pass `tools_allow=tuple(tools_allow), default_enabled=default_enabled,` to the `McpServerSpec(...)` constructor.

- [ ] **Step 4: Write the failing host-filter test (append to `tests/test_tools_allow.py`)**

Follow the fake-transport pattern in `tests/test_mcp_host.py` (read it first; it builds `McpHost`/`ManagedServer` with a `transport_factory` serving scripted tools). The test: a fake server lists tools `["workflow__start", "native__bash", "experiment__log"]`; a spec with `tools_allow = ("workflow__*", "experiment__*")` results in registry entries for `mcp__router__workflow__start` and `mcp__router__experiment__log` only — `mcp__router__native__bash` absent. Copy the file's existing fixture idiom exactly; assert via the registry's `specs()` names.

- [ ] **Step 5: Implement the filter in `src/harness/mcp_host.py`**

At the single point where listed tools are turned into registry entries, add:

```python
import fnmatch

def _allowed(spec_globs: tuple[str, ...], tool_name: str) -> bool:
    return not spec_globs or any(fnmatch.fnmatch(tool_name, g) for g in spec_globs)
```

and skip tools where `not _allowed(spec.tools_allow, tool.name)`. Filtering happens at exposure, not at call time — a filtered-out tool must not exist in the registry at all.

- [ ] **Step 6: Run the new tests, then the full suite (own step, check exit code), update docs**

Run: `.venv/bin/python -m pytest tests/test_tools_allow.py -v` → PASS.
Run: `.venv/bin/python -m pytest tests/ -q`; check `$?` is 0 before proceeding.
Docs: in `docs/user-guide.md`'s MCP section, document both keys with the agent-swarm example:

```toml
[servers.agent-swarm]
command = "/home/fearsidhe/.claude/plugins/cache/fearsidhe-plugins/agent-swarm/1.1.0/bin/mcp-router"
tools_allow = ["workflow__*", "experiment__*", "router__*"]  # its unique families only
default_enabled = false  # present in the checklist, dormant unless opted in
```

- [ ] **Step 7: Commit**

```bash
git add src/harness/mcp_config.py src/harness/mcp_host.py tests/test_tools_allow.py docs/user-guide.md
git commit -m "feat(mcp): tools_allow filtering + default_enabled per server"
```

---

### Task 2: Session-start server checklist (TUI)

**Files:**
- Modify: `src/harness/tui.py` (new `ServerChecklistScreen`; `on_mount` at the `kernel.mcp` start block, lines ~205-212)
- Modify: `docs/user-guide.md` (describe the checklist)
- Test: `tests/test_tui.py` (append)

**Interfaces:**
- Consumes: `McpHost` (read `mcp_host.py` for how it holds its servers — it is constructed from `Sequence[McpServerSpec]`; you need each server's `spec`), `McpServerSpec.default_enabled` (Task 1), the `PermissionScreen` ModalScreen idiom (`tui.py:77`).
- Produces: before `kernel.mcp.start()`, the TUI shows a checkbox list (one row per configured server, pre-checked from `default_enabled`); unchecked servers are removed from the host for this session BEFORE start so they never launch. Headless `-p` never shows UI: it starts only `default_enabled` servers (implement that in `McpHost.start()` gaining an optional `only: set[str] | None = None` parameter — `None` = all; cli's `run_once` path passes `only={s for s in specs if s.default_enabled}` equivalently; read `cli.py`'s `run_once`/pump-start region for where `mcp.start()` is called headless).

- [ ] **Step 1: Write the failing tests (append to `tests/test_tui.py`, following its `make_app` idiom — read the top of the file for how apps are built with kernels; build a kernel whose mcp specs include one `default_enabled = true` and one `= false` fake server using the fake-transport pattern from `tests/test_mcp_host.py`)**

Three tests:

```python
async def test_checklist_lists_servers_with_defaults(tmp_path):
    # App with two configured fake servers: "on-server" (default_enabled True),
    # "off-server" (False). After mount, a ServerChecklistScreen is pushed;
    # its checkboxes reflect defaults.
    ...
    assert screen.query_one("#chk-on-server").value is True
    assert screen.query_one("#chk-off-server").value is False


async def test_unchecked_server_never_starts(tmp_path):
    # Accept the checklist with off-server left unchecked: its transport
    # factory was never invoked (track with a counter in the fake), and no
    # mcp__off-server__* tools are in the registry.
    ...


async def test_checked_server_full_capability(tmp_path):
    # Accept with on-server checked: its tools are all present (respecting
    # tools_allow) and callable — the lossless-ON half of the invariant.
    ...
```

Write them fully against the real `make_app`/fake-transport idioms you find; the three assertions above are the contract. Fill the `...` with the actual pilot driving (`app.run_test()`, `pilot.pause`, accept via a key binding you define, e.g. Enter on the screen).

- [ ] **Step 2: Run to verify they fail** (`ServerChecklistScreen` undefined).

- [ ] **Step 3: Implement**

`ServerChecklistScreen(ModalScreen[set[str]])` styled after `PermissionScreen`: a `Checkbox(id=f"chk-{spec.name}", value=spec.default_enabled, label=f"{spec.name}  ({spec.command or spec.url})")` per server, Enter dismisses returning the checked names. In `on_mount`, wrap the existing mcp start block: if `kernel.mcp` has servers, `selected = await self.push_screen_wait(ServerChecklistScreen(kernel.mcp))`, then start with `await kernel.mcp.start(only=selected)`. `McpHost.start(only=None)` skips (does not construct/launch) servers not in `only` — they hold zero resources and register zero tools. Headless path in `cli.py`: pass `only=` the default-enabled names (no UI).

- [ ] **Step 4: Run the new tests → PASS; full suite (own step, exit code); docs**

Docs (`docs/user-guide.md`): a short "Session-start server checklist" subsection — defaults from `default_enabled`, unchecked servers never start, headless uses defaults.

- [ ] **Step 5: Commit**

```bash
git add src/harness/tui.py src/harness/mcp_host.py src/harness/cli.py tests/test_tui.py docs/user-guide.md
git commit -m "feat(tui): session-start MCP server checklist — lossless enable/disable"
```

---

### Task 3: Local-model context-cost toast

**Files:**
- Modify: `src/harness/tui.py` (`on_mount` after registry is final, and `_switch_model` after the model is set)
- Test: `tests/test_tui.py` (append)

**Interfaces:**
- Consumes: `kernel.registry.specs()` (`ToolSpec.parameters` JSON schemas), `catalog.resolve(alias)` → `ResolvedModel.max_input_tokens` / `.tags` (from `_switch_model`'s existing resolve), Textual's `App.notify(message, severity="warning", timeout=...)`.
- Produces: `def _schema_token_estimate(specs) -> int` in `tui.py` (module-level, testable): `sum(len(json.dumps({"name": str(s.name), "description": s.description, "parameters": s.parameters})) for s in specs) // 4`. A toast fires when the active model is constrained — `"local" in resolved.tags` or (`resolved.max_input_tokens or 10**9`) < 32768 — and the estimate exceeds 10% of `max_input_tokens` (use 16384 when unset for a local-tagged model).

- [ ] **Step 1: Failing tests** — two: (a) unit: `_schema_token_estimate` over two hand-built `ToolSpec`s returns `len(json)//4` sum (compute the literal expected value in the test); (b) pilot: an app whose kernel model resolves to a `local`-tagged, `max_input_tokens = 16384` alias with a fat fake registry (one spec with a ~40KB parameters schema) shows a notification containing `"of context"` after `/model` switch; a `gpt`-like alias shows none. Write them fully against the real idioms.

- [ ] **Step 2: RED.** — [ ] **Step 3: Implement** (estimate fn + `_maybe_warn_context(resolved)` called from `on_mount` and `_switch_model`; message format: `f"{alias}: {n} tools ≈ {est:,} tokens of schemas (~{pct}% of {ctx:,} context) — /tools to review"`). — [ ] **Step 4: GREEN + full suite (own step) + a line in the user-guide checklist subsection.** — [ ] **Step 5: Commit** `feat(tui): context-cost toast for constrained models`.

---

### Task 4: `CodexProvider` — codex backend over `codex mcp-server`

**Files:**
- Create: `src/harness/provider_codex.py`
- Modify: `src/harness/catalog.py` (`KNOWN_BACKENDS`), `src/harness/provider_litellm.py` (`CatalogProvider`), `src/harness/cli.py` + `src/harness/tui.py` (ALL THREE construction sites per Global Constraints)
- Modify: `docs/user-guide.md`, `docs/architecture.md`, `README.md` (docs land with the change)
- Test: `tests/test_provider_codex.py` (create), `tests/test_backend_dispatch.py` (extend)

**Interfaces:**
- Consumes: `McpToolServer` (`mcp_serve.py`), `current_dispatch_tool` (`dispatcher.py`), `_render_prompt` and `_sanitized_env` (import from `harness.provider_claude_code` — shared deliberately; do not duplicate), `Chunk`/`TextDelta`/`Usage`/`UsageReport`/`StreamStop` (`provider.py`), `ProviderError`/`MalformedStreamError` (`errors.py`), `mcp` client: `from mcp import ClientSession, StdioServerParameters; from mcp.client.stdio import stdio_client`.
- Produces: `CodexProvider(binary: str = "codex", timeout_s: float = 600.0)` with `bind_dispatcher(dispatcher)` and `async complete(*, model, messages, tools=()) -> AsyncIterator[Chunk]`; `KNOWN_BACKENDS == frozenset({"claude-code", "codex"})`; `CatalogProvider(catalog, claude_code=None, codex=None)` routing `backend == "codex"` entries with the same not-wired `ProviderError` shape.

- [ ] **Step 1: Write the failing provider tests**

```python
# tests/test_provider_codex.py
"""CodexProvider against a fake `codex` executable implementing the mcp-server
JSON-RPC surface (captured live from codex-cli 0.147.0). No subscription use."""

import json
import stat

import pytest

from harness.dispatcher import ToolOutcome
from harness.errors import ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import collect
from harness.provider_codex import CodexProvider
from harness.types import ModelId

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys

def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()

assert sys.argv[1] == "mcp-server", sys.argv
open(sys.argv[0] + ".argv", "w").write(json.dumps(sys.argv[1:]))
open(sys.argv[0] + ".env", "w").write(json.dumps(sorted(os.environ.keys())))
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get("method")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "codex-mcp-server", "version": "0.0-fake"}}})
    elif m == "tools/call":
        open(sys.argv[0] + ".call", "w").write(json.dumps(msg["params"]))
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "structuredContent": {"threadId": "t-1", "content": "pong"},
            "content": [{"type": "text", "text": "pong"}]}})
        break
'''

ERROR_CODEX = FAKE_CODEX.replace(
    '"structuredContent": {"threadId": "t-1", "content": "pong"},\n            "content": [{"type": "text", "text": "pong"}]}',
    '"content": [{"type": "text", "text": "Not logged in"}], "isError": True}',
)

USER = [Message(role=Role.USER, blocks=(TextBlock(text="say pong"),))]


def _fake(tmp_path, body: str) -> str:
    path = tmp_path / "codex"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class _Dispatcher:
    async def dispatch_tool(self, call):
        return ToolOutcome(text="", blob=None, is_error=False)


def _provider(binary, timeout_s=30.0):
    p = CodexProvider(binary=binary, timeout_s=timeout_s)
    p.bind_dispatcher(_Dispatcher())
    return p


async def test_happy_turn_maps_text(tmp_path):
    provider = _provider(_fake(tmp_path, FAKE_CODEX))
    message, usage, stop = await collect(
        provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
    )
    assert message.text() == "pong"
    assert stop == "end_turn"


async def test_call_contract(tmp_path):
    binary = _fake(tmp_path, FAKE_CODEX)
    provider = _provider(binary)
    await collect(provider.complete(model=ModelId("codex/default"), messages=USER, tools=()))
    argv = json.loads(open(binary + ".argv").read())
    assert argv[0] == "mcp-server"
    joined = " ".join(argv)
    assert "mcp_servers=" in joined and "http://127.0.0.1" in joined  # harness injected, table replaced
    call = json.loads(open(binary + ".call").read())
    assert call["name"] == "codex"
    args = call["arguments"]
    assert args["sandbox"] == "read-only"
    assert args["approval-policy"] == "never"
    assert "say pong" in args["prompt"]
    assert "model" not in args  # "default" suffix means no override
    env_keys = json.loads(open(binary + ".env").read())
    assert "OPENAI_API_KEY" not in env_keys and "ANTHROPIC_API_KEY" not in env_keys
    assert "PATH" in env_keys


async def test_model_suffix_becomes_override(tmp_path):
    binary = _fake(tmp_path, FAKE_CODEX)
    provider = _provider(binary)
    await collect(
        provider.complete(model=ModelId("codex/gpt-5.2-codex"), messages=USER, tools=())
    )
    call = json.loads(open(binary + ".call").read())
    assert call["arguments"]["model"] == "gpt-5.2-codex"


async def test_is_error_result_raises(tmp_path):
    provider = _provider(_fake(tmp_path, ERROR_CODEX))
    with pytest.raises(ProviderError, match="Not logged in"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_missing_binary_raises(tmp_path):
    provider = _provider("/nonexistent/codex")
    with pytest.raises(ProviderError):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_unbound_dispatcher_is_loud(tmp_path):
    provider = CodexProvider(binary=_fake(tmp_path, FAKE_CODEX))
    with pytest.raises(ProviderError, match="dispatcher"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_timeout_raises(tmp_path):
    slow = FAKE_CODEX.replace('elif m == "tools/call":',
                              'elif m == "tools/call":\n        import time; time.sleep(60)')
    provider = _provider(_fake(tmp_path, slow), timeout_s=2.0)
    with pytest.raises(ProviderError, match="timed out"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )
```

(Set env sentinels via `monkeypatch.setenv("OPENAI_API_KEY", "sk-test")` in `test_call_contract` — add the `monkeypatch` fixture parameter.)

- [ ] **Step 2: RED** (`ModuleNotFoundError: harness.provider_codex`).

- [ ] **Step 3: Implement `src/harness/provider_codex.py`**

```python
"""Codex on ChatGPT-subscription auth: one complete() = one `codex mcp-server`
child driven over MCP. The harness's tools reach codex by REPLACING its
mcp_servers config table at spawn (isolates the user's codex config AND mounts
the per-turn McpToolServer in one flag). sandbox=read-only + approval-policy=
never: the harness permission engine is the only gate. Codex's built-in
read-only shell remains — tool parity is additive, documented in the guide."""

import asyncio
from typing import AsyncIterator, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
                        result = await session.call_tool(
                            "codex", self._arguments(model=model, prompt=_render_prompt(messages))
                        )
        except TimeoutError:
            raise ProviderError(f"codex turn timed out after {self.timeout_s}s") from None
        except (OSError, Exception) as exc:  # narrow below in Step 3b
            if isinstance(exc, ProviderError):
                raise
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
```

**Step 3b — tighten the broad except:** the blanket `except Exception` above is a plan sketch, not the final shape — narrow it: catch `TimeoutError` (as shown), let `ProviderError` fly, and wrap `OSError`, `McpError` (from `mcp.shared.exceptions` — check the installed package's exception for failed spawn/protocol), and `anyio.BrokenResourceError` explicitly. The missing-binary test pins the behavior; make the implementation honest rather than catch-all. If `stdio_client` wraps spawn failure differently on the installed version, adapt to whatever makes `test_missing_binary_raises` pass with a `ProviderError`.

- [ ] **Step 4: GREEN on the provider tests.**

- [ ] **Step 5: Backend wiring (failing tests first, extend `tests/test_backend_dispatch.py`)**

Mirror the three existing claude-code tests for codex: catalog entry `backend = "codex"` routes to a `_FakeCodexBackend` (same shape as `_FakeClaudeBackend`), unwired raises `ProviderError` matching `"codex"`, `bind_dispatcher` forwards to both backends when present. Then implement: `KNOWN_BACKENDS = frozenset({"claude-code", "codex"})` in `catalog.py`; `CatalogProvider` gains `codex: "CodexProvider | None" = None`, a `backend == "codex"` branch (same shape as claude-code's), and `bind_dispatcher` forwards to both. Update ALL THREE construction sites to `CatalogProvider(catalog, claude_code=ClaudeCodeProvider(), codex=CodexProvider())` — then `grep -rn "CatalogProvider(" src/` and confirm exactly three call sites, all updated.

- [ ] **Step 6: Full suite (own step, exit code checked). Docs in the same commit:** user-guide "Codex on your ChatGPT subscription" section (mirror the claude section: entry example with `backend = "codex"`, the additive-shell caveat, log-in-once via `codex login`); architecture module inventory line for `provider_codex.py`; README's multi-model bullet mentions both subscription CLIs.

- [ ] **Step 7: Commit**

```bash
git add src/harness/provider_codex.py src/harness/catalog.py src/harness/provider_litellm.py src/harness/cli.py src/harness/tui.py tests/test_provider_codex.py tests/test_backend_dispatch.py docs/user-guide.md docs/architecture.md README.md
git commit -m "feat: codex backend — ChatGPT-subscription turns over codex mcp-server with harness tools injected"
```

---

### Task 5: Local model upgrade — Qwen3.6-35B-A3B

**Files:**
- Modify: `scripts/serve-local.sh` (default model, comments, NCPUMOE default)
- Modify: `docs/user-guide.md` (local model section: the new default + quant options)

No code; no tests beyond a shell syntax check. Grounded facts (HF API, 2026-08-15): repo `unsloth/Qwen3.6-35B-A3B-GGUF`; files `Qwen3.6-35B-A3B-UD-IQ4_XS.gguf` (17.7 GB, the new default), `UD-Q4_K_M` (22.1 GB, quality upgrade), `UD-Q3_K_XL` (16.8 GB, tight-RAM fallback); `mmproj-F16.gguf` exists (vision, not wired v1).

- [ ] **Step 1:** In `serve-local.sh`: default `MODEL` → `unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS`; default `NCPUMOE` → `28`; update the tuning comment block (35B-A3B MoE, ~17.7 GB, same 12 GB-VRAM strategy; note the old Qwen3-Coder line remains reachable via `HARNESS_LOCAL_MODEL`). Keep every env override intact.
- [ ] **Step 2:** `bash -n scripts/serve-local.sh` → exit 0.
- [ ] **Step 3:** user-guide local-model section: new default, the three quant options with sizes, and a note that `[models.local]`-style entries pair with whatever the server loads (`route` name is advisory to llama.cpp).
- [ ] **Step 4: Commit** `feat(local): default local model → Qwen3.6-35B-A3B UD-IQ4_XS`.

---

### Task 6: Live verification (manual, controller/user — not CI)

User-config + live checks; the repo's `verified = true` bar.

- [ ] **Step 1:** Populate `~/.config/harness/mcp.toml`: `[servers.serena]` (command `uvx`, args `["--from", "git+https://github.com/oraios/serena", "serena", "start-mcp-server"]`, `default_enabled = true`) and `[servers.agent-swarm]` (the router binary path, `tools_allow = ["workflow__*", "experiment__*", "router__*"]`, `default_enabled = false`).
- [ ] **Step 2:** Add `[models.codex]` (`backend = "codex"`, `route = "codex/default"`, zero costs, tags `["openai", "subscription", "tool-calling"]`) to `~/.config/harness/models.toml`.
- [ ] **Step 3:** TUI launch: checklist appears; serena checked, agent-swarm unchecked; accept; confirm no router process spawned (lossless OFF) and serena tools present under `/tools`.
- [ ] **Step 4:** Live codex turn: `harness -p "Use the read_file tool to read pyproject.toml and name the project. One sentence." --model codex --no-plugins` → answer names `harness`; session log shows `tool_call_*` for `read_file` inside the codex model call. Stamp `verified = true` (dated) on `[models.codex]`.
- [ ] **Step 5:** Second TUI run with agent-swarm CHECKED: registry contains `mcp__agent-swarm__workflow__*` tools and NOT `mcp__agent-swarm__native__*` (filter holds); call `workflow__workflow_is_active` once to prove full-capability ON (if the router requires registration, perform it at enable time and record what was needed in the ledger).
- [ ] **Step 6:** `HARNESS_LOCAL_MODEL` unset, run `scripts/serve-local.sh` (downloads ~17.7 GB on first run); add `[models.local36]` (`route = "openai/qwen3.6"`, `api_base = "http://localhost:8080/v1"`, zero costs, `max_input_tokens = 16384`, tags `["local", "tool-calling"]`); one live tool-calling turn → stamp `verified = true`; confirm the context-cost toast fires when switching to it with a loaded registry.

---

## Self-review notes (run before execution)

- Spec coverage: lossless toggle (T1 default_enabled + T2 never-start + T6 step 3/5), invariant surface with deliberate narrowing (T1 filter + T2 checklist), context warning (T3), codex subscription backend with all-three-sites wiring (T4), credible local model (T5/T6). No uncovered agreement.
- The three `CatalogProvider(` sites are enumerated from a live grep this session (cli ×2, tui ×1); Task 4 re-greps before commit.
- Type consistency: `CodexProvider` mirrors `ClaudeCodeProvider`'s exact construction/bind/complete shapes consumed by `CatalogProvider` and the tests; `tools_allow`/`default_enabled` names match across T1 config, T2 checklist, and T6 config files.
