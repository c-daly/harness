# tests/test_catalog.py
import pytest

from harness.catalog import Catalog, ResolvedModel, UnknownAliasError


CATALOG_TOML = """
[models.gpt]
route = "gpt-4o-mini"
tags = ["tools", "cheap"]
api_key_env = "OPENAI_API_KEY"
verified = true

[models.local]
route = "ollama_chat/llama3.1"
api_base = "http://localhost:11434"
tags = ["local", "tools"]
input_cost_per_token = 0.0
output_cost_per_token = 0.0
max_input_tokens = 8192

[models.mystery]
route = "totally/unknown-model-xyz"
"""


def _catalog(tmp_path):
    path = tmp_path / "models.toml"
    path.write_text(CATALOG_TOML)
    return Catalog.load(path)


def test_resolve_known_model_pricing_from_litellm(tmp_path):
    resolved = _catalog(tmp_path).resolve("gpt")
    assert isinstance(resolved, ResolvedModel)
    assert resolved.route == "gpt-4o-mini"
    assert resolved.input_cost_per_token is not None and resolved.input_cost_per_token > 0
    assert resolved.max_input_tokens and resolved.max_input_tokens > 1000
    assert resolved.verified is True
    assert resolved.api_key_env == "OPENAI_API_KEY"


def test_local_overrides_beat_cost_map(tmp_path):
    resolved = _catalog(tmp_path).resolve("local")
    assert resolved.input_cost_per_token == 0.0
    assert resolved.max_input_tokens == 8192
    assert resolved.api_base == "http://localhost:11434"
    assert resolved.verified is False  # default


def test_unknown_route_without_overrides_resolves_with_none_pricing(tmp_path):
    resolved = _catalog(tmp_path).resolve("mystery")
    assert resolved.input_cost_per_token is None  # honest: unknown, not zero


def test_unknown_alias_raises(tmp_path):
    with pytest.raises(UnknownAliasError):
        _catalog(tmp_path).resolve("nope")


def test_pricing_dict_for_events(tmp_path):
    resolved = _catalog(tmp_path).resolve("gpt")
    d = resolved.pricing_dict()
    assert set(d) == {"input_cost_per_token", "output_cost_per_token"}
    resolved_none = _catalog(tmp_path).resolve("mystery")
    assert resolved_none.pricing_dict() == {}
