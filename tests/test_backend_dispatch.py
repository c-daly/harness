"""CatalogProvider routes backend='claude-code' entries to ClaudeCodeProvider
and everything else down the existing LiteLLM path."""

import pytest

from harness.catalog import Catalog
from harness.errors import ProviderError
from harness.messages import Message, Role, TextBlock
from harness.provider import TextDelta, collect
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId

USER = [Message(role=Role.USER, blocks=(TextBlock(text="hi"),))]


def _catalog(tmp_path):
    p = tmp_path / "models.toml"
    p.write_text(
        '[models.claude]\nbackend = "claude-code"\nroute = "claude-code/default"\n'
        "input_cost_per_token = 0.0\noutput_cost_per_token = 0.0\n"
    )
    return Catalog.load(p)


class _FakeClaudeBackend:
    def __init__(self):
        self.calls = []
        self.bound = None

    def bind_dispatcher(self, dispatcher):
        self.bound = dispatcher

    async def complete(self, *, model, messages, tools=()):
        self.calls.append((str(model), len(tuple(messages)), len(tuple(tools))))
        yield TextDelta(text="from-claude-backend")


async def test_backend_entry_routes_to_claude_code(tmp_path):
    fake = _FakeClaudeBackend()
    provider = CatalogProvider(_catalog(tmp_path), claude_code=fake)
    message, _, _ = await collect(
        provider.complete(model=ModelId("claude"), messages=USER, tools=())
    )
    assert message.text() == "from-claude-backend"
    assert fake.calls == [("claude-code/default", 1, 0)]  # route passed through


async def test_backend_entry_without_wiring_is_loud(tmp_path):
    provider = CatalogProvider(_catalog(tmp_path))  # claude_code=None
    with pytest.raises(ProviderError, match="claude-code"):
        await collect(provider.complete(model=ModelId("claude"), messages=USER, tools=()))


def test_bind_dispatcher_forwards(tmp_path):
    fake = _FakeClaudeBackend()
    provider = CatalogProvider(_catalog(tmp_path), claude_code=fake)
    sentinel = object()
    provider.bind_dispatcher(sentinel)
    assert fake.bound is sentinel


def test_bind_dispatcher_noop_without_backend(tmp_path):
    provider = CatalogProvider(_catalog(tmp_path))
    provider.bind_dispatcher(object())  # must not raise
