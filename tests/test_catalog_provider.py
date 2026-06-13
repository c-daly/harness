"""Layer A: CatalogProvider resolves endpoint + key per call from the alias."""

import pytest

from harness import provider_litellm
from harness.catalog import Catalog
from harness.provider import StreamStop, TextDelta
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId

CATALOG = Catalog(
    entries={
        "local": {"route": "ollama/llama3", "api_base": "http://localhost:11434"},
        "gpt": {"route": "openai/gpt-4o-mini", "api_key_env": "MY_TEST_KEY"},
    }
)


@pytest.fixture(autouse=True)
def _no_cost_map(monkeypatch):
    # keep resolve() hermetic + fast (skip the litellm cost-map import)
    monkeypatch.setattr("harness.catalog._cost_map_lookup", lambda route: {})


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    async def _fake_acomplete(**kwargs):
        calls.append(kwargs)
        yield TextDelta(text="ok")
        yield StreamStop(stop_reason="end_turn")

    monkeypatch.setattr(provider_litellm, "_acomplete", _fake_acomplete)
    return calls


async def _drain(provider, model):
    async for _ in provider.complete(model=ModelId(model), messages=[], tools=()):
        pass


async def test_local_alias_resolves_route_and_api_base(captured):
    await _drain(CatalogProvider(CATALOG), "local")
    (call,) = captured
    assert str(call["model"]) == "ollama/llama3"
    assert call["api_base"] == "http://localhost:11434"
    assert call["api_key"] is None  # no api_key_env on this entry


async def test_gpt_alias_dereferences_api_key_env(monkeypatch, captured):
    monkeypatch.setenv("MY_TEST_KEY", "secret-value")
    await _drain(CatalogProvider(CATALOG), "gpt")
    (call,) = captured
    assert str(call["model"]) == "openai/gpt-4o-mini"
    assert call["api_base"] is None
    assert call["api_key"] == "secret-value"


async def test_unknown_alias_falls_back_to_literal_route(captured):
    await _drain(CatalogProvider(CATALOG), "some/raw-model")
    (call,) = captured
    assert str(call["model"]) == "some/raw-model"
    assert call.get("api_base") is None
    assert call.get("api_key") is None
