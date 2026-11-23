"""Use a signed-in Claude CLI for one model response within the Harness agent loop."""

import asyncio
import json
import logging
import os
import shutil
import site
import sys
from dataclasses import asdict
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, ValidationError

from harness.catalog import claude_cli_model_name, read_catalog_source
from harness.claude_sdk_worker import CLAUDE_SUBSCRIPTION_SETTINGS, SDK_UNAVAILABLE
from harness.errors import AuthFailed, ContextOverflow, MalformedStreamError, ProviderError
from harness.inference import InferenceRequest, check_input
from harness.messages import ImageBlock, Role, TextBlock, ToolCallBlock, ToolResultBlock
from harness.provider import StreamStop, TextDelta, ToolCallDelta, Usage, UsageReport
from harness.source_improvement import _process
from harness.types import ToolName, new_call_id


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    args: dict


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str
    tool_calls: list[_Proposal]


_SYSTEM = (
    "You are the inference model for Harness. Interpret the input as a JSON conversation, "
    "with role-labeled user, assistant and tool turns. Return the next assistant "
    "response in the requested output format. For a tool proposal, output its advertised name "
    "and arguments as JSON. Harness handles all tool execution and returns the results on a "
    "later turn. Respond directly, without code fences around requested JSON."
)
_SAFE_FLAGS = (
    "--safe-mode", "--restricted", "--no-chrome", "--setting-sources", "",
    "--settings", CLAUDE_SUBSCRIPTION_SETTINGS,
)
_MACHINE_POLICY = Path("/etc/claude-code")
_SUBSCRIPTIONS = {"free", "pro", "max", "ultra", "team", "enterprise"}


def sdk_available() -> bool:
    return find_spec("claude_agent_sdk") is not None


def _sdk_python_flags():
    # Honor an intentional pip --user installation. Both modes ignore
    # PYTHONPATH and exclude the project and worker directory from imports.
    spec = find_spec("claude_agent_sdk")
    if (site.ENABLE_USER_SITE and spec and spec.origin
            and Path(spec.origin).is_relative_to(site.getusersitepackages())):
        return ["-E", "-P"]
    return ["-I"]


def _require_compatible_policy():
    """Linux/WSL managed hooks and MCP configuration can override the CLI switches."""
    if not sys.platform.startswith("linux"):
        raise ProviderError("Claude CLI model isolation is available on Linux and WSL.")
    paths = [_MACHINE_POLICY / "managed-settings.json", _MACHINE_POLICY / "managed-mcp.json"]
    try:
        paths.extend(p for p in (_MACHINE_POLICY / "managed-settings.d").iterdir()
                     if p.suffix == ".json" and not p.name.startswith("."))
    except FileNotFoundError:
        pass
    except OSError:
        raise ProviderError("Cannot inspect the Claude CLI managed settings directory.") from None
    for path in paths:
        try:
            data = json.loads(read_catalog_source(path))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError):
            raise ProviderError("Cannot establish the Claude CLI managed tool configuration.") from None
        if not isinstance(data, dict) or not isinstance(data.get("enabledPlugins", {}), dict):
            raise ProviderError("Cannot establish the Claude CLI managed tool configuration.")
        if any(data.get(key) for key in (
            "mcpServers", "managedMcpServers", "policyHelper", "apiKeyHelper", "fallbackModel",
        )) or any(data.get("enabledPlugins", {}).values()) or (data.get("hooks") and data.get("disableAllHooks") is not True):
            raise ProviderError("Claude CLI managed settings add tools, hooks or routing overrides. "
                                "Inspect the managed policy with its administrator.")


def _payload(request: InferenceRequest):
    check_input(request)
    if request.temperature is not None:
        raise ProviderError("Claude CLI model inference does not expose temperature control.")
    instructions = [_SYSTEM]
    conversation = []
    for message in request.messages:
        if message.role == Role.SYSTEM:
            if any(not isinstance(block, TextBlock) for block in message.blocks):
                raise ProviderError("Claude CLI model inference requires text system instructions.")
            instructions.append(message.text())
            continue
        blocks = []
        for block in message.blocks:
            if isinstance(block, TextBlock):
                blocks.append({"kind": "text", "text": block.text})
            elif isinstance(block, ToolCallBlock):
                blocks.append({"kind": "tool_call", "call_id": block.call_id,
                               "tool": block.tool, "args": block.args})
            elif isinstance(block, ToolResultBlock):
                if block.text is None and block.blob is not None:
                    raise ProviderError("Claude CLI model inference requires materialized text input.")
                blocks.append({"kind": "tool_result", "call_id": block.call_id,
                               "text": block.text, "is_error": block.is_error})
            elif isinstance(block, ImageBlock):
                raise ProviderError("Claude CLI model inference requires materialized text input.")
        if blocks:
            conversation.append({"role": message.role, "blocks": blocks})
    with_tools = bool(request.tools and request.tool_choice == "auto" and request.response_schema is None)
    if request.response_schema is not None:
        output = "Return a single JSON value conforming to response_schema."
    elif with_tools:
        output = {"format": _Reply.model_json_schema(), "parallel_tool_calls": request.parallel_tool_calls}
    else:
        output = "Return plain answer text."
    payload = {"messages": conversation,
               "tools": [asdict(tool) for tool in request.tools] if with_tools else [],
               "response_schema": request.response_schema, "output": output}
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    system = "\n\n".join(instructions).encode("utf-8")
    if len(raw) + len(system) > request.max_input_bytes:
        raise ContextOverflow("Claude CLI request including its transport instructions exceeds the input limit.")
    return raw, system, with_tools


async def _run(argv, *, workdir, env, timeout, limit, data=None, sdk=False):
    try:
        result = await _process(argv, cwd=workdir, env=env, timeout=timeout, limit=limit, data=data)
    except OSError:
        raise ProviderError("Cannot launch the Claude CLI. Check installation and PATH.") from None
    if result["status"] == "timed_out":
        raise TimeoutError("Claude CLI request exceeded its deadline.")
    if result["status"] != "completed":
        raise ProviderError("Claude CLI response exceeded the transport bounds or failed during I/O.")
    try:
        value = json.loads(result["stdout"])
    except (ValueError, RecursionError):
        value = None
    if result["returncode"] != 0:
        if sdk and result["returncode"] == SDK_UNAVAILABLE and value == {"sdk_unavailable": True}:
            # The worker emits both signals only before submitting any model input.
            return None
        if isinstance(value, dict) and (data is not None and value.get("type") == "result"):
            # The kernel records known usage even when an inference call fails.
            return {**value, "is_error": True}
        if isinstance(value, dict) and data is None and value.get("loggedIn") is False:
            raise AuthFailed("Sign in with claude auth login, then retry the CLI model.")
        if sdk:
            raise ProviderError("Claude Agent SDK request failed. Check the CLI login and account status.")
        raise ProviderError("Claude CLI command failed. Check subscription login and support for "
                            "restricted single-turn print mode.")
    if not isinstance(value, dict):
        raise MalformedStreamError("Claude CLI returned an unexpected record shape.")
    return value


class ClaudeCliModelProvider:
    def __init__(self, binary="claude"):
        self.binary = binary

    async def infer(self, request: InferenceRequest):
        route = claude_cli_model_name(str(request.model))
        if route is None:
            raise ProviderError("Select a Claude CLI model name or default.")
        payload, system, with_tools = _payload(request)
        _require_compatible_policy()
        # An API key, gateway or provider override from the calling shell must never
        # switch subscription inference to metered API authentication.
        env = {key: os.environ[key] for key in (
            "HOME", "PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
        ) if key in os.environ}
        env.update(CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(request.max_output_tokens),
                   CLAUDE_CODE_MAX_RETRIES="0", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   CLAUDE_CODE_DISABLE_WORKFLOWS="1", DISABLE_AUTOUPDATER="1",
                   ENABLE_CLAUDEAI_MCP_SERVERS="false", DISABLE_EXTRA_USAGE_COMMAND="1")
        with TemporaryDirectory(prefix="harness-claude-model-") as workdir:
            async with asyncio.timeout(request.timeout_seconds):
                system_file = Path(workdir) / "system.txt"
                with os.fdopen(os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                    f.write(system)
                executable = shutil.which(self.binary)
                if executable is None:
                    raise ProviderError("Cannot launch the Claude CLI. Check installation and PATH.")
                binary = os.path.abspath(executable)
                prefix = [binary, *_SAFE_FLAGS]
                auth = await _run([*prefix, "auth", "status", "--json"], workdir=workdir, env=env,
                                  timeout=request.timeout_seconds, limit=64 * 1024)
                if (auth.get("loggedIn") is not True or auth.get("apiProvider") != "firstParty"
                        or auth.get("authMethod") != "claude.ai"
                        or str(auth.get("subscriptionType")).lower() not in _SUBSCRIPTIONS):
                    raise AuthFailed("Sign in with a Claude subscription using claude auth login, "
                                     "then retry the CLI model.")
                # Include room for CLI metadata and JSON string escaping. Raw pipes
                # remain bounded even if the binary emits noise or malformed output.
                limit = request.max_output_bytes * 6 + 65536
                run_options = dict(workdir=workdir, env=env, timeout=request.timeout_seconds,
                                   limit=limit, data=payload)
                result = None
                if sdk_available():
                    worker = Path(__file__).with_name("claude_sdk_worker.py").resolve()
                    result = await _run([sys.executable, *_sdk_python_flags(), str(worker), binary, route, str(limit)],
                                        sdk=True, **run_options)
                    if result is None:
                        logging.getLogger(__name__).info("Claude SDK unavailable before inference; using direct CLI")
                if result is None:
                    args = [*prefix, "-p", "--output-format", "json", "--max-turns", "1",
                            "--no-session-persistence", "--tools", "", "--disallowedTools", "mcp__*",
                            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                            "--disable-slash-commands", "--permission-mode", "dontAsk",
                            "--permission-prompts", "none", "--system-prompt-file", str(system_file)]
                    if route != "default":
                        args.extend(["--model", route])
                    result = await _run(args, **run_options)
        raw_usage = result.get("usage")
        if raw_usage is None:
            raw_usage = {}
        try:
            usage = Usage(input_tokens=raw_usage.get("input_tokens"),
                          output_tokens=raw_usage.get("output_tokens"),
                          cache_read_tokens=raw_usage.get("cache_read_input_tokens"),
                          cache_write_tokens=raw_usage.get("cache_creation_input_tokens"))
        except (AttributeError, ValueError):
            raise MalformedStreamError("Claude CLI returned invalid token usage.") from None
        yield UsageReport(usage)
        if (result.get("type") != "result" or result.get("is_error") is not False
                or result.get("subtype") != "success" or type(result.get("num_turns")) is not int
                or result["num_turns"] != 1 or result.get("permission_denials")):
            raise ProviderError("Claude CLI did not complete one isolated inference turn.")
        answer = result.get("result")
        if not isinstance(answer, str):
            raise MalformedStreamError("Claude CLI result contains no answer text.")
        stop = result.get("stop_reason")
        if stop == "max_tokens":
            if not with_tools:
                yield TextDelta(answer)
            yield StreamStop(stop_reason="max_tokens")
            return
        if stop not in ("end_turn", "stop_sequence"):
            raise MalformedStreamError("Claude CLI returned an unsupported inference stop reason.")
        if not with_tools:
            yield TextDelta(answer)
            yield StreamStop(stop_reason="end_turn")
            return
        try:
            reply = _Reply.model_validate_json(answer)
            arguments = [json.dumps(p.args, allow_nan=False, separators=(",", ":")) for p in reply.tool_calls]
        except (ValidationError, ValueError, RecursionError):
            raise MalformedStreamError("Claude CLI response does not match the host tool proposal format.") from None
        advertised = {tool.name for tool in request.tools}
        if any(proposal.name not in advertised for proposal in reply.tool_calls):
            raise MalformedStreamError("Claude CLI proposed an unadvertised tool.")
        if reply.text:
            yield TextDelta(reply.text)
        for index, (proposal, args_json) in enumerate(zip(reply.tool_calls, arguments, strict=True)):
            yield ToolCallDelta(index, new_call_id(), ToolName(proposal.name), args_json)
        yield StreamStop(stop_reason="tool_use" if reply.tool_calls else "end_turn")
