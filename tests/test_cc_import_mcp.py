"""MCP conversion: reuse convert_mcp_json semantics, PLUGIN_ROOT rewrite, [mcp.servers.*] emit."""

import json
import tomllib

from harness.cc_import import convert_mcp


def _toml_table(lines: str) -> dict:
    return tomllib.loads(lines)


def test_clean_stdio_server_emits_mcp_servers_table():
    text = json.dumps({"mcpServers": {"continuity": {"command": "/bin/continuity"}}})
    conv = convert_mcp(text)
    data = _toml_table(conv.toml)
    assert data["mcp"]["servers"]["continuity"]["command"] == "/bin/continuity"
    assert conv.report == () or all(e.kind == "mcp" for e in conv.report)


def test_claude_plugin_root_rewritten_to_plugin_root_in_command_and_args():
    text = json.dumps(
        {
            "mcpServers": {
                "s": {
                    "command": "${CLAUDE_PLUGIN_ROOT}/bin/run",
                    "args": ["--cfg", "${CLAUDE_PLUGIN_ROOT}/cfg.json"],
                    "cwd": "${CLAUDE_PLUGIN_ROOT}",
                }
            }
        }
    )
    conv = convert_mcp(text)
    data = _toml_table(conv.toml)
    srv = data["mcp"]["servers"]["s"]
    assert srv["command"] == "${PLUGIN_ROOT}/bin/run"
    assert srv["args"] == ["--cfg", "${PLUGIN_ROOT}/cfg.json"]
    assert srv["cwd"] == "${PLUGIN_ROOT}"
    assert "CLAUDE_PLUGIN_ROOT" not in conv.toml


def test_other_var_refs_pass_through_as_env_names():
    text = json.dumps(
        {
            "mcpServers": {
                "s": {
                    "command": "x",
                    "env": {"TOKEN": "${GITHUB_PAT}"},
                }
            }
        }
    )
    conv = convert_mcp(text)
    data = _toml_table(conv.toml)
    assert data["mcp"]["servers"]["s"]["env"] == {"TOKEN": "GITHUB_PAT"}


def test_literal_env_value_refused_and_secret_never_echoed():
    text = json.dumps(
        {
            "mcpServers": {
                "leaky": {
                    "command": "x",
                    "env": {"TOKEN": "ghp_areallivetoken123"},
                }
            }
        }
    )
    conv = convert_mcp(text)
    data = _toml_table(conv.toml)
    assert "leaky" not in data.get("mcp", {}).get("servers", {})  # refused, not emitted
    assert any(
        e.kind == "mcp" and "leaky" in e.detail and "literal" in e.detail for e in conv.report
    )
    # the secret never appears anywhere in the toml OR the report
    assert "ghp_areallivetoken123" not in conv.toml
    assert all("ghp_areallivetoken123" not in e.detail for e in conv.report)


def test_args_quoting_survives_round_trip_through_plugins_loader():
    text = json.dumps(
        {
            "mcpServers": {
                "s": {
                    "command": "npx",
                    "args": ["-y", "@scope/pkg", 'a b"c'],
                }
            }
        }
    )
    conv = convert_mcp(text)
    data = _toml_table(conv.toml)
    assert data["mcp"]["servers"]["s"]["args"] == ["-y", "@scope/pkg", 'a b"c']


def test_no_mcp_json_returns_empty_conversion():
    conv = convert_mcp(None)
    assert conv.toml == ""
    assert conv.report == ()


def test_emitted_table_round_trips_through_parse_mcp_servers(tmp_path):
    from harness.plugins import _parse_mcp_servers

    text = json.dumps(
        {
            "mcpServers": {
                "s": {
                    "command": "${CLAUDE_PLUGIN_ROOT}/run",
                    "args": ["-x"],
                    "env": {"K": "${MY_VAR}"},
                }
            }
        }
    )
    conv = convert_mcp(text)
    data = tomllib.loads(conv.toml)
    specs = _parse_mcp_servers("demo", tmp_path, data["mcp"])
    (spec,) = specs
    assert spec.name == "s"
    assert spec.command == str(tmp_path / "run")  # ${PLUGIN_ROOT} substituted at load
    assert spec.env == {"K": "MY_VAR"}
