"""AntigravityProvider against fake `agy` executables replaying the
`agy -p ... --output-format stream-json` event stream shapes documented in
docs/superpowers/specs/2026-08-28-antigravity-gemini-backend-design.md
(live-probed against agy 1.1.22). No real CLI, no subscription use."""

import asyncio
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from harness.dispatcher import ToolOutcome, current_dispatch_tool
from harness.errors import MalformedStreamError, ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import TextDelta, ThinkingDelta, collect
from harness.provider_antigravity import AntigravityProvider
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName

# Every fake asserts the mcp-add / turn invocation shapes up front, then dumps
# what the provider handed it (argv, stdin, env, cwd state, HOME state) as
# sidecar files next to the binary -- never inside cwd, which the flag
# contract test asserts is empty. The same binary path serves both the
# `agy mcp add ...` registration call and the turn call: it branches on
# sys.argv[1] == "mcp".
_PREAMBLE = """
import json, os, sys

_here = sys.argv[0]

if len(sys.argv) > 1 and sys.argv[1] == "mcp":
    assert sys.argv[1:3] == ["mcp", "add"], sys.argv
    open(_here + ".mcpargv", "w").write(json.dumps(sys.argv[1:]))
    open(_here + ".mcpenv", "w").write(json.dumps(sorted(os.environ.keys())))
    open(_here + ".mcphome", "w").write(os.environ.get("HOME") or "")
    sys.exit(0)

assert "-p" in sys.argv, sys.argv
assert sys.argv[sys.argv.index("-p") + 1], sys.argv  # non-empty orientation prefix
assert "--output-format" in sys.argv, sys.argv
assert sys.argv[sys.argv.index("--output-format") + 1] == "stream-json", sys.argv
assert "--dangerously-skip-permissions" in sys.argv, sys.argv
assert "--print-timeout" in sys.argv, sys.argv

_prompt = sys.stdin.read()
_cwd = os.getcwd()
_home = os.environ.get("HOME")

_AUTH_FILES = {
    ".gemini": ["oauth_creds.json", "google_accounts.json", "installation_id",
                "settings.json", "projects.json", "state.json"],
    ".gemini/antigravity-cli": ["antigravity-oauth-token", "installation_id", "settings.json"],
}


def _auth_snapshot(home):
    out = {}
    if not home:
        return out
    for rel_dir, names in _AUTH_FILES.items():
        for name in names:
            out[rel_dir + "/" + name] = os.path.isfile(os.path.join(home, rel_dir, name))
    return out


open(_here + ".argv", "w").write(json.dumps(sys.argv[1:]))
open(_here + ".stdin", "w").write(_prompt)
open(_here + ".env", "w").write(json.dumps(sorted(os.environ.keys())))
open(_here + ".cwd", "w").write(json.dumps({"path": _cwd, "empty": not os.listdir(_cwd)}))
open(_here + ".home", "w").write(json.dumps({"value": _home, "files": _auth_snapshot(_home)}))


def emit(event):
    print(json.dumps(event), flush=True)
"""

# Event-mapping contract, pinned to the real wire shape live-verified in
# Task 1 against agy 1.1.22: every line is {"event": "<kind>", "<kind>":
# {...payload...}} -- the discriminator names its own nested payload key.
# There is no flat top-level "type" field.
HAPPY = _PREAMBLE + """
emit({"event": "init", "conversation_id": "sess-1",
      "init": {"cwd": _cwd, "tools": [], "permission_mode": "always-proceed"}})
emit({"event": "step_update", "step_update": {
      "step_index": 0, "state": "DONE", "step_type": "user_input"}})
emit({"event": "step_update", "step_update": {
      "step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "echo-"}})
emit({"event": "step_update", "step_update": {
      "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "result-42",
      "duration_seconds": 0.5, "usage": {"input_tokens": 100, "output_tokens": 20}}})
emit({"event": "result", "result": {"status": "SUCCESS", "response": "echo-result-42",
      "usage": {"input_tokens": 5821, "output_tokens": 233, "thinking_tokens": 40}}})
"""

# A tool step interleaved with agent_response text: proves the parser
# branches on step_type, not merely on text_delta presence -- the tool step
# below carries a text_delta on purpose and must still be ignored.
WITH_TOOL_STEP = _PREAMBLE + """
emit({"event": "init", "conversation_id": "sess-2",
      "init": {"cwd": _cwd, "tools": [], "permission_mode": "always-proceed"}})
emit({"event": "step_update", "step_update": {
      "step_index": 0, "state": "DONE", "step_type": "tool", "tool_name": "read_file",
      "text_delta": "should never surface"}})
emit({"event": "step_update", "step_update": {
      "step_index": 1, "state": "DONE", "step_type": "agent_response",
      "text_delta": "echo-result-42"}})
emit({"event": "result", "result": {"status": "SUCCESS",
      "usage": {"input_tokens": 10, "output_tokens": 2}}})
"""

WITH_NOISE = _PREAMBLE + """
print("not json, just noise", flush=True)
emit({"event": "step_update", "step_update": {
      "step_index": 0, "state": "DONE", "step_type": "agent_response",
      "text_delta": "echo-result-42"}})
print("more noise {broken", flush=True)
emit({"event": "result", "result": {"status": "SUCCESS",
      "usage": {"input_tokens": 1, "output_tokens": 1}}})
"""

ERRORED = _PREAMBLE + """
emit({"event": "init", "conversation_id": "sess-3",
      "init": {"cwd": _cwd, "tools": [], "permission_mode": "always-proceed"}})
emit({"event": "result", "result": {"status": "ERROR",
      "error": "quota exceeded for gemini-3.7-flash-high"}})
"""

NO_RESULT = _PREAMBLE + """
emit({"event": "init", "conversation_id": "sess-4",
      "init": {"cwd": _cwd, "tools": [], "permission_mode": "always-proceed"}})
emit({"event": "step_update", "step_update": {
      "step_index": 0, "state": "ACTIVE", "step_type": "agent_response",
      "text_delta": "half a turn"}})
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
emit({"event": "step_update", "step_update": {
      "step_index": 0, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "partial"}})
time.sleep(60)
"""

# `agy mcp add` rejects; the turn body is never reached in this test, so it
# is omitted entirely.
MCP_ADD_FAILS = """
import sys

if len(sys.argv) > 1 and sys.argv[1] == "mcp":
    sys.stderr.write("boom: mcp add rejected")
    sys.exit(3)

sys.exit(0)
"""

# Byte-for-byte JSONL captured from Task 1's own mandatory live verification
# turn against real agy 1.1.22 (one real McpToolServer, a stub `read_file`
# dispatch, real Gemini OAuth). Recorded here as the regression pin for the
# nested {"event": ..., "<kind>": {...}} envelope discovered live -- this
# transcript is what first proved the flat-"type" assumption wrong. The
# "tools" list in the init event is trimmed for file-size; everything the
# parser actually touches (step_update / result payloads) is unmodified.
# It exercises: step_index reuse across an ACTIVE/DONE pair, an unrecognized
# step_type ("unknown"), five separate tool round trips (including the real
# call_mcp_tool -> harness/read_file hop whose output is this test's stub
# marker), agent_response steps with no text_delta at all (pure thinking),
# and a text_delta split across three fragments spanning ACTIVE then DONE.
_REAL_LINES = [
    {"event": "init", "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
     "init": {"cwd": "/tmp/raw-probe-cwd-jjg7qseu", "tools": ["read_file"],
              "permission_mode": "always-proceed"}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 0, "state": "DONE", "step_type": "user_input"}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 1, "state": "DONE", "step_type": "agent_response",
        "duration_seconds": 1.517900745,
        "usage": {"input_tokens": 14757, "output_tokens": 224, "thinking_tokens": 156,
                  "cache_read_tokens": 0, "total_tokens": 14981}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "list_dir",
        "tool_info": {"name": "list_dir", "parameters": {
            "DirectoryPath": "/tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness"}}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "list_dir",
        "duration_seconds": 0.032312074,
        "tool_info": {"name": "list_dir", "parameters": {
            "DirectoryPath": "/tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness"},
            "output": "read_file.json"}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 3, "state": "DONE", "step_type": "agent_response",
        "duration_seconds": 2.190063457,
        "usage": {"input_tokens": 15086, "output_tokens": 122, "thinking_tokens": 49,
                  "cache_read_tokens": 0, "total_tokens": 15208}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 4, "state": "ACTIVE", "step_type": "tool", "tool_name": "view_file",
        "tool_info": {"name": "view_file", "parameters": {
            "AbsolutePath": "/tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json"}}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 4, "state": "DONE", "step_type": "tool", "tool_name": "view_file",
        "duration_seconds": 0.0298935,
        "tool_info": {"name": "view_file", "parameters": {
            "AbsolutePath": "/tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json"},
            "output": "1 lines, 228 bytes"}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 5, "state": "DONE", "step_type": "agent_response",
        "duration_seconds": 4.904435615,
        "usage": {"input_tokens": 15470, "output_tokens": 143, "thinking_tokens": 106,
                  "cache_read_tokens": 0, "total_tokens": 15613}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 6, "state": "DONE", "step_type": "unknown", "duration_seconds": 0.025765039}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 7, "state": "DONE", "step_type": "agent_response",
        "duration_seconds": 0.830305031,
        "usage": {"input_tokens": 15694, "output_tokens": 90, "thinking_tokens": 39,
                  "cache_read_tokens": 0, "total_tokens": 15784}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 8, "state": "ACTIVE", "step_type": "tool", "tool_name": "list_dir",
        "tool_info": {"name": "list_dir",
                      "parameters": {"DirectoryPath": "/tmp/harness-antigravity-home-jxn4vxcy"}}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 8, "state": "DONE", "step_type": "tool", "tool_name": "list_dir",
        "duration_seconds": 0.028318844,
        "tool_info": {"name": "list_dir",
                      "parameters": {"DirectoryPath": "/tmp/harness-antigravity-home-jxn4vxcy"},
                      "output": ".cache/\n.gemini/"}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 9, "state": "DONE", "step_type": "agent_response",
        "duration_seconds": 0.926686187,
        "usage": {"input_tokens": 15898, "output_tokens": 116, "thinking_tokens": 56,
                  "cache_read_tokens": 0, "total_tokens": 16014}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 10, "state": "ACTIVE", "step_type": "tool", "tool_name": "find_by_name",
        "tool_info": {"name": "find_by_name", "parameters": {
            "Pattern": "*", "SearchDirectory": "/tmp/harness-antigravity-home-jxn4vxcy"}}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 10, "state": "DONE", "step_type": "tool", "tool_name": "find_by_name",
        "duration_seconds": 0.027507245,
        "tool_info": {"name": "find_by_name", "parameters": {
            "Pattern": "*", "SearchDirectory": "/tmp/harness-antigravity-home-jxn4vxcy"}}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 11, "state": "DONE", "step_type": "agent_response",
        "duration_seconds": 1.039234239,
        "usage": {"input_tokens": 16093, "output_tokens": 232, "thinking_tokens": 135,
                  "cache_read_tokens": 0, "total_tokens": 16325}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 12, "state": "ACTIVE", "step_type": "tool", "tool_name": "call_mcp_tool",
        "tool_info": {"name": "call_mcp_tool", "parameters": {
            "Arguments": {"path": "/tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json"},
            "ServerName": "harness", "ToolName": "read_file"}}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 12, "state": "DONE", "step_type": "tool", "tool_name": "call_mcp_tool",
        "duration_seconds": 0.026462326,
        "tool_info": {"name": "call_mcp_tool", "parameters": {
            "Arguments": {"path": "/tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json"},
            "ServerName": "harness", "ToolName": "read_file"},
            "output": "LIVE_PROBE_MARKER_9f3a21"}}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 13, "state": "ACTIVE", "step_type": "agent_response",
        "text_delta": "I have completed the orientation with the `harness` MCP server:\n\n"
                       "1. **Schema Inspect"}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 13, "state": "ACTIVE", "step_type": "agent_response",
        "text_delta": "ed**: Reviewed [read_file.json]"
                       "(file:///tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json), "
                       "which defines the lazy-loaded `read_file` tool requiring a `path` string parameter.\n2"}},
    {"event": "step_update", "step_update": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b",
        "step_index": 13, "state": "DONE", "step_type": "agent_response",
        "text_delta": ". **Tool Invocation**: Executed `call_mcp_tool` targeting `harness` / `read_file`:\n"
                       "   - **Target**: [read_file.json]"
                       "(file:///tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json)\n"
                       "   - **Result**: `LIVE_PROBE_MARKER_9f3a21`\n",
        "duration_seconds": 2.045521059,
        "usage": {"input_tokens": 16415, "output_tokens": 482, "thinking_tokens": 288,
                  "cache_read_tokens": 0, "total_tokens": 16897}}},
    {"event": "result", "result": {
        "conversation_id": "963e8c0c-4a00-4d14-9be6-58ceb3e0b98b", "status": "SUCCESS",
        "response": "I have completed the orientation with the `harness` MCP server:\n\n"
                    "1. **Schema Inspected**: Reviewed [read_file.json]"
                    "(file:///tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json), "
                    "which defines the lazy-loaded `read_file` tool requiring a `path` string parameter.\n"
                    "2. **Tool Invocation**: Executed `call_mcp_tool` targeting `harness` / `read_file`:\n"
                    "   - **Target**: [read_file.json]"
                    "(file:///tmp/harness-antigravity-home-jxn4vxcy/.gemini/antigravity-cli/mcp/harness/read_file.json)\n"
                    "   - **Result**: `LIVE_PROBE_MARKER_9f3a21`\n",
        "duration_seconds": 13.514214797, "num_turns": 1,
        "usage": {"input_tokens": 109413, "output_tokens": 1409, "thinking_tokens": 829,
                  "cache_read_tokens": 0, "total_tokens": 110822}}},
]

REAL_TRANSCRIPT_REPLAY = _PREAMBLE + f"""
import json as _json
for _event in _json.loads({json.dumps(_REAL_LINES)!r}):
    print(_json.dumps(_event), flush=True)
"""

USER = [Message(role=Role.USER, blocks=(TextBlock(text="say pong"),))]

# Captured at import time, before the autouse fixture below ever runs, so the
# one live test can restore real auth after that fixture points HOME at an
# empty scratch dir for every other test in this file.
_REAL_HOME = os.environ.get("HOME", str(Path.home()))


@pytest.fixture(autouse=True)
def _isolated_gemini_home(tmp_path, monkeypatch):
    """Every AntigravityProvider turn copies auth files out of ~/.gemini
    (Path.home(), driven by $HOME) into a fresh scratch dir. Default HOME to
    an empty directory with no .gemini so tests never touch real Google
    credentials on this machine as a side effect; test_flag_contract
    overrides this to exercise the copy explicitly."""
    empty = tmp_path / "no-gemini-home"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(empty))


def _fake_agy(tmp_path, script_body: str) -> str:
    path = tmp_path / "agy"
    path.write_text("#!/usr/bin/env python3\n" + script_body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class _Dispatcher:
    async def dispatch_tool(self, call):  # never called in these tests
        return ToolOutcome(text="", blob=None, is_error=False)


def _provider(binary: str, timeout_s: float = 30.0) -> AntigravityProvider:
    p = AntigravityProvider(binary=binary, timeout_s=timeout_s)
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
    raise AssertionError("fake agy never wrote its pid file")


# --- (a)/(b): event mapping, happy path ---


async def test_text_deltas_yielded_in_order(tmp_path):
    provider = _provider(_fake_agy(tmp_path, HAPPY))
    chunks = [
        c
        async for c in provider.complete(
            model=ModelId("antigravity/default"), messages=USER, tools=()
        )
    ]
    text_chunks = [c for c in chunks if isinstance(c, TextDelta)]
    assert [c.text for c in text_chunks] == ["echo-", "result-42"]


async def test_happy_turn_maps_usage_and_stop(tmp_path):
    provider = _provider(_fake_agy(tmp_path, HAPPY))
    message, usage, stop = await collect(
        provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
    )
    assert message.text() == "echo-result-42"
    assert stop == "end_turn"
    assert usage.input_tokens == 5821
    assert usage.output_tokens == 233
    # agy's thinking_tokens is deliberately not folded in (harness Usage has no field for it)
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0


async def test_no_thinking_delta_ever_emitted(tmp_path):
    """v1 global constraint: agy exposes thinking token counts but never
    thought text, so this provider must never synthesize a ThinkingDelta."""
    provider = _provider(_fake_agy(tmp_path, WITH_TOOL_STEP))
    chunks = [
        c
        async for c in provider.complete(
            model=ModelId("antigravity/default"), messages=USER, tools=()
        )
    ]
    assert not any(isinstance(c, ThinkingDelta) for c in chunks)


# --- (c)/(d)/(e): failure and malformed-stream contracts ---


async def test_error_result_raises_provider_error(tmp_path):
    provider = _provider(_fake_agy(tmp_path, ERRORED))
    with pytest.raises(ProviderError, match="quota exceeded"):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )


async def test_eof_without_result_raises_malformed_stream_error(tmp_path):
    provider = _provider(_fake_agy(tmp_path, NO_RESULT))
    with pytest.raises(MalformedStreamError):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )


async def test_non_json_lines_skipped(tmp_path):
    provider = _provider(_fake_agy(tmp_path, WITH_NOISE))
    message, usage, stop = await collect(
        provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
    )
    assert message.text() == "echo-result-42"
    assert stop == "end_turn"


async def test_crash_raises_provider_error_with_stderr(tmp_path):
    provider = _provider(_fake_agy(tmp_path, CRASH))
    with pytest.raises(ProviderError, match="boom"):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )


# --- (f): tool steps are informational only ---


async def test_tool_step_yields_no_text(tmp_path):
    provider = _provider(_fake_agy(tmp_path, WITH_TOOL_STEP))
    chunks = [
        c
        async for c in provider.complete(
            model=ModelId("antigravity/default"), messages=USER, tools=()
        )
    ]
    text_chunks = [c for c in chunks if isinstance(c, TextDelta)]
    assert [c.text for c in text_chunks] == ["echo-result-42"]


# --- live-verification regression: exact transcript replay ---


async def test_real_transcript_replay_matches_live_verification(tmp_path):
    """Regression pin for Task 1's mandatory live verification turn (real agy
    1.1.22, real McpToolServer, stub read_file dispatch): replays the exact
    captured JSONL and asserts the assembled message equals agy's own
    result.response text, with the terminal (cumulative) usage figures --
    not any of the per-step usage noise along the way."""
    provider = _provider(_fake_agy(tmp_path, REAL_TRANSCRIPT_REPLAY))
    message, usage, stop = await collect(
        provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
    )
    assert message.text() == _REAL_LINES[-1]["result"]["response"]
    assert stop == "end_turn"
    assert usage.input_tokens == 109413
    assert usage.output_tokens == 1409


# --- (g)/(h)/(i)/(k): argv, stdin, env, cwd, and scratch-HOME wiring ---


async def test_flag_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # A fake user HOME with real-shaped auth files, so this test can prove
    # they get copied into the isolated scratch HOME the child actually sees.
    fake_user_home = tmp_path / "fake-user-gemini-home"
    (fake_user_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (fake_user_home / ".gemini" / "oauth_creds.json").write_text(json.dumps({"fake": "oauth"}))
    (fake_user_home / ".gemini" / "google_accounts.json").write_text("{}")
    (fake_user_home / ".gemini" / "installation_id").write_text("abc123")
    (fake_user_home / ".gemini" / "settings.json").write_text("{}")
    (fake_user_home / ".gemini" / "projects.json").write_text("{}")
    (fake_user_home / ".gemini" / "state.json").write_text("{}")
    (fake_user_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("tok")
    (fake_user_home / ".gemini" / "antigravity-cli" / "installation_id").write_text("abc123")
    (fake_user_home / ".gemini" / "antigravity-cli" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_user_home))

    binary = _fake_agy(tmp_path, HAPPY)
    provider = _provider(binary)
    await collect(provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=()))

    argv = json.loads(open(binary + ".argv").read())
    assert argv[0] == "-p"
    assert argv[1]  # non-empty orientation prefix
    assert "say pong" not in argv[1]  # transcript not smuggled into argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--print-timeout") + 1].endswith("s")  # Go duration syntax
    assert "--model" not in argv  # "default" suffix means no override

    stdin = open(binary + ".stdin").read()
    assert "say pong" in stdin
    # Regression pin for the live-verification bug fix: -p's value is
    # appended directly after stdin with no separator agy inserts itself, so
    # a trailing "[assistant]:" completion cue here would glue onto it and
    # agy would read the orientation text as an assistant turn already in
    # progress instead of addressing the user's request.
    assert not stdin.endswith("[assistant]:")

    cwd = json.loads(open(binary + ".cwd").read())
    assert cwd["empty"] is True  # a fresh, empty per-turn scratch dir
    assert cwd["path"] != os.getcwd()
    assert not os.path.exists(cwd["path"])  # torn down after the turn

    env_keys = set(json.loads(open(binary + ".env").read()))
    assert "GOOGLE_API_KEY" not in env_keys
    assert "OPENAI_API_KEY" not in env_keys
    assert "PATH" in env_keys
    assert "HOME" in env_keys

    home = json.loads(open(binary + ".home").read())
    assert home["value"] not in (None, str(fake_user_home))  # isolated scratch home
    assert all(home["files"].values()), home["files"]  # every auth file copied over
    assert not os.path.exists(home["value"])  # torn down after the turn


async def test_model_suffix_becomes_flag(tmp_path):
    binary = _fake_agy(tmp_path, HAPPY)
    provider = _provider(binary)
    await collect(
        provider.complete(
            model=ModelId("antigravity/gemini-3.7-flash-high"), messages=USER, tools=()
        )
    )
    argv = json.loads(open(binary + ".argv").read())
    assert argv[argv.index("--model") + 1] == "gemini-3.7-flash-high"


async def test_missing_auth_files_skipped_silently(tmp_path):
    """No .gemini directory at all in the source HOME (the autouse fixture's
    default): the turn must still run, with an empty-but-present scratch
    HOME rather than failing."""
    provider = _provider(_fake_agy(tmp_path, HAPPY))
    await collect(
        provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
    )


# --- (j): agy mcp add registration ---


async def test_mcp_registration_runs_before_turn_in_scratch_home(tmp_path):
    binary = _fake_agy(tmp_path, HAPPY)
    provider = _provider(binary)
    await collect(
        provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
    )

    mcpargv = json.loads(open(binary + ".mcpargv").read())
    assert mcpargv[:5] == ["mcp", "add", "-t", "http", "harness"]
    assert mcpargv[5].startswith("http://127.0.0.1")
    assert mcpargv[5].endswith("/")  # trailing slash, per spec

    mcp_home = open(binary + ".mcphome").read()
    turn_home = json.loads(open(binary + ".home").read())["value"]
    assert mcp_home == turn_home  # same scratch HOME for registration and the turn


async def test_mcp_registration_failure_raises_provider_error(tmp_path):
    provider = _provider(_fake_agy(tmp_path, MCP_ADD_FAILS))
    with pytest.raises(ProviderError, match="mcp add exited"):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )


# --- misc hygiene, mirroring the codex suite ---


async def test_missing_binary_raises_provider_error(tmp_path):
    provider = _provider("/nonexistent/agy")
    with pytest.raises(ProviderError, match="spawn failed"):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )


async def test_unbound_dispatcher_is_loud(tmp_path):
    provider = AntigravityProvider(binary=_fake_agy(tmp_path, HAPPY))  # no bind_dispatcher
    with pytest.raises(ProviderError, match="dispatcher"):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )


async def test_contextvar_dispatch_overrides_bound_dispatcher(tmp_path, monkeypatch):
    provider = _provider(_fake_agy(tmp_path, HAPPY))  # bound to the _Dispatcher shim

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

    monkeypatch.setattr("harness.provider_antigravity.McpToolServer", _FakeMcpToolServer)

    async def recorder_b(call):
        return ToolOutcome(text="", blob=None, is_error=False)

    token = current_dispatch_tool.set(recorder_b)
    try:
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )
    finally:
        current_dispatch_tool.reset(token)

    assert captured["dispatch"] is recorder_b


# --- (n): the codex-leak regression contract ---


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

    monkeypatch.setattr("harness.provider_antigravity.McpToolServer", _FakeMcpToolServer)

    def _boom(*args, **kwargs):
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)

    provider = _provider(_fake_agy(tmp_path, HAPPY))
    with pytest.raises(OSError, match="ENOSPC"):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )

    # The turn never even reached the point of touching disk beyond the
    # server: the failure must not hang or get swallowed, and the server
    # that was already started must still be torn down.
    assert events == ["start", "stop"]


# --- (l)/(m): process-group kill on timeout and on abandonment ---


async def test_timeout_kills_process_group(tmp_path):
    binary = _fake_agy(tmp_path, SLEEPER)
    provider = _provider(binary, timeout_s=1.0)
    with pytest.raises(ProviderError, match="timed out"):
        await collect(
            provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
        )
    pid = await _read_pid(binary + ".pid")
    assert await _wait_for_death(pid), "child survived the turn timeout"


async def test_abandoning_stream_kills_process_group(tmp_path):
    binary = _fake_agy(tmp_path, ONE_DELTA_THEN_SLEEP)
    provider = _provider(binary, timeout_s=30.0)
    gen = provider.complete(model=ModelId("antigravity/default"), messages=USER, tools=())
    first = await gen.__anext__()
    assert isinstance(first, TextDelta)
    await gen.aclose()
    pid = await _read_pid(binary + ".pid")
    assert await _wait_for_death(pid), "child survived the stream being abandoned"


# --- Step 4: mandatory live verification, kept as a permanent skip-marked
# regression. Runs a real turn against the real agy binary and real Google
# subscription auth when both are present on the machine; CI has neither, so
# this always skips there and the suite stays hermetic. ---


def _real_agy_available() -> bool:
    if shutil.which("agy") is None:
        return False
    oauth = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    return oauth.is_file()


@pytest.mark.skipif(
    not _real_agy_available(), reason="real agy binary or ~/.gemini auth not present"
)
async def test_live_turn_round_trips_through_real_mcp_tool_server(monkeypatch):
    """Consumes real subscription quota -- deliberately the only test in this
    file that does. Confirms, against the real CLI: registration via `agy
    mcp add` succeeds, the scratch-HOME auth copy is sufficient to
    authenticate, a real MCP tool call round-trips through a real
    McpToolServer to this stub and back, and the assembled response reflects
    the tool's result."""
    # Override the autouse _isolated_gemini_home fixture: this test is the
    # one place that needs the REAL ~/.gemini to authenticate.
    monkeypatch.setenv("HOME", _REAL_HOME)

    calls = []

    async def stub_dispatch(call):
        calls.append(call)
        return ToolOutcome(text="LIVE_VERIFICATION_MARKER", blob=None, is_error=False)

    class _LiveDispatcher:
        async def dispatch_tool(self, call):
            return await stub_dispatch(call)

    spec = ToolSpec(
        name=ToolName("read_file"),
        description="Read the contents of a file at the given path and return its text.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to read"}},
            "required": ["path"],
        },
    )
    provider = AntigravityProvider(timeout_s=120.0)
    provider.bind_dispatcher(_LiveDispatcher())
    messages = [
        Message(
            role=Role.USER,
            blocks=(
                TextBlock(
                    text=(
                        'Call the read_file tool with path="probe.txt", then reply '
                        "with exactly the tool's returned text and nothing else -- no "
                        "extra words, no punctuation, no markdown."
                    )
                ),
            ),
        )
    ]
    message, usage, stop = await collect(
        provider.complete(model=ModelId("antigravity/default"), messages=messages, tools=(spec,))
    )
    assert calls, f"the real turn never called the stubbed harness tool; got: {message.text()!r}"
    assert "LIVE_VERIFICATION_MARKER" in message.text()
    assert stop == "end_turn"
