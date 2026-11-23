# tests/test_catalog.py
import pytest
import subprocess
import sys

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


def test_catalog_resolution_never_imports_inference_runtime():
    code = """
import sys
from harness.catalog import Catalog
resolved = Catalog({'local': {'route': 'openai/local', 'max_input_tokens': 8192}}).resolve('local')
assert resolved.max_input_tokens == 8192
assert 'litellm' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_catalog_inference_defers_external_tool_server_until_agent_use(tmp_path):
    code = """
import asyncio
import sys
from pathlib import Path

from harness.catalog import Catalog
from harness.cli import _catalog_provider, build_kernel
from harness.handoff import capture_scope
from harness.provider_claude_code import McpToolServer, running_tool_server

root = Path(sys.argv[1])
catalog = Catalog({
    "desk": {"route": "openai/fixture", "api_base": "http://127.0.0.1:8080/v1"},
    "vendor": {"backend": "claude-code", "route": "claude-code/default"},
})
provider = _catalog_provider(catalog)
kernel = build_kernel(base_dir=root, model="desk", provider=provider, native_tools=True, workspace_root=root)

async def check():
    dispatcher = kernel.loop.dispatcher
    assert provider.execution_kind("desk") == "inference"
    scope = capture_scope(dispatcher)
    assert "read_file" in scope["tools"] and scope["context_bindings"] == {}
    assert provider.agent_runtime_info("vendor").runtime == "claude-code"
    server = McpToolServer(specs=kernel.registry.specs(), dispatch=dispatcher.dispatch_tool)
    dependencies = {"mcp", "uvicorn", "starlette"}
    assert not dependencies.intersection(sys.modules), dependencies.intersection(sys.modules)
    async with running_tool_server(server):
        assert server.url.startswith("http://127.0.0.1:")
        assert dependencies.issubset(sys.modules)

try:
    asyncio.run(check())
finally:
    kernel.session.close()
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
