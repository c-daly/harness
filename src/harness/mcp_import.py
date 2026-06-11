"""Convert .mcp.json (the mcpServers convention) into McpServerSpecs.

A converter, not a compatibility layer: the clean subset converts, everything
else is skipped per-server with a warning that names the reason. The critical
inversion: .mcp.json env/header values are LITERALS (possibly ${VAR}
expansions); mcp.toml values are env-var NAMES. Only pure references convert.
Literals are never written anywhere."""

import json
import re

from harness.mcp_config import McpConfigError, McpServerSpec, _parse_server

_PURE_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_REF_WITH_DEFAULT = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*):-[^}]*\}$")
_KNOWN_FIELDS = {
    "type", "command", "args", "env", "url", "headers", "timeout", "alwaysLoad", "cwd",
}
_TYPE_ALIASES = {None: None, "stdio": "stdio", "http": "http", "streamable-http": "http"}


class McpImportError(Exception):
    pass


class _Skip(Exception):
    """Internal: this server cannot convert; the message is the warning body."""


def _reference(value: str, *, where: str, warnings: list[str], server: str) -> str:
    if match := _PURE_REF.match(value):
        return match.group(1)
    if match := _REF_WITH_DEFAULT.match(value):
        warnings.append(
            f"{server}: {where} used a default expansion {value!r}; the default was"
            f" dropped (harness reads ${match.group(1)} only)"
        )
        return match.group(1)
    if value.startswith("$") or "${" in value:
        raise _Skip(
            f"{where} value {value!r} is a composite (literal + reference);"
            " export the full value in one env var and re-add by hand"
        )
    raise _Skip(
        f"{where} value is a literal; mcp.toml only stores env-var"
        " references — export it and reference the variable name instead"
    )


def _convert_server(name: str, body, warnings: list[str]) -> McpServerSpec:
    if not isinstance(body, dict):
        raise _Skip("server entry must be an object")
    for field in ("oauth", "headersHelper"):
        if field in body:
            raise _Skip(f"{field} requires interactive auth — not convertible")
    raw_type = body.get("type")
    if raw_type in ("sse", "ws"):
        raise _Skip(
            f"transport {raw_type!r} is not supported"
            " (ask upstream for streamable HTTP)"
        )
    if raw_type not in _TYPE_ALIASES:
        raise _Skip(f"unknown type {raw_type!r}")
    for field in sorted(set(body) - _KNOWN_FIELDS):
        warnings.append(
            f"{name}: unrecognized field {field!r} ignored"
            " (source may not be a Claude Code config)"
        )

    command = body.get("command")
    args = list(body.get("args", []))
    if command in ("cmd", "cmd.exe") and args and args[0].lower() in ("/c", "/k"):
        warnings.append(f"{name}: unwrapped Windows command wrapper")
        command, args = args[1], args[2:]
    if isinstance(command, str) and command.endswith(".cmd"):
        warnings.append(f"{name}: unwrapped Windows .cmd suffix")
        command = command[: -len(".cmd")]

    env = {
        key: _reference(str(value), where=f"env.{key}", warnings=warnings, server=name)
        for key, value in (body.get("env") or {}).items()
    }
    headers = {
        key: _reference(str(value), where=f"headers.{key}", warnings=warnings, server=name)
        for key, value in (body.get("headers") or {}).items()
    }

    toml_body: dict = {}
    if command is not None:
        toml_body["command"] = command
        if args:
            toml_body["args"] = args
        if body.get("cwd"):
            toml_body["cwd"] = body["cwd"]
    if body.get("url") is not None:
        toml_body["url"] = body["url"]
    if env:
        toml_body["env"] = env
    if headers:
        toml_body["headers"] = headers
    if isinstance(body.get("timeout"), (int, float)) and body["timeout"] >= 1000:
        toml_body["tool_timeout_s"] = body["timeout"] / 1000.0
    try:
        return _parse_server(name, toml_body, source="adhoc")
    except McpConfigError as exc:
        raise _Skip(str(exc)) from exc


def convert_mcp_json(text: str) -> tuple[list[McpServerSpec], list[str]]:
    """Returns (specs, warnings). Per-server problems skip that server with a
    warning; document-level problems raise McpImportError."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpImportError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        raise McpImportError("missing top-level 'mcpServers' object")
    specs: list[McpServerSpec] = []
    warnings: list[str] = []
    for name, body in data["mcpServers"].items():
        try:
            specs.append(_convert_server(name, body, warnings))
        except _Skip as skip:
            warnings.append(f"skipped {name}: {skip}")
    return specs, warnings
