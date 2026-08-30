"""CatalogProvider routes backend='claude-code' entries to ClaudeCodeProvider,
backend='codex' entries to CodexProvider, and everything else down the
existing LiteLLM path."""

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
        '\n[models.codex]\nbackend = "codex"\nroute = "codex/default"\n'
        "input_cost_per_token = 0.0\noutput_cost_per_token = 0.0\n"
        '\n[models.gemini]\nbackend = "antigravity"\nroute = "antigravity/default"\n'
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


class _FakeCodexBackend:
    def __init__(self):
        self.calls = []
        self.bound = None

    def bind_dispatcher(self, dispatcher):
        self.bound = dispatcher

    async def complete(self, *, model, messages, tools=()):
        self.calls.append((str(model), len(tuple(messages)), len(tuple(tools))))
        yield TextDelta(text="from-codex-backend")


class _FakeAntigravityBackend:
    def __init__(self):
        self.calls = []
        self.bound = None

    def bind_dispatcher(self, dispatcher):
        self.bound = dispatcher

    async def complete(self, *, model, messages, tools=()):
        self.calls.append((str(model), len(tuple(messages)), len(tuple(tools))))
        yield TextDelta(text="from-antigravity-backend")


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


async def test_backend_entry_routes_to_codex(tmp_path):
    fake = _FakeCodexBackend()
    provider = CatalogProvider(_catalog(tmp_path), codex=fake)
    message, _, _ = await collect(
        provider.complete(model=ModelId("codex"), messages=USER, tools=())
    )
    assert message.text() == "from-codex-backend"
    assert fake.calls == [("codex/default", 1, 0)]  # route passed through


async def test_backend_entry_without_wiring_is_loud_codex(tmp_path):
    provider = CatalogProvider(_catalog(tmp_path))  # codex=None
    with pytest.raises(ProviderError, match="codex"):
        await collect(provider.complete(model=ModelId("codex"), messages=USER, tools=()))


def test_bind_dispatcher_forwards_to_both(tmp_path):
    fake_claude = _FakeClaudeBackend()
    fake_codex = _FakeCodexBackend()
    provider = CatalogProvider(_catalog(tmp_path), claude_code=fake_claude, codex=fake_codex)
    sentinel = object()
    provider.bind_dispatcher(sentinel)
    assert fake_claude.bound is sentinel
    assert fake_codex.bound is sentinel


async def test_backend_entry_routes_to_antigravity(tmp_path):
    fake = _FakeAntigravityBackend()
    provider = CatalogProvider(_catalog(tmp_path), antigravity=fake)
    message, _, _ = await collect(
        provider.complete(model=ModelId("gemini"), messages=USER, tools=())
    )
    assert message.text() == "from-antigravity-backend"
    assert fake.calls == [("antigravity/default", 1, 0)]  # route passed through


async def test_backend_entry_without_wiring_is_loud_antigravity(tmp_path):
    provider = CatalogProvider(_catalog(tmp_path))  # antigravity=None
    with pytest.raises(ProviderError, match="antigravity"):
        await collect(provider.complete(model=ModelId("gemini"), messages=USER, tools=()))


def test_bind_dispatcher_forwards_to_all_three(tmp_path):
    fake_claude = _FakeClaudeBackend()
    fake_codex = _FakeCodexBackend()
    fake_antigravity = _FakeAntigravityBackend()
    provider = CatalogProvider(
        _catalog(tmp_path),
        claude_code=fake_claude,
        codex=fake_codex,
        antigravity=fake_antigravity,
    )
    sentinel = object()
    provider.bind_dispatcher(sentinel)
    assert fake_claude.bound is sentinel
    assert fake_codex.bound is sentinel
    assert fake_antigravity.bound is sentinel


# --- keyless local endpoints: litellm openai/* routes refuse to run without
# SOME api_key even against a local api_base; a local server ignores the
# value, so CatalogProvider must inject a placeholder rather than requiring
# an unrelated real credential. ---


def _fake_acompletion(captured):
    async def _empty_stream():
        for _ in ():
            yield None

    async def _acompletion(**kwargs):
        captured.append(kwargs)
        return _empty_stream()

    return _acompletion


async def test_local_api_base_without_key_gets_placeholder_api_key(tmp_path, monkeypatch):
    import litellm

    captured = []
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion(captured))
    p = tmp_path / "models.toml"
    p.write_text(
        """[models.local36]
route = "openai/local-model"
api_base = "http://localhost:8080/v1"
input_cost_per_token = 0.0
output_cost_per_token = 0.0
"""
    )
    provider = CatalogProvider(Catalog.load(p))
    await collect(provider.complete(model=ModelId("local36"), messages=USER, tools=()))
    assert captured[0]["api_key"] == "local-no-key"
    assert captured[0]["api_base"] == "http://localhost:8080/v1"


async def test_no_api_base_and_no_key_leaves_api_key_absent(tmp_path, monkeypatch):
    import litellm

    captured = []
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion(captured))
    p = tmp_path / "models.toml"
    p.write_text(
        """[models.plainroute]
route = "gpt-4o-mini"
input_cost_per_token = 0.0
output_cost_per_token = 0.0
"""
    )
    provider = CatalogProvider(Catalog.load(p))
    await collect(provider.complete(model=ModelId("plainroute"), messages=USER, tools=()))
    assert "api_key" not in captured[0]
    assert "api_base" not in captured[0]


async def test_real_api_key_env_wins_over_local_placeholder(tmp_path, monkeypatch):
    import litellm

    captured = []
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion(captured))
    monkeypatch.setenv("FAKE_LOCAL_KEY", "real-secret-value")
    p = tmp_path / "models.toml"
    p.write_text(
        """[models.localwithkey]
route = "openai/local-model"
api_base = "http://localhost:8080/v1"
api_key_env = "FAKE_LOCAL_KEY"
input_cost_per_token = 0.0
output_cost_per_token = 0.0
"""
    )
    provider = CatalogProvider(Catalog.load(p))
    await collect(
        provider.complete(model=ModelId("localwithkey"), messages=USER, tools=())
    )
    assert captured[0]["api_key"] == "real-secret-value"
