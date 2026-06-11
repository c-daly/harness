"""Convert .mcp.json (Claude Code convention) to mcp.toml specs.

env-value semantics INVERT across formats: .mcp.json values are literals
(possibly ${VAR} expansions); mcp.toml values are env-var NAMES. Only pure
references convert; literals and composites are refused per-server."""

import json

import pytest

from harness.mcp_import import McpImportError, convert_mcp_json


def convert(payload):
    return convert_mcp_json(json.dumps(payload))


def test_minimal_stdio_server():
    specs, warnings = convert({"mcpServers": {"continuity": {"command": "/bin/continuity"}}})
    assert warnings == []
    (spec,) = specs
    assert spec.name == "continuity"
    assert spec.transport == "stdio"
    assert spec.command == "/bin/continuity"
    assert spec.source == "adhoc"


def test_stdio_with_args_and_reference_env():
    specs, warnings = convert({"mcpServers": {"gh": {
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "${GITHUB_PAT}"},
    }}})
    (spec,) = specs
    assert spec.args == ("-y", "@modelcontextprotocol/server-github")
    assert spec.env == {"GITHUB_TOKEN": "GITHUB_PAT"}


def test_http_server_with_type_alias():
    specs, _ = convert({"mcpServers": {"api": {
        "type": "streamable-http", "url": "https://x/mcp",
    }}})
    assert specs[0].transport == "http"


def test_literal_env_value_skips_server_with_warning():
    specs, warnings = convert({"mcpServers": {"leaky": {
        "command": "x", "env": {"TOKEN": "ghp_areallivetoken123"},
    }}})
    assert specs == []
    assert any("leaky" in w and "literal" in w for w in warnings)


def test_default_expansion_converts_with_warning():
    specs, warnings = convert({"mcpServers": {"d": {
        "command": "x", "env": {"K": "${MY_VAR:-supersecret123}"},
    }}})
    (spec,) = specs
    assert spec.env == {"K": "MY_VAR"}
    assert any("default" in w for w in warnings)
    assert all("supersecret123" not in w for w in warnings)  # secrets never echo


def test_composite_header_skips_server():
    specs, warnings = convert({"mcpServers": {"api": {
        "type": "http", "url": "https://x/mcp",
        "headers": {"Authorization": "Bearer ${API_KEY}"},
    }}})
    assert specs == []
    assert any("api" in w and "composite" in w.lower() for w in warnings)
    assert all("Bearer" not in w for w in warnings)  # composite value never echoed


@pytest.mark.parametrize("server,why", [
    ({"type": "sse", "url": "https://x/sse"}, "sse"),
    ({"type": "ws", "url": "wss://x"}, "ws"),
    ({"type": "http", "url": "https://x/mcp", "oauth": {"clientId": "c"}}, "oauth"),
    ({"type": "http", "url": "https://x/mcp", "headersHelper": "get-headers.sh"},
     "headersHelper"),
])
def test_unsupported_features_skip_with_warning(server, why):
    specs, warnings = convert({"mcpServers": {"s": server}})
    assert specs == []
    assert any(why in w for w in warnings)


def test_cmd_slash_c_unwrap():
    specs, warnings = convert({"mcpServers": {"win": {
        "command": "cmd", "args": ["/c", "npx", "-y", "pkg"],
    }}})
    (spec,) = specs
    assert spec.command == "npx"
    assert spec.args == ("-y", "pkg")
    assert any("unwrap" in w for w in warnings)


def test_timeout_ms_maps_to_tool_timeout_s():
    specs, _ = convert({"mcpServers": {"t": {"command": "x", "timeout": 30000}}})
    assert specs[0].tool_timeout_s == 30.0


def test_unknown_fields_warn_but_convert():
    specs, warnings = convert({"mcpServers": {"s": {
        "command": "x", "disabled": True, "autoApprove": ["y"],
    }}})
    assert len(specs) == 1
    assert any("disabled" in w for w in warnings)


def test_invalid_name_skips_with_warning():
    specs, warnings = convert({"mcpServers": {"bad__name": {"command": "x"}}})
    assert specs == []
    assert any("bad__name" in w for w in warnings)


def test_missing_mcp_servers_key_is_an_error():
    with pytest.raises(McpImportError):
        convert_mcp_json(json.dumps({"servers": {}}))


def test_invalid_json_is_an_error():
    with pytest.raises(McpImportError):
        convert_mcp_json("{not json")


def test_non_dict_env_skips_not_crashes():
    specs, warnings = convert({"mcpServers": {"s": {"command": "x", "env": ["VAR"]}}})
    assert specs == []
    assert any("env must be an object" in w for w in warnings)


def test_non_dict_headers_skips_not_crashes():
    specs, warnings = convert({"mcpServers": {"s": {
        "type": "http", "url": "https://x/mcp", "headers": "oops",
    }}})
    assert specs == []
    assert any("headers must be an object" in w for w in warnings)


def test_cmd_suffix_strip():
    specs, warnings = convert({"mcpServers": {"w": {"command": "npx.cmd", "args": ["-y", "p"]}}})
    assert specs[0].command == "npx"
    assert any("unwrap" in w for w in warnings)


def test_sub_second_timeout_ignored_with_warning():
    specs, warnings = convert({"mcpServers": {"t": {"command": "x", "timeout": 999}}})
    assert specs[0].tool_timeout_s == 60.0
    assert any("below 1000ms" in w for w in warnings)


def test_always_load_is_known_and_silently_ignored():
    specs, warnings = convert({"mcpServers": {"s": {"command": "x", "alwaysLoad": True}}})
    assert len(specs) == 1
    assert warnings == []
