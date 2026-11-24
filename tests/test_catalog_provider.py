"""Layer A: CatalogProvider resolves endpoint + key per call from the alias."""

import pytest

from harness import provider_litellm
from harness.catalog import Catalog
from harness.errors import AuthFailed
from harness.inference import InferenceRequest
from harness.provider import StreamStop, TextDelta
from harness.provider_litellm import CatalogProvider, LiteLLMProvider
from harness.types import ModelId

CATALOG = Catalog(
    entries={
        "local": {"route": "ollama/llama3", "api_base": "http://localhost:11434"},
        "keyed-loopback": {"route": "openai/test-model", "api_base": "http://localhost:8000/v1",
                          "api_key_env": "MY_TEST_KEY"},
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


async def _drain(provider, model, operation="complete"):
    if operation == "infer":
        stream = provider.infer(InferenceRequest(model=ModelId(model), messages=(), purpose="completion"))
    else:
        stream = provider.complete(model=ModelId(model), messages=[], tools=())
    async for _ in stream:
        pass


async def test_local_alias_resolves_route_and_api_base(captured):
    await _drain(CatalogProvider(CATALOG), "local")
    (call,) = captured
    assert str(call["model"]) == "ollama/llama3"
    assert call["api_base"] == "http://localhost:11434"
    # no api_key_env on this entry: CatalogProvider injects a placeholder
    # (a real value is never required for a local api_base)
    assert call["api_key"] == "local-no-key"


async def test_gpt_alias_dereferences_api_key_env(monkeypatch, captured):
    monkeypatch.setenv("MY_TEST_KEY", "secret-value")
    await _drain(CatalogProvider(CATALOG), "gpt")
    (call,) = captured
    assert str(call["model"]) == "openai/gpt-4o-mini"
    assert call["api_base"] is None
    assert call["api_key"] == "secret-value"


@pytest.mark.parametrize("operation", ["infer", "complete"])
@pytest.mark.parametrize("value", [None, "", " \t "])
@pytest.mark.parametrize(("provider", "model"), [
    (CatalogProvider(CATALOG), "gpt"),
    (CatalogProvider(CATALOG), "keyed-loopback"),
    (LiteLLMProvider(api_key_env="MY_TEST_KEY"), "openai/test-model"),
])
async def test_missing_named_key_stops_before_submission(monkeypatch, captured, provider, model, operation, value):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-account")
    if value is None:
        monkeypatch.delenv("MY_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("MY_TEST_KEY", value)
    with pytest.raises(AuthFailed, match="api_key_env"):
        await _drain(provider, model, operation)
    assert captured == []


@pytest.mark.parametrize("operation", ["infer", "complete"])
async def test_anonymous_loopback_ignores_ambient_vendor_key(monkeypatch, captured, operation):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-account")
    endpoint = "http://127.0.0.1:8000/v1"
    catalog = Catalog(entries={"local": {"route": "openai/test-model", "api_base": endpoint}})
    await _drain(CatalogProvider(catalog), "local", operation)
    (call,) = captured
    assert call["api_base"] == endpoint
    assert call["api_key"] == "local-no-key"


async def test_unknown_alias_falls_back_to_literal_route(captured):
    await _drain(CatalogProvider(CATALOG), "some/raw-model")
    (call,) = captured
    assert str(call["model"]) == "some/raw-model"
    assert call.get("api_base") is None
    assert call.get("api_key") is None
