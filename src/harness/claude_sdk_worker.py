"""One Claude Agent SDK model turn in a child with the selected account's environment.

The parent owns the deadline, process group, private working directory and output
limit. Exit 78 specifically permits retrying through the same executable.
"""

import asyncio
import json
import sys
from pathlib import Path

SDK_UNAVAILABLE = 78
CLAUDE_SUBSCRIPTION_SETTINGS = json.dumps({
    "disableAllHooks": True,
    "disableClaudeAiConnectors": True,
    "fallbackModel": [],
    "fastMode": False,
    "forceLoginMethod": "claudeai",
})


async def _run(binary: str, route: str, limit: int) -> int:
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            UserMessage,
        )
        # Older versions may accept a prompt dict but silently omit the file flag.
        from claude_agent_sdk.types import SystemPromptFile
    except ImportError:
        return SDK_UNAVAILABLE

    try:
        options = ClaudeAgentOptions(
            cli_path=binary,
            cwd=Path.cwd(),
            model=None if route == "default" else route,
            system_prompt=SystemPromptFile(type="file", path=str(Path.cwd() / "system.txt")),
            tools=[],
            max_turns=1,
            mcp_servers='{"mcpServers":{}}',
            strict_mcp_config=True,
            disallowed_tools=["mcp__*"],
            permission_mode="dontAsk",
            setting_sources=[],
            settings=CLAUDE_SUBSCRIPTION_SETTINGS,
            max_buffer_size=limit,
            extra_args={"safe-mode": None, "restricted": None, "no-chrome": None,
                        "no-session-persistence": None, "disable-slash-commands": None,
                        "permission-prompts": "none"},
        )
    except TypeError:
        return SDK_UNAVAILABLE

    prompt = sys.stdin.buffer.read().decode("utf-8")
    client = ClaudeSDKClient(options=options)
    try:
        # Connect's empty input stream sends only the SDK initialize request.
        await client.connect()
    except Exception as exc:
        # The SDK sends a plain Exception containing the CLI's control error.
        # An opaque disconnect can equally be a policy refusal. Fail closed.
        if str(exc).strip().lower() in {
            "unsupported protocol version", "unknown control request: initialize",
            "unknown control request subtype: initialize",
        }:
            return SDK_UNAVAILABLE
        raise

    try:
        # Once query starts, even a failed write may already have submitted
        # the prompt. Every path below this point is terminal for this request.
        await client.query(prompt)
        invalid_response = False
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                if (message.error or message.parent_tool_use_id or
                        any(not isinstance(block, (TextBlock, ThinkingBlock)) for block in message.content)):
                    invalid_response = True
            elif isinstance(message, UserMessage):
                if (message.tool_use_result is not None or message.parent_tool_use_id or
                        isinstance(message.content, list) and
                        any(not isinstance(block, TextBlock) for block in message.content)):
                    invalid_response = True
            elif isinstance(message, ResultMessage):
                failed = (message.is_error is not False or invalid_response or bool(message.errors)
                          or message.api_error_status is not None or message.deferred_tool_use is not None)
                record = {
                    "type": "result", "subtype": message.subtype, "is_error": failed,
                    "num_turns": message.num_turns, "result": message.result,
                    "usage": message.usage, "stop_reason": message.stop_reason,
                    "permission_denials": message.permission_denials,
                }
                print(json.dumps(record, ensure_ascii=False, allow_nan=False))
                return 0
        return 1
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        binary, route, limit = sys.argv[1:]
        status = asyncio.run(_run(binary, route, int(limit)))
    except Exception:
        # SDK exceptions may include the submitted conversation or vendor output.
        status = 1
    if status == SDK_UNAVAILABLE:
        print('{"sdk_unavailable": true}')
    raise SystemExit(status)
