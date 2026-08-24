"""Catalog `backend` field: selects a non-LiteLLM provider implementation."""

import pytest

from harness.catalog import Catalog, UnknownBackendError


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


def test_unknown_backend_errors_at_resolve(tmp_path):
    cat = _catalog(tmp_path, '[models.x]\nbackend = "frobnicator"\nroute = "x/y"\n')
    with pytest.raises(UnknownBackendError, match="frobnicator"):
        cat.resolve("x")
