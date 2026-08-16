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
        # Snapshot the cwd argument's on-disk state AT CALL TIME, before the
        # harness's post-turn cleanup can remove it: the test reads this back
        # instead of stat-ing the (by-then-deleted) path itself.
        cwd_arg = msg["params"]["arguments"].get("cwd")
        cwd_ok = bool(cwd_arg) and os.path.isdir(cwd_arg) and not os.listdir(cwd_arg)
        open(sys.argv[0] + ".cwdcheck", "w").write(json.dumps(cwd_ok))
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


async def test_call_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    binary = _fake(tmp_path, FAKE_CODEX)
    provider = _provider(binary)
    await collect(provider.complete(model=ModelId("codex/default"), messages=USER, tools=()))
    argv = json.loads(open(binary + ".argv").read())
    assert argv == ["mcp-server"]  # no spawn-time -c override; injection travels per-call
    call = json.loads(open(binary + ".call").read())
    assert call["name"] == "codex"
    args = call["arguments"]
    assert args["sandbox"] == "read-only"
    assert args["approval-policy"] == "never"
    assert "say pong" in args["prompt"]
    assert "model" not in args  # "default" suffix means no override
    url = args["config"]["mcp_servers"]["harness"]["url"]
    assert url.startswith("http://127.0.0.1")  # harness's per-turn McpToolServer
    assert json.loads(open(binary + ".cwdcheck").read()) is True  # existing, empty scratch dir
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
