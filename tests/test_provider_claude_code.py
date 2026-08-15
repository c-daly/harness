"""ClaudeCodeProvider against fake `claude` executables emitting captured
stream-json shapes (CC v2.1.233). No real CLI, no subscription use."""

import json
import stat

import pytest

from harness.dispatcher import ToolOutcome
from harness.errors import ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import collect
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
