"""ClaudeCodeProvider against fake `claude` executables emitting captured
stream-json shapes (CC v2.1.233). No real CLI, no subscription use."""

import asyncio
import json
import os
import stat

import pytest

from harness.dispatcher import ToolOutcome, current_dispatch_tool
from harness.errors import MalformedStreamError, ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import TextDelta, collect
from harness.provider_claude_code import DISALLOWED_BUILTINS, ClaudeCodeProvider
from harness.types import ModelId


def _fake_claude(tmp_path, script_body: str) -> str:
    path = tmp_path / "claude"
    path.write_text("#!/usr/bin/env python3\n" + script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


HAPPY = r"""
import json, sys
prompt = sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1", "tools": []}))
print(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5",
    "content": [{"type": "text", "text": "pong"}],
    "usage": {"input_tokens": 2, "output_tokens": 1}}, "session_id": "s-1"}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
    "num_turns": 1, "stop_reason": "end_turn", "result": "pong", "session_id": "s-1",
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 6646,
              "cache_read_input_tokens": 16241, "output_tokens": 4}}))
# also record argv and stdin length so tests can assert the flag contract
open(sys.argv[0] + ".argv", "w").write(json.dumps(sys.argv[1:]))
open(sys.argv[0] + ".stdinlen", "w").write(str(len(prompt)))
"""

ERROR_RESULT = r"""
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
    "result": "Not logged in", "session_id": "s-2", "num_turns": 0}))
"""

CRASH = r"""
import sys
sys.stdin.read()
sys.stderr.write("boom: something broke\n")
sys.exit(2)
"""

NO_RESULT = r"""
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-4", "tools": []}))
"""

ONE_DELTA_THEN_SLEEP = r"""
import json, os, sys, time
sys.stdin.read()
open(sys.argv[0] + ".pid", "w").write(str(os.getpid()))
print(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5",
    "content": [{"type": "text", "text": "partial"}],
    "usage": {"input_tokens": 1, "output_tokens": 1}}, "session_id": "s-3"}), flush=True)
time.sleep(60)
"""

ENV_DUMP = r"""
import json, os, sys
sys.stdin.read()
open(sys.argv[0] + ".env", "w").write(json.dumps(sorted(os.environ.keys())))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
    "num_turns": 0, "stop_reason": "end_turn", "result": "ok", "session_id": "s-5",
    "usage": {"input_tokens": 0, "output_tokens": 0}}))
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
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__harness"
    assert "--model" not in argv  # "default" suffix means no override
    # prompt travels over stdin now, never argv
    assert not any("say pong" in a for a in argv if isinstance(a, str))
    stdin_len = int(open(binary + ".stdinlen").read())
    assert stdin_len > 0


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
    binary = _fake_claude(tmp_path, "import sys, time\nsys.stdin.read()\ntime.sleep(60)\n")
    provider = _provider(binary, timeout_s=1.0)
    with pytest.raises(ProviderError, match="timed out"):
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )


async def test_eof_without_result_raises_malformed_stream_error(tmp_path):
    binary = _fake_claude(tmp_path, NO_RESULT)
    provider = _provider(binary)
    with pytest.raises(MalformedStreamError):
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )


async def test_abandoning_stream_kills_subprocess(tmp_path):
    binary = _fake_claude(tmp_path, ONE_DELTA_THEN_SLEEP)
    provider = _provider(binary, timeout_s=30.0)
    gen = provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
    first = await gen.__anext__()
    assert isinstance(first, TextDelta)
    await gen.aclose()

    pid_path = binary + ".pid"
    for _ in range(50):
        if os.path.exists(pid_path):
            break
        await asyncio.sleep(0.05)
    assert os.path.exists(pid_path), "fake claude never wrote its pid file"
    pid = int(open(pid_path).read())

    dead = False
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead = True
            break
        await asyncio.sleep(0.05)
    assert dead, "subprocess still running after the stream was abandoned"


async def test_spawn_failure_raises_provider_error(tmp_path):
    provider = _provider("/nonexistent/claude")
    with pytest.raises(ProviderError, match="spawn failed"):
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )


async def test_env_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak-either")
    binary = _fake_claude(tmp_path, ENV_DUMP)
    provider = _provider(binary)
    await collect(
        provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
    )
    keys = set(json.loads(open(binary + ".env").read()))
    assert "ANTHROPIC_API_KEY" not in keys
    assert "ANTHROPIC_AUTH_TOKEN" not in keys
    assert "ANTHROPIC_BASE_URL" not in keys
    assert "CLAUDE_CODE_USE_BEDROCK" not in keys
    assert "CLAUDE_CODE_USE_VERTEX" not in keys
    assert "OPENAI_API_KEY" not in keys
    assert "GEMINI_API_KEY" not in keys
    assert "PATH" in keys


async def test_contextvar_dispatch_overrides_bound_dispatcher(tmp_path, monkeypatch):
    binary = _fake_claude(tmp_path, HAPPY)
    provider = _provider(binary)  # bound to recorder A via the _Dispatcher shim

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

    monkeypatch.setattr("harness.provider_claude_code.McpToolServer", _FakeMcpToolServer)

    async def recorder_b(call):
        return ToolOutcome(text="", blob=None, is_error=False)

    token = current_dispatch_tool.set(recorder_b)
    try:
        await collect(
            provider.complete(model=ModelId("claude-code/default"), messages=USER, tools=())
        )
    finally:
        current_dispatch_tool.reset(token)

    assert captured["dispatch"] is recorder_b
