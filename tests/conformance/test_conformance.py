# tests/conformance/test_conformance.py
"""Replay recorded provider streams through the adapter normalization.

Parametrized over whatever fixture directories exist; a provider without
fixtures SKIPS with an explicit reason (record with scripts/record_fixtures.py
once its credentials are available). These tests are the 'verified' gate for
catalog entries.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.provider import collect
from harness.provider_litellm import _normalize_chunk

FIXTURES = Path(__file__).parent.parent / "fixtures"
ALL_PROVIDERS = ("openai", "anthropic", "ollama")


def _attrize(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _attrize(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_attrize(v) for v in value]
    return value


def _load(provider: str, scenario: str):
    path = FIXTURES / provider / f"{scenario}.jsonl"
    if not path.exists():
        pytest.skip(f"no {provider} fixtures recorded (run scripts/record_fixtures.py {provider})")
    return [_attrize(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


async def _collect_fixture(provider: str, scenario: str):
    raw_chunks = _load(provider, scenario)

    async def stream():
        for raw in raw_chunks:
            for chunk in _normalize_chunk(raw):
                yield chunk

    return await collect(stream())


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_text_scenario(provider):
    message, usage, stop_reason = await _collect_fixture(provider, "text")
    assert "hello harness" in message.text().lower()
    assert stop_reason == "end_turn"
    assert usage.input_tokens > 0 and usage.output_tokens > 0


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_tool_call_scenario(provider):
    message, usage, stop_reason = await _collect_fixture(provider, "tool_call")
    calls = message.tool_calls()
    assert len(calls) >= 1
    assert calls[0].tool == "get_word_length"
    assert calls[0].args.get("word", "").lower() == "harness"
    assert stop_reason == "tool_use"


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
async def test_multi_tool_scenario(provider):
    message, _, _ = await _collect_fixture(provider, "multi_tool")
    calls = message.tool_calls()
    words = sorted(c.args.get("word", "").lower() for c in calls)
    assert len(calls) >= 2 and words[0] == "alpha" and "beta" in words
