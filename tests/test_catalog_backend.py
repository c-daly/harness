"""Catalog `backend` field: selects a non-LiteLLM provider implementation."""

import pytest

from harness.catalog import Catalog, CatalogConfigError, UnknownBackendError


def _catalog(tmp_path, body: str) -> Catalog:
    p = tmp_path / "models.toml"
    p.write_text(body)
    return Catalog.load(p)


def test_backend_defaults_to_none(tmp_path):
    cat = _catalog(tmp_path, '[models.gpt]\nroute = "openai/gpt-5"\n')
    assert cat.resolve("gpt").backend is None


def test_backend_claude_code_resolves(tmp_path):
    cat = _catalog(
        tmp_path,
        '[models.claude]\nbackend = "claude-code"\nroute = "claude-code/default"\n'
        "input_cost_per_token = 0.0\noutput_cost_per_token = 0.0\n",
    )
    resolved = cat.resolve("claude")
    assert resolved.backend == "claude-code"
    assert resolved.route == "claude-code/default"


def test_backend_antigravity_resolves(tmp_path):
    cat = _catalog(
        tmp_path,
        '[models.gemini]\nbackend = "antigravity"\nroute = "antigravity/default"\n'
        "input_cost_per_token = 0.0\noutput_cost_per_token = 0.0\n",
    )
    resolved = cat.resolve("gemini")
    assert resolved.backend == "antigravity"
    assert resolved.route == "antigravity/default"


def test_unknown_backend_errors_at_resolve(tmp_path):
    cat = _catalog(tmp_path, '[models.x]\nbackend = "CANARY"\nroute = "x/y"\n')
    with pytest.raises(UnknownBackendError, match=r"models\.x\.backend") as error:
        cat.resolve("x")
    assert "codex" in str(error.value) and "CANARY" not in str(error.value)


def test_subscription_model_has_no_implied_api_credentials_or_prices():
    entry = {"route": "claude-cli/sonnet", "transport": "claude-cli"}
    resolved = Catalog({"sub": entry}).resolve("sub")
    assert resolved.execution_kind == "inference" and resolved.backend is None
    assert resolved.output_cost_per_token is None and resolved.input_cost_per_token is None
    for conflict in ({"api_key_env": "MY_KEY"}, {"backend": "claude-code"}, {"api_base": "http://localhost"}):
        with pytest.raises(CatalogConfigError):
            Catalog({"sub": {**entry, **conflict}}).resolve("sub")


@pytest.mark.parametrize("name", ["sonnet[1m]", "claude-3:latest", "a" * 181])
def test_subscription_model_name_supported_by_catalog(name):
    route = f"claude-cli/{name}"
    assert Catalog({"sub": {"route": route, "transport": "claude-cli"}}).resolve("sub").route == route


@pytest.mark.parametrize("route", ["sonnet", "claude-cli/", "claude-cli/" + "a" * 182,
                                   "claude-cli/--help", "claude-cli/one/two", "claude-cli/model name"])
def test_catalog_rejects_invalid_subscription_model_name(route):
    with pytest.raises(CatalogConfigError, match=r"models\.sub\.route"):
        Catalog({"sub": {"route": route, "transport": "claude-cli"}}).resolve("sub")


async def test_unregistered_cli_model_never_falls_through_to_api():
    from harness.errors import ProviderError
    from harness.inference import InferenceRequest
    from harness.provider_litellm import CatalogProvider

    provider = CatalogProvider(Catalog({}))
    request = InferenceRequest(model="claude-cli/sonnet", messages=(), purpose="utility")
    for source in (provider.infer(request), provider.complete(model=request.model, messages=())):
        with pytest.raises(ProviderError, match="harness setup cli"):
            await anext(source)
