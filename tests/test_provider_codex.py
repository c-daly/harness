"""CodexProvider against fake `codex` executables replaying the `codex exec
--json` event stream captured live from codex-cli 0.147.0. No real CLI, no
subscription use."""

import asyncio
import json
import os
import stat
import tempfile

import pytest

from harness.dispatcher import ToolOutcome, current_dispatch_tool
from harness.errors import MalformedStreamError, ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import TextDelta, ThinkingDelta, collect
from harness.provider_codex import CodexProvider
from harness.types import ModelId

# Every fake asserts the exec/--json shape up front, then dumps what the
# provider handed it (argv, stdin, env, cwd state, CODEX_HOME state) as
# sidecar files next to the binary -- never inside cwd, which the flag
# contract test asserts is empty.
_PREAMBLE = """
import json, os, sys

assert sys.argv[1:3] == ["exec", "--json"], sys.argv
_here = sys.argv[0]
_prompt = sys.stdin.read()
_cwd = os.getcwd()
_home = os.environ.get("CODEX_HOME")
_config = os.path.join(_home or "", "config.toml")
open(_here + ".argv", "w").write(json.dumps(sys.argv[1:]))
open(_here + ".stdin", "w").write(_prompt)
open(_here + ".env", "w").write(json.dumps(sorted(os.environ.keys())))
open(_here + ".cwd", "w").write(json.dumps({"path": _cwd, "empty": not os.listdir(_cwd)}))
open(_here + ".codexhome", "w").write(json.dumps({
    "value": _home,
    "has_auth_json": bool(_home) and os.path.isfile(os.path.join(_home, "auth.json")),
    "config_toml": open(_config).read() if os.path.isfile(_config) else None,
}))


def emit(event):
    print(json.dumps(event), flush=True)
"""

# Verbatim event contract from a live `codex exec --json` run against a real
# McpToolServer: the harness tool call ran with no elicitation of any kind.
HAPPY = _PREAMBLE + """
emit({"type": "thread.started", "thread_id": "01a0feed"})
emit({"type": "turn.started"})
emit({"type": "item.started", "item": {"id": "item_0", "type": "mcp_tool_call",
      "server": "harness", "tool": "echo", "arguments": {}, "result": None,
      "error": None, "status": "in_progress"}})
emit({"type": "item.completed", "item": {"id": "item_0", "type": "mcp_tool_call",
      "server": "harness", "tool": "echo", "arguments": {},
      "result": {"content": [{"type": "text", "text": "echo-result-42"}],
                 "structured_content": None},
      "error": None, "status": "completed"}})
emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
      "text": "echo-result-42"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 37337,
      "cached_input_tokens": 22016, "cache_write_input_tokens": 0,
      "output_tokens": 184, "reasoning_output_tokens": 52}})
"""

REASONING_THEN_TEXT = _PREAMBLE + """
emit({"type": "thread.started", "thread_id": "01a0feed"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"type": "reasoning", "text": "considering options"}})
emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
      "text": "echo-result-42"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 37337,
      "cached_input_tokens": 22016, "cache_write_input_tokens": 0,
      "output_tokens": 184, "reasoning_output_tokens": 52}})
"""

EMPTY_OR_MISSING_REASONING = _PREAMBLE + """
emit({"type": "thread.started", "thread_id": "01a0fee2"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"type": "reasoning", "text": ""}})
emit({"type": "item.completed", "item": {"type": "reasoning"}})
emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
      "text": "echo-result-42"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 1,
      "cached_input_tokens": 0, "cache_write_input_tokens": 0,
      "output_tokens": 1, "reasoning_output_tokens": 0}})
"""

FAILED = _PREAMBLE + """
emit({"type": "thread.started", "thread_id": "01a0dead"})
emit({"type": "turn.started"})
emit({"type": "turn.failed", "error": {"message": "Not logged in"}})
"""

NO_TURN_COMPLETED = _PREAMBLE + """
emit({"type": "thread.started", "thread_id": "01a0bare"})
emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
      "text": "half a turn"}})
"""

CRASH = _PREAMBLE + """
sys.stderr.write("boom: something broke")
sys.exit(2)
"""

SLEEPER = _PREAMBLE + """
import time
open(_here + ".pid", "w").write(str(os.getpid()))
time.sleep(60)
"""

ONE_DELTA_THEN_SLEEP = _PREAMBLE + """
import time
open(_here + ".pid", "w").write(str(os.getpid()))
emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
      "text": "partial"}})
time.sleep(60)
"""

# The scratch CODEX_HOME the provider builds must carry exactly this, byte for
# byte: codex exec resolves an MCP tool approval affirmatively only when the
# home it runs under names an approvals reviewer.
EXPECTED_CONFIG_TOML = """approvals_reviewer = "auto_review"
"""

USER = [Message(role=Role.USER, blocks=(TextBlock(text="say pong"),))]


@pytest.fixture(autouse=True)
def _isolated_codex_home(tmp_path, monkeypatch):
    """Every CodexProvider turn copies auth.json out of $CODEX_HOME (or
    ~/.codex if unset) into a fresh scratch dir. Default CODEX_HOME to an
    empty directory with no auth.json so tests never touch the real ChatGPT
    credentials on this machine as a side effect; test_flag_contract
    overrides this to exercise the copy explicitly."""
    empty = tmp_path / "no-codex-home"
    empty.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(empty))


def _fake_codex(tmp_path, script_body: str) -> str:
    path = tmp_path / "codex"
    path.write_text("#!/usr/bin/env python3\n" + script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class _Dispatcher:
    async def dispatch_tool(self, call):  # never called in these tests
        return ToolOutcome(text="", blob=None, is_error=False)


def _provider(binary: str, timeout_s: float = 30.0) -> CodexProvider:
    p = CodexProvider(binary=binary, timeout_s=timeout_s)
    p.bind_dispatcher(_Dispatcher())
    return p


async def _wait_for_death(pid: int) -> bool:
    for _ in range(60):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def _read_pid(path: str) -> int:
    for _ in range(60):
        if os.path.exists(path):
            return int(open(path).read())
        await asyncio.sleep(0.05)
    raise AssertionError("fake codex never wrote its pid file")


async def test_happy_turn_maps_chunks_and_usage(tmp_path):
    provider = _provider(_fake_codex(tmp_path, HAPPY))
    message, usage, stop = await collect(
        provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
    )
    assert message.text() == "echo-result-42"  # only the agent_message item is output
    assert stop == "end_turn"
    assert usage.input_tokens == 37337
    assert usage.output_tokens == 184
    assert usage.cache_read_tokens == 22016
    assert usage.cache_write_tokens == 0


async def test_reasoning_item_yields_thinking_delta_before_text_delta(tmp_path):
    provider = _provider(_fake_codex(tmp_path, REASONING_THEN_TEXT))
    chunks = [
        c
        async for c in provider.complete(
            model=ModelId("codex/default"), messages=USER, tools=()
        )
    ]
    thinking_indices = [i for i, c in enumerate(chunks) if isinstance(c, ThinkingDelta)]
    text_indices = [i for i, c in enumerate(chunks) if isinstance(c, TextDelta)]
    assert thinking_indices, "expected a ThinkingDelta chunk"
    assert text_indices, "expected a TextDelta chunk"
    assert thinking_indices[0] < text_indices[0]
    assert chunks[thinking_indices[0]] == ThinkingDelta(text="considering options")


async def test_reasoning_never_leaks_into_assembled_message_text(tmp_path):
    provider = _provider(_fake_codex(tmp_path, REASONING_THEN_TEXT))
    message, _, _ = await collect(
        provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
    )
    assert message.text() == "echo-result-42"


async def test_reasoning_item_with_empty_or_missing_text_yields_nothing(tmp_path):
    provider = _provider(_fake_codex(tmp_path, EMPTY_OR_MISSING_REASONING))
    chunks = [
        c
        async for c in provider.complete(
            model=ModelId("codex/default"), messages=USER, tools=()
        )
    ]
    assert not any(isinstance(c, ThinkingDelta) for c in chunks)


async def test_flag_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # A fake user CODEX_HOME with a real auth.json, so this test can prove it
    # gets copied into the isolated scratch home the child actually sees.
    fake_user_home = tmp_path / "fake-user-codex-home"
    fake_user_home.mkdir()
    (fake_user_home / "auth.json").write_text(json.dumps({"fake": "auth"}))
    monkeypatch.setenv("CODEX_HOME", str(fake_user_home))
    binary = _fake_codex(tmp_path, HAPPY)
    provider = _provider(binary)
    await collect(provider.complete(model=ModelId("codex/default"), messages=USER, tools=()))

    argv = json.loads(open(binary + ".argv").read())
    assert argv[:2] == ["exec", "--json"]
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    config = argv[argv.index("-c") + 1]
    assert config.startswith("mcp_servers.harness.url=")  # dotted: merges, not replaces
    url = json.loads(config.split("=", 1)[1])
    assert url.startswith("http://127.0.0.1")  # the per-turn McpToolServer
    assert url.endswith("/mcp/")  # trailing slash skips the Starlette mount 307
    assert argv[-1] == "-"  # prompt comes from stdin
    assert "-m" not in argv  # "default" suffix means no override
    assert not any("say pong" in a for a in argv)

    stdin = open(binary + ".stdin").read()
    assert "say pong" in stdin
    assert "mcp__harness" in stdin  # orientation prefix rides on the prompt

    cwd = json.loads(open(binary + ".cwd").read())
    assert cwd["empty"] is True  # a fresh, empty per-turn scratch dir
    assert cwd["path"] != os.getcwd()

    env_keys = set(json.loads(open(binary + ".env").read()))
    assert "OPENAI_API_KEY" not in env_keys and "ANTHROPIC_API_KEY" not in env_keys
    assert "PATH" in env_keys
    assert "CODEX_HOME" in env_keys

    codex_home = json.loads(open(binary + ".codexhome").read())
    assert codex_home["value"] not in (None, str(fake_user_home))  # isolated scratch home
    assert codex_home["has_auth_json"] is True  # auth.json carried over from the fake source
    # Without this key seeded into the scratch home, codex exec auto-declines
    # every MCP tool call ("user cancelled MCP tool call") -- live-bisected.
    assert codex_home["config_toml"] == EXPECTED_CONFIG_TOML
    assert not os.path.exists(codex_home["value"])  # torn down after the turn


async def test_model_suffix_becomes_override(tmp_path):
    binary = _fake_codex(tmp_path, HAPPY)
    provider = _provider(binary)
    await collect(
        provider.complete(model=ModelId("codex/gpt-5.2-codex"), messages=USER, tools=())
    )
    argv = json.loads(open(binary + ".argv").read())
    assert argv[argv.index("-m") + 1] == "gpt-5.2-codex"
    assert argv[-1] == "-"


async def test_turn_failed_raises_provider_error(tmp_path):
    provider = _provider(_fake_codex(tmp_path, FAILED))
    with pytest.raises(ProviderError, match="Not logged in"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_crash_raises_provider_error_with_stderr(tmp_path):
    provider = _provider(_fake_codex(tmp_path, CRASH))
    with pytest.raises(ProviderError, match="boom"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_missing_binary_raises_provider_error(tmp_path):
    provider = _provider("/nonexistent/codex")
    with pytest.raises(ProviderError, match="spawn failed"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_unbound_dispatcher_is_loud(tmp_path):
    provider = CodexProvider(binary=_fake_codex(tmp_path, HAPPY))  # no bind_dispatcher
    with pytest.raises(ProviderError, match="dispatcher"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_eof_without_turn_completed_raises_malformed_stream_error(tmp_path):
    provider = _provider(_fake_codex(tmp_path, NO_TURN_COMPLETED))
    with pytest.raises(MalformedStreamError):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )


async def test_timeout_kills_process_group(tmp_path):
    binary = _fake_codex(tmp_path, SLEEPER)
    provider = _provider(binary, timeout_s=1.0)
    with pytest.raises(ProviderError, match="timed out"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )
    pid = await _read_pid(binary + ".pid")
    assert await _wait_for_death(pid), "child survived the turn timeout"


async def test_abandoning_stream_kills_process_group(tmp_path):
    binary = _fake_codex(tmp_path, ONE_DELTA_THEN_SLEEP)
    provider = _provider(binary, timeout_s=30.0)
    gen = provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
    first = await gen.__anext__()
    assert isinstance(first, TextDelta)
    await gen.aclose()
    pid = await _read_pid(binary + ".pid")
    assert await _wait_for_death(pid), "child survived the stream being abandoned"


async def test_contextvar_dispatch_overrides_bound_dispatcher(tmp_path, monkeypatch):
    provider = _provider(_fake_codex(tmp_path, HAPPY))  # bound to the _Dispatcher shim

    captured = {}

    class _FakeMcpToolServer:
        def __init__(self, *, specs, dispatch):
            captured["dispatch"] = dispatch

        @property
        def url(self):
            return "http://127.0.0.1:0/mcp"

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr("harness.provider_codex.McpToolServer", _FakeMcpToolServer)

    async def recorder_b(call):
        return ToolOutcome(text="", blob=None, is_error=False)

    token = current_dispatch_tool.set(recorder_b)
    try:
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )
    finally:
        current_dispatch_tool.reset(token)

    assert captured["dispatch"] is recorder_b


async def test_scratch_setup_failure_still_stops_started_server(tmp_path, monkeypatch):
    """Critical fix: if scratch-dir setup (tempfile.mkdtemp, e.g. under
    ENOSPC) raises after the McpToolServer has already been started, the
    server must still be stopped rather than leaking a bound port for the
    process lifetime."""
    events = []

    class _FakeMcpToolServer:
        def __init__(self, *, specs, dispatch):
            pass

        @property
        def url(self):
            return "http://127.0.0.1:0/mcp"

        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr("harness.provider_codex.McpToolServer", _FakeMcpToolServer)

    def _boom(*args, **kwargs):
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)

    provider = _provider(_fake_codex(tmp_path, HAPPY))
    with pytest.raises(OSError, match="ENOSPC"):
        await collect(
            provider.complete(model=ModelId("codex/default"), messages=USER, tools=())
        )

    # The turn never even reached the point of touching disk beyond the
    # server: the failure must not hang or get swallowed, and the server
    # that was already started must still be torn down.
    assert events == ["start", "stop"]
