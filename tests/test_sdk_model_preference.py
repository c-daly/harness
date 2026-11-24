"""The real Claude Agent SDK and the direct CLI share one subscription model route."""

import asyncio
import fcntl
import json
import sys

import pytest

from harness import provider_cli_model, provider_litellm
from harness.catalog import Catalog
from harness.errors import AuthFailed, ProviderError
from harness.inference import InferenceRequest, infer
from harness.messages import Message
from harness.tools import ToolSpec


FAKE_CLAUDE = r'''
import json
import os
import sys
from pathlib import Path

root = Path(__file__).parent
mode = (root / "mode").read_text()
args = sys.argv[1:]


def record(event, **details):
    with (root / "events.jsonl").open("a") as log:
        log.write(json.dumps({"event": event, **details}) + "\n")


def send(message):
    print(json.dumps(message), flush=True)


if "auth" in args:
    record("auth")
    send({"loggedIn": True, "apiProvider": "firstParty", "subscriptionType": "max",
          "authMethod": "api-key" if mode == "wrong-account" else "claude.ai"})
    sys.exit()
if args == ["-v"]:
    print("2.1.330")
    sys.exit()

output = args[args.index("--output-format") + 1]


def answer(request):
    record("prompt", transport=output, model=args[args.index("--model") + 1],
           tools=args[args.index("--tools") + 1],
           max_turns=args[args.index("--max-turns") + 1],
           strict_mcp="--strict-mcp-config" in args,
           extra_secrets=any(k in os.environ for k in
                             ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK")),
           subscription=os.getenv("CLAUDE_CODE_OAUTH_TOKEN") == "fixture-account",
           system=Path(args[args.index("--system-prompt-file") + 1]).read_text(),
           request=request)
    if mode == "drop-after-send":
        sys.exit(1)
    if mode == "wait-after-send":
        import fcntl
        import time
        with (root / "active-request").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record("waiting")
            time.sleep(60)
    proposal = {"text": "Checking", "tool_calls": [{"name": "read_file", "args": {"file": "note"}}]}
    send({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
          "is_error": False, "num_turns": 1, "session_id": "fixture", "stop_reason": "end_turn",
          "usage": {"input_tokens": 8, "output_tokens": 5}, "result": json.dumps(proposal),
          "permission_denials": [{"tool_name": "SomeTool"}] if mode == "tool-denied" else []})


if output == "json":
    answer(json.load(sys.stdin))
elif output == "stream-json":
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["type"] == "control_request":
            assert msg["request"]["subtype"] == "initialize"
            record("sdk-initialize")
            response = {"subtype": "success", "request_id": msg["request_id"], "response": {}}
            if mode == "incompatible":
                response.update(subtype="error", error="unsupported protocol version")
            elif mode == "policy-denied":
                response.update(subtype="error", error="access denied by policy")
            send({"type": "control_response", "response": response})
        elif msg["type"] == "user":
            answer(json.loads(msg["message"]["content"]))
            break
else:
    raise AssertionError(output)
'''


@pytest.fixture
def subscription(tmp_path, monkeypatch):
    executable = tmp_path / "claude"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fixture-account")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK"):
        monkeypatch.setenv(name, "unexpected-credential-or-provider")
    monkeypatch.setattr(provider_cli_model, "_MACHINE_POLICY", tmp_path / "managed")

    def reject_metered_api(**kwargs):
        pytest.fail("Subscription model dispatched to the metered API")

    monkeypatch.setattr(provider_litellm, "_acomplete", reject_metered_api)
    provider = provider_litellm.CatalogProvider(
        Catalog({"desk": {"transport": "claude-cli", "route": "claude-cli/sonnet"}})
    )
    request = InferenceRequest(
        model="desk", purpose="chat", timeout_seconds=10,
        messages=(Message.system_text("Saoirse fixture identity"), Message.user_text("inspect note")),
        tools=(ToolSpec("read_file", "Read a file", {"type": "object"}),), tool_choice="auto",
    )

    async def run(mode):
        (tmp_path / "mode").write_text(mode)
        return await infer(provider, request)

    def events():
        return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]

    return run, events


@pytest.mark.parametrize(("mode", "stages", "transport"), [
    ("available", ["auth", "sdk-initialize", "prompt"], "stream-json"),
    ("incompatible", ["auth", "sdk-initialize", "prompt"], "json"),
    ("missing", ["auth", "prompt"], "json"),
])
async def test_selects_one_transport_for_subscription_request(subscription, monkeypatch, mode, stages, transport):
    if mode == "missing":
        monkeypatch.setattr(provider_cli_model, "sdk_available", lambda: False)
    run, events = subscription
    result = await run(mode)
    trace = events()
    assert [event["event"] for event in trace] == stages
    prompt = trace[-1]
    assert prompt["transport"] == transport and prompt["model"] == "sonnet"
    assert prompt["tools"] == "" and prompt["max_turns"] == "1" and prompt["strict_mcp"]
    assert prompt["subscription"] and not prompt["extra_secrets"]
    assert "Saoirse fixture identity" in prompt["system"]
    assert [m["role"] for m in prompt["request"]["messages"]] == ["user"]
    assert [t["name"] for t in prompt["request"]["tools"]] == ["read_file"]
    proposal, = result.message.tool_calls()
    assert proposal.tool == "read_file" and proposal.args == {"file": "note"}
    assert result.usage.output_tokens == 5


@pytest.mark.parametrize(("mode", "stages"), [
    ("wrong-account", ["auth"]),
    ("policy-denied", ["auth", "sdk-initialize"]),
    ("drop-after-send", ["auth", "sdk-initialize", "prompt"]),
    ("tool-denied", ["auth", "sdk-initialize", "prompt"]),
])
async def test_no_replay_on_denial_or_uncertain_model_outcome(subscription, mode, stages):
    run, events = subscription
    with pytest.raises(AuthFailed if mode == "wrong-account" else ProviderError):
        await run(mode)
    trace = events()
    assert [event["event"] for event in trace] == stages
    assert all(event["transport"] == "stream-json" for event in trace if event["event"] == "prompt")


async def test_cancelling_sdk_request_stops_vendor_process(subscription, tmp_path):
    run, events = subscription
    task = asyncio.create_task(run("wait-after-send"))
    try:
        async with asyncio.timeout(8):
            while not (tmp_path / "active-request").exists() or events()[-1]["event"] != "waiting":
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The CLI holds this lock throughout generation. A surviving SDK child would still own it.
    with (tmp_path / "active-request").open() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert [event["event"] for event in events()] == ["auth", "sdk-initialize", "prompt", "waiting"]
