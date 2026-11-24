# tests/test_catalog.py
import pytest
import os
import subprocess
import sys

from harness.catalog import Catalog, CatalogConfigError, ResolvedModel, UnknownAliasError


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


def test_in_memory_catalog_preserves_explicit_unknown_metadata():
    catalog = Catalog({"small": {
        "route": "gpt-4o-mini", "backend": None, "api_base": None, "api_key_env": None,
        "input_cost_per_token": None, "output_cost_per_token": None, "max_input_tokens": None,
    }})
    resolved = catalog.resolve("small")
    assert resolved.max_input_tokens is None and resolved.pricing_dict() == {}


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


@pytest.mark.parametrize(("source", "field"), [
    ("models = ['CANARY']", "models"),
    ("[models]\ndemo = ['CANARY']", "models.demo"),
    ("[models.demo]\napi_key_env = 'CANARY'", "models.demo.route"),
    ("[models.demo]\nroute = ['CANARY']", "models.demo.route"),
    ("[models.demo]\nroute = '   '", "models.demo.route"),
    ("[models.demo]\nroute = 'x'\ntags = 'CANARY'", "models.demo.tags"),
    ("[models.demo]\nroute = 'x'\ninput_cost_per_token = nan", "models.demo.input_cost_per_token"),
    ("[models.demo]\nroute = 'x'\nverified = 'CANARY'", "models.demo.verified"),
    ("[models.demo]\nroute = 'x'\nmax_input_tokens = true", "models.demo.max_input_tokens"),
    ("[models.demo]\nroute = 'x'\nlocal = 'CANARY'", "models.demo.local"),
])
def test_invalid_entry_identifies_catalog_and_field_without_dumping_data(tmp_path, source, field):
    path = tmp_path / "custom.toml"
    path.write_text(source)
    with pytest.raises(CatalogConfigError) as error:
        Catalog.load(path)
    assert str(path) in str(error.value)
    assert field in str(error.value)
    assert "CANARY" not in str(error.value)


@pytest.mark.parametrize("source", [b"[models.demo]\n'CANARY'=1\n'CANARY'=2", b"\xffCANARY"])
def test_bad_catalog_syntax_reports_safe_path_and_remedy(tmp_path, source):
    path = tmp_path / "custom.toml"
    path.write_bytes(source)
    with pytest.raises(CatalogConfigError) as error:
        Catalog.load(path)
    message = str(error.value)
    assert str(path) in message and ("syntax" in message or "UTF-8" in message)
    assert "CANARY" not in message


@pytest.mark.parametrize(("local_settings", "field"), [
    ('probe_kind = "CANARY"', "models.demo.local.probe_kind"),
    ('CANARY = "secret"', "models.demo.local"),
    ('command = [2, "CANARY"]', "models.demo.local.command"),
])
def test_local_profile_error_excludes_raw_inputs_and_unknown_keys(tmp_path, local_settings, field):
    path = tmp_path / "custom.toml"
    path.write_text('[models.demo]\nroute = "openai/test"\n[models.demo.local]\n' + local_settings)
    with pytest.raises(CatalogConfigError) as error:
        Catalog.load(path).resolve("demo")
    assert str(path) in str(error.value) and field in str(error.value)
    assert "CANARY" not in str(error.value)


def test_symlinked_catalog_remains_readable_but_registration_and_listing_require_actual_path(tmp_path):
    from harness.model_management import ModelSetupError, load_setup_catalog, render_catalog

    path = tmp_path / "real.toml"
    path.write_text(CATALOG_TOML)
    alias = tmp_path / "models.toml"
    alias.symlink_to(path)
    assert Catalog.load(alias).resolve("gpt").verified
    for read in (load_setup_catalog, render_catalog):
        with pytest.raises(ModelSetupError, match="actual path"):
            read(alias)


def test_catalog_readers_reject_fifo_without_a_writer(tmp_path):
    path = tmp_path / "models.toml"
    os.mkfifo(path)
    code = """
from pathlib import Path
import sys
from harness.catalog import Catalog
from harness.model_management import render_catalog, load_setup_catalog
for read in (Catalog.load, load_setup_catalog, render_catalog):
    try:
        read(Path(sys.argv[1]))
    except ValueError as exc:
        assert "regular file" in str(exc)
    else:
        raise AssertionError("expected a regular file diagnostic")
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_catalog_readers_reject_oversized_files_before_parsing(tmp_path):
    from harness.model_management import load_setup_catalog, render_catalog

    path = tmp_path / "models.toml"
    path.write_bytes(b"#" * (2 * 1024 * 1024 + 1))
    for read in (Catalog.load, load_setup_catalog, render_catalog):
        with pytest.raises(ValueError, match="2 MiB"):
            read(path)


def test_models_command_displays_safe_repair_for_a_broken_catalog(tmp_path):
    from harness.models_cli import main

    path = tmp_path / "broken.toml"
    path.write_text("[models.broken]\napi_key_env = 'CANARY'")
    with pytest.raises(SystemExit) as error:
        main(["--catalog", str(path), "list"])
    assert str(path) in str(error.value)
    assert "models.broken.route must be a nonempty string" in str(error.value)
    assert "CANARY" not in str(error.value)


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
