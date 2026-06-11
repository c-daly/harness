#!/usr/bin/env python3
"""Record raw litellm streams as conformance fixtures.

Usage: uv run python scripts/record_fixtures.py [provider ...]
Providers run only when their credentials exist:
  openai  -> OPENAI_API_KEY      (model: gpt-4o-mini)
  anthropic -> ANTHROPIC_API_KEY (model: claude-haiku-4-5-20251001)
  ollama  -> reachable localhost:11434 (model: ollama_chat/llama3.1)
Each scenario writes tests/fixtures/<provider>/<scenario>.jsonl, one raw
chunk dict per line. Spend is a few cents per provider. Re-running overwrites.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import litellm

PROVIDERS = {
    "openai": {"model": "gpt-4o-mini", "env": "OPENAI_API_KEY", "api_base": None},
    "anthropic": {"model": "anthropic/claude-haiku-4-5-20251001", "env": "ANTHROPIC_API_KEY", "api_base": None},
    "ollama": {"model": "ollama_chat/llama3.1", "env": None, "api_base": "http://localhost:11434"},
}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_word_length",
        "description": "Return the number of letters in a word",
        "parameters": {
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
    },
}]

SCENARIOS = {
    "text": {"messages": [{"role": "user", "content": "Reply with exactly: hello harness"}]},
    "tool_call": {
        "messages": [{"role": "user", "content": "Use the tool to count letters in 'harness'."}],
        "tools": TOOLS,
        "tool_choice": "required",
    },
    "multi_tool": {
        "messages": [{
            "role": "user",
            "content": "Call get_word_length twice in one turn: once for 'alpha', once for 'beta'.",
        }],
        "tools": TOOLS,
    },
}


async def record(provider: str) -> None:
    cfg = PROVIDERS[provider]
    if cfg["env"] and not os.environ.get(cfg["env"]):
        print(f"skip {provider}: {cfg['env']} not set")
        return
    out_dir = Path("tests/fixtures") / provider
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, scenario in SCENARIOS.items():
        kwargs = dict(scenario)
        kwargs.update(
            model=cfg["model"], stream=True, stream_options={"include_usage": True}
        )
        if cfg["api_base"]:
            kwargs["api_base"] = cfg["api_base"]
        stream = await litellm.acompletion(**kwargs)
        lines = []
        async for chunk in stream:
            lines.append(json.dumps(chunk.model_dump(), default=str))
        (out_dir / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
        print(f"recorded {provider}/{name}: {len(lines)} chunks")


if __name__ == "__main__":
    targets = sys.argv[1:] or list(PROVIDERS)
    for p in targets:
        asyncio.run(record(p))
