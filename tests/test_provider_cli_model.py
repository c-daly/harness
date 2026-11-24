"""The inference transport calls a local executable with CLI-owned auth and bounded I/O."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from harness import provider_cli_model as adapter
from harness.errors import AuthFailed, ContextOverflow, MalformedStreamError, ProviderError
from harness.inference import InferenceRequest, infer
from harness.messages import Message, Role, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from harness.provider import Usage
from harness.tools import ToolSpec


AUTH = {"loggedIn": True, "apiProvider": "firstParty", "authMethod": "claude.ai",
        "subscriptionType": "max", "orgId": "ordinary-personal-plan-org"}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
          "result": "answer", "stop_reason": "end_turn",
          "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3}}
TOOL = ToolSpec("read", "Read text", {"type": "object", "properties": {"file": {"type": "string"}}})

FAKE = """
import json, os, stat, sys, time
from pathlib import Path

root = Path(__file__).parent
args = sys.argv[1:]
config = json.loads((root / "fixture.json").read_text())
if "auth" in args:
    print(json.dumps(config["auth"]))
    sys.exit(config.get("auth_code", 0))
prompt = Path(args[args.index("--system-prompt-file") + 1])
trace = {"argv": args, "cwd": str(Path.cwd()), "system": prompt.read_text(),
         "mode": stat.S_IMODE(prompt.stat().st_mode), "request": json.load(sys.stdin),
         "env": {k: os.getenv(k) for k in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_MAX_RETRIES", "ENABLE_CLAUDEAI_MCP_SERVERS"
         )}}
(root / "trace.json").write_text(json.dumps(trace))
(root / "pid").write_text(str(os.getpid()))
if config.get("sleep"):
    time.sleep(60)
if "noise" in config:
    sys.stderr.write("x" * config["noise"])
print(config["stdout"] if "stdout" in config else json.dumps(config["result"]))
sys.exit(config.get("exit_code", 0))
"""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "sdk_available", lambda: False)
    monkeypatch.setattr(adapter, "_MACHINE_POLICY", tmp_path / "policy")
    binary = tmp_path / "claude"
    binary.write_text(f"#!{sys.executable}\n" + FAKE)
    binary.chmod(0o700)

    def configure(**options):
        (tmp_path / "fixture.json").write_text(json.dumps({"auth": AUTH, "result": RESULT, **options}))
        return adapter.ClaudeCliModelProvider(str(binary))

    return configure


def request(**kwargs):
    return InferenceRequest(**{"model": "claude-cli/default", "purpose": "chat",
        "messages": (Message.system_text("trusted-private"), Message.user_text("private-question")),
        **kwargs})


@pytest.mark.parametrize("model", ["default", "sonnet", "sonnet[1m]", "a" * 181])
async def test_subscription_plain_text_and_isolated_invocation(cli, tmp_path, monkeypatch, model):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK"):
        monkeypatch.setenv(name, "private-value")
    result = await infer(cli(), request(model=f"claude-cli/{model}", max_output_tokens=512))
    assert (result.message.text(), result.stop_reason) == ("answer", "end_turn")
    assert result.usage == Usage(input_tokens=10, output_tokens=4, cache_read_tokens=3)
    trace = json.loads((tmp_path / "trace.json").read_text())
    argv = trace["argv"]
    for flag in ("-p", "--restricted", "--safe-mode", "--no-chrome", "--strict-mcp-config",
                 "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv
    for key, value in {"--tools": "", "--disallowedTools": "mcp__*", "--permission-mode": "dontAsk",
                       "--permission-prompts": "none", "--mcp-config": '{"mcpServers":{}}',
                       "--setting-sources": "", "--max-turns": "1"}.items():
        assert argv[argv.index(key) + 1] == value
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["fallbackModel"] == [] and settings["disableClaudeAiConnectors"] is True
    assert "--fallback-model" not in argv
    if model == "default":
        assert "--model" not in argv
    else:
        assert argv[argv.index("--model") + 1] == model
    assert trace["env"] == {"ANTHROPIC_API_KEY": None, "ANTHROPIC_BASE_URL": None,
                            "CLAUDE_CODE_USE_BEDROCK": None, "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
                            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "512", "CLAUDE_CODE_MAX_RETRIES": "0"}
    assert all("private" not in arg for arg in argv)
    assert "trusted-private" in trace["system"]
    assert "trusted-private" not in json.dumps(trace["request"])
    assert trace["request"]["messages"][0]["blocks"][0]["text"] == "private-question"
    assert trace["request"]["tools"] == []
    assert trace["mode"] == 0o600 and not Path(trace["cwd"]).exists()


@pytest.mark.parametrize(("status", "code"), [
    ({"loggedIn": False}, 1),
    ({**AUTH, "authMethod": "api-key"}, 0),
    ({**AUTH, "apiProvider": "bedrock"}, 0),
    ({**AUTH, "subscriptionType": None}, 0),
])
async def test_wrong_credentials_stop_before_prompt(cli, tmp_path, status, code):
    with pytest.raises(AuthFailed, match="claude auth login"):
        await infer(cli(auth=status, auth_code=code), request())
    assert not (tmp_path / "pid").exists()


async def test_history_uses_only_portable_content(cli, tmp_path):
    history = (Message.system_text("Saoirse private instructions"),
               Message.user_text("Inspect this"),
               Message(role=Role.ASSISTANT, provider_extras={"private-vendor": "metadata"}, blocks=(
                   ThinkingBlock(text="private-reasoning", provider_extras={"signature": "private-signature"}),
                   TextBlock(text="Checking", provider_extras={"private-vendor": "more metadata"}),
                   ToolCallBlock(call_id="c1", tool="read", args={"file": "a"}),
               )),
               Message(role=Role.TOOL, blocks=(ToolResultBlock(call_id="c1", text="evidence",
                                       provider_extras={"private-tool-metadata": True}),)),
               Message(role=Role.ASSISTANT, blocks=(ThinkingBlock(text="private-reasoning"),)))
    await infer(cli(), request(messages=history))
    trace = json.loads((tmp_path / "trace.json").read_text())
    assert "private" not in json.dumps(trace["request"])
    assert trace["request"]["messages"] == [
        {"role": "user", "blocks": [{"kind": "text", "text": "Inspect this"}]},
        {"role": "assistant", "blocks": [{"kind": "text", "text": "Checking"},
                   {"kind": "tool_call", "call_id": "c1", "tool": "read", "args": {"file": "a"}}]},
        {"role": "tool", "blocks": [{"kind": "tool_result", "call_id": "c1", "text": "evidence",
                                     "is_error": False}]},
    ]
    assert "private-reasoning" not in trace["system"]


async def test_host_tool_proposals_have_fresh_ids_and_typed_args(cli, tmp_path):
    body = {"text": "Inspecting", "tool_calls": [{"name": "read", "args": {"file": "a"}}] * 2}
    result = await infer(cli(result={**RESULT, "result": json.dumps(body)}),
                         request(tools=(TOOL,), tool_choice="auto"))
    assert result.stop_reason == "tool_use" and result.message.text() == "Inspecting"
    first, second = result.message.tool_calls()
    assert first.tool == "read" and first.args == {"file": "a"} and first.call_id != second.call_id
    trace = json.loads((tmp_path / "trace.json").read_text())["request"]
    assert trace["tools"] == [{"name": TOOL.name, "description": TOOL.description, "parameters": TOOL.parameters}]
    assert "tool_calls" in trace["output"]["format"]["required"]
    body["tool_calls"][0]["name"] = "unadvertised"
    with pytest.raises(MalformedStreamError, match="unadvertised tool"):
        await infer(cli(result={**RESULT, "result": json.dumps(body)}),
                    request(tools=(TOOL,), tool_choice="auto"))


async def test_schema_and_truncation_are_honored(cli):
    schema = {"type": "integer"}
    result = await infer(cli(result={**RESULT, "result": "42"}), request(response_schema=schema))
    assert result.structured == 42
    with pytest.raises(MalformedStreamError, match="schema"):
        await infer(cli(), request(response_schema=schema))
    partial = {**RESULT, "result": "fragment", "stop_reason": "max_tokens"}
    result = await infer(cli(result=partial), request())
    assert (result.stop_reason, result.message.text()) == ("max_tokens", "fragment")
    result = await infer(cli(result=partial), request(tools=(TOOL,), tool_choice="auto"))
    assert result.stop_reason == "max_tokens" and not result.message.tool_calls()


@pytest.mark.parametrize("changes", [
    {"result": {**RESULT, "num_turns": 2}}, {"result": {**RESULT, "permission_denials": ["PRIVATE"]}},
    {"result": {**RESULT, "stop_reason": "tool_use"}}, {"stdout": "PRIVATE-DIAGNOSTIC"},
    {"result": {**RESULT, "usage": {"output_tokens": -2}}},
])
async def test_unexpected_cli_output_fails_without_reflecting_values(cli, changes):
    with pytest.raises(ProviderError) as error:
        await infer(cli(**changes), request())
    assert "PRIVATE" not in str(error.value)


async def test_failed_call_preserves_reported_usage_and_is_not_retried(cli):
    observed = []
    with pytest.raises(ProviderError, match="one isolated"):
        await infer(cli(exit_code=1), request(), on_usage=observed.append)
    assert observed == [Usage(input_tokens=10, output_tokens=4, cache_read_tokens=3)]
    unknown = await infer(cli(result={**RESULT, "usage": {}}), request())
    assert unknown.usage == Usage()


async def test_byte_and_token_budgets(cli, tmp_path):
    with pytest.raises(ContextOverflow):
        await infer(cli(), request(max_input_bytes=300))
    assert not (tmp_path / "pid").exists()
    with pytest.raises(ProviderError, match="transport bounds"):
        await infer(cli(noise=131072), request(max_output_bytes=1024))
    with pytest.raises(ProviderError, match="requested token limit"):
        await infer(cli(), request(max_output_tokens=2))


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
async def test_sleeping_cli_is_reaped(cli, tmp_path, cancel):
    deadline = 5 if cancel else 1
    task = asyncio.create_task(infer(cli(sleep=True), request(timeout_seconds=deadline)))
    async with asyncio.timeout(3):
        while not (tmp_path / "pid").exists():
            await asyncio.sleep(0.005)
    pid = int((tmp_path / "pid").read_text())
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("case", ["fifo", "oversize", "hooks", "split-plugin"])
async def test_unusable_or_conflicting_managed_settings_fail_before_cli(cli, tmp_path, case):
    directory = tmp_path / "policy"
    directory.mkdir()
    policy = directory / "managed-settings.json"
    if case == "fifo":
        os.mkfifo(policy)
    elif case == "oversize":
        policy.write_bytes(b"{}" + b" " * (2 * 1024 * 1024))
    elif case == "hooks":
        policy.write_text('{"hooks": {"SessionStart": ["PRIVATE"]}}')
    else:
        fragment = directory / "managed-settings.d"
        fragment.mkdir()
        (fragment / "plugin.json").write_text('{"disableAllHooks":true,"enabledPlugins":{"PRIVATE":true}}')
    with pytest.raises(ProviderError, match="managed") as error:
        await infer(cli(), request())
    assert "PRIVATE" not in str(error.value)
    assert not (tmp_path / "pid").exists()
