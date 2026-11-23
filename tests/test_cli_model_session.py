"""Exercise subscription inference through both entry points with a local fake CLI."""

import asyncio
import json
import sys
from uuid import uuid4

import pytest
from textual.widgets import RichLog

from harness import cli, provider_cli_model, provider_litellm
from harness.catalog import Catalog
from harness.tui import HarnessApp, HistoryInput


FAKE_CLAUDE = r'''
import json
import re
import sys
from pathlib import Path

argv = sys.argv[1:]
executable = Path(__file__)

def record(*fields):
    with executable.with_suffix(".calls").open("a") as log:
        print(json.dumps(fields), file=log)

if argv == ["-v"]:
    print("2.1.330")
    sys.exit(0)
if "auth" in argv and "status" in argv:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                      "subscriptionType": "max"}))
    sys.exit(0)

assert argv[argv.index("--tools") + 1] == ""
assert argv[argv.index("--model") + 1] == "sonnet"
output = argv[argv.index("--output-format") + 1]
if output == "stream-json":
    init = json.loads(sys.stdin.readline())
    assert init["request"]["subtype"] == "initialize"
    incompatible = executable.with_suffix(".incompatible").exists()
    response = {"subtype": "success", "request_id": init["request_id"], "response": {}}
    if incompatible:
        response.update(subtype="error", error="unsupported protocol version")
    record("initialize", response["subtype"])
    print(json.dumps({"type": "control_response", "response": response}), flush=True)
    if incompatible:
        for line in sys.stdin:
            if json.loads(line).get("type") == "user":
                record("unexpected-sdk-prompt")
        sys.exit(0)
    request = json.loads(json.loads(sys.stdin.readline())["message"]["content"])
else:
    assert output == "json"
    request = json.load(sys.stdin)
assert "Saoirse" in Path(argv[argv.index("--system-prompt-file") + 1]).read_text()
assert all(m["role"] != "system" for m in request["messages"])
assert "read_file" in [item["name"] for item in request["tools"]]
results = [block for message in request["messages"] if message["role"] == "tool"
           for block in message["blocks"] if block["kind"] == "tool_result"]
record("prompt", output, "answer" if results else "request-read")
if results:
    value = re.search(r"checkout=([0-9a-f]{32})", results[-1]["text"]).group(1)
    reply = {"text": value, "tool_calls": []}
else:
    reply = {"text": "", "tool_calls": [{"name": "read_file", "args": {"file_path": "note.txt"}}]}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                  "result": json.dumps(reply), "usage": {"input_tokens": 20, "output_tokens": 10},
                  "stop_reason": "end_turn", "duration_ms": 1, "duration_api_ms": 1,
                  "session_id": "test-session"}))
'''


@pytest.mark.parametrize("interactive", [False, True], ids=["headless", "interactive"])
@pytest.mark.parametrize("transport", ["direct", "sdk", "incompatible-sdk"])
def test_setup_cli_runs_saoirse_with_one_host_file_read(tmp_path, monkeypatch, capsys, interactive, transport):
    if transport == "direct":
        monkeypatch.setattr(provider_cli_model, "sdk_available", lambda: False)
    project = tmp_path / "work"
    project.mkdir()
    value = uuid4().hex
    (project / "note.txt").write_text(f"checkout={value}\n")
    home = tmp_path / "home"
    home.mkdir()
    fake_cli = tmp_path / "claude"
    fake_cli.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
    fake_cli.chmod(0o700)
    if transport == "incompatible-sdk":
        fake_cli.with_suffix(".incompatible").touch()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(provider_cli_model, "_MACHINE_POLICY", tmp_path / "managed-policy")
    monkeypatch.chdir(project)

    def unexpected_api_call(**kwargs):
        pytest.fail("CLI inference dispatched to the LiteLLM/API transport")

    monkeypatch.setattr(provider_litellm, "_acomplete", unexpected_api_call)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["harness", *map(str, args)])
        cli.main()

    run("setup", "cli", "subscription", "--model", "sonnet")
    run("setup", "agent", "external", "--backend", "claude-code")
    catalog = Catalog.load(home / ".config" / "harness" / "models.toml")
    assert catalog.resolve("subscription").execution_kind == "inference"
    assert catalog.resolve("external").execution_kind == "agent"
    capsys.readouterr()
    run("doctor", "--model", "subscription", "--json")
    checks = json.loads(capsys.readouterr().out)["checks"]
    detail = next(check["detail"] for check in checks if check["check"] == "transport")
    assert ("Direct Claude CLI available" if transport == "direct" else "Agent SDK detected") in detail
    store = tmp_path / "state"
    options = ["--base-dir", str(store), "--allow", "read_file", "--no-mcp", "--no-plugins"]
    prompt = "Read note.txt and return the checkout value."
    if interactive:
        async def drive_terminal(app):
            async with app.run_test() as pilot:
                field = app.query_one("#prompt", HistoryInput)
                field.focus()
                field.value = prompt
                await pilot.press("enter")
                async with asyncio.timeout(10):
                    # The tool receipt can display the value before the final model turn.
                    while app._turn_worker is not None or value not in "\n".join(
                            str(line) for line in app.query_one(RichLog).lines):
                        await pilot.pause(0.025)

        monkeypatch.setattr(HarnessApp, "run_async", drive_terminal)
        run(*options)
    else:
        run(*options, "-p", prompt)
        assert capsys.readouterr().out.strip() == value

    trace = [json.loads(line) for line in fake_cli.with_suffix(".calls").read_text().splitlines()]
    expected = []
    for phase in ("request-read", "answer"):
        if transport != "direct":
            expected.append(["initialize", "error" if transport == "incompatible-sdk" else "success"])
        expected.append(["prompt", "stream-json" if transport == "sdk" else "json", phase])
    assert trace == expected
    (log,) = (store / "sessions").glob("*.jsonl")
    events = [json.loads(line)["event"] for line in log.read_text().splitlines()]
    assert [e["runtime"] for e in events if e["type"] == "agent_run_started"] == ["harness"]
    calls = [e for e in events if e["type"] == "model_call_completed"]
    assert [(e["model"], e["execution_kind"]) for e in calls] == [("subscription", "inference")] * 2
    reads = [e for e in events if e["type"] == "dispatch_resolved" and e["kind"] == "tool"]
    assert [(e["tool"], e["args"]) for e in reads] == [("read_file", {"file_path": str(project / "note.txt")})]
    (receipt,) = [e for e in events if e["type"] == "tool_call_completed"]
    assert receipt["call_id"] == reads[0]["call_id"] and not receipt["is_error"]
    assert value in receipt["result_text"]
