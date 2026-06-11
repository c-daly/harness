"""mcp.toml: layered MCP server configuration.

Law: env/header values are NAMES of environment variables, never literal
secrets. Resolution happens at connection start (resolve_env), so a missing
variable fails one server loudly without sinking the others.
"""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_ENV_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RESTARTS = ("never", "on_failure")
_KNOWN_KEYS = frozenset(
    {"transport", "command", "args", "cwd", "url", "env", "headers", "restart", "tool_timeout_s"}
)


class McpConfigError(Exception):
    pass


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    url: str | None = None
    # key -> ENV VAR NAME; frozen protects the ref, not contents
    env: dict[str, str] = field(default_factory=dict)
    # header -> ENV VAR NAME; frozen protects the ref, not contents
    headers: dict[str, str] = field(default_factory=dict)
    restart: Literal["never", "on_failure"] = "on_failure"
    tool_timeout_s: float = 60.0
    source: str = "user"  # "user" | "project" | "adhoc" — attribution, not behavior


def _refs(table: object, *, where: str) -> dict[str, str]:
    """Validate an env/headers table: every value must be an env-var NAME."""
    if not isinstance(table, dict):
        raise McpConfigError(f"{where} must be a table")
    out: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(value, str) or not _ENV_VAR_RE.fullmatch(value):
            raise McpConfigError(
                f"{where}.{key} = {value!r}: must be the NAME of an environment variable"
                " (never a literal value)"
            )
        out[key] = value
    return out


# Shared parse kernel: the .mcp.json importer (mcp_import) feeds JSON-derived
# bodies through here too.
def _parse_server(name: str, body: dict, *, source: str) -> McpServerSpec:
    if not isinstance(body, dict):
        raise McpConfigError(
            f"server {name!r}: body must be a table, got {type(body).__name__}"
        )
    if not _NAME_RE.fullmatch(name) or "__" in name:
        raise McpConfigError(
            f"server name {name!r} invalid: must match [A-Za-z0-9_-]+ and not contain '__'"
        )
    unknown = sorted(set(body) - _KNOWN_KEYS)
    if unknown:
        raise McpConfigError(f"server {name!r}: unknown keys: {', '.join(unknown)}")
    command = body.get("command")
    url = body.get("url")
    if command is not None and not isinstance(command, str):
        raise McpConfigError(f"server {name!r}: command must be a string")
    if url is not None and not isinstance(url, str):
        raise McpConfigError(f"server {name!r}: url must be a string")
    cwd = body.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise McpConfigError(f"server {name!r}: cwd must be a string")
    if command is not None and url is not None:
        raise McpConfigError(f"server {name!r}: both command and url given - pick one")
    if command is None and url is None:
        raise McpConfigError(f"server {name!r}: needs command or url")
    inferred = "stdio" if command is not None else "http"
    transport = body.get("transport", inferred)
    if transport != inferred:
        raise McpConfigError(
            f"server {name!r}: transport {transport!r} contradicts "
            f"{'command' if command else 'url'} (expected {inferred!r})"
        )
    restart = body.get("restart", "on_failure")
    if restart not in _RESTARTS:
        raise McpConfigError(f"server {name!r}: restart must be one of {_RESTARTS}")
    timeout = body.get("tool_timeout_s", 60.0)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise McpConfigError(f"server {name!r}: tool_timeout_s must be > 0")
    args = body.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise McpConfigError(f"server {name!r}: args must be an array of strings")
    return McpServerSpec(
        name=name,
        transport=transport,
        command=command,
        args=tuple(args),
        cwd=cwd,
        url=url,
        env=_refs(body.get("env", {}), where=f"servers.{name}.env"),
        headers=_refs(body.get("headers", {}), where=f"servers.{name}.headers"),
        restart=restart,
        tool_timeout_s=float(timeout),
        source=source,
    )


def load_mcp_file(path: Path, *, source: str) -> tuple[McpServerSpec, ...]:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise McpConfigError(f"{path}: {exc}") from exc
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        raise McpConfigError(f"{path}: [servers] must be a table")
    out = []
    for name, body in servers.items():
        try:
            out.append(_parse_server(name, body, source=source))
        except McpConfigError as exc:
            raise McpConfigError(f"{path}: {exc}") from exc
    return tuple(out)


def user_mcp_path(config_home: Path | None = None) -> Path:
    return (config_home or Path.home() / ".config" / "harness") / "mcp.toml"


def project_mcp_path(project_dir: Path) -> Path:
    return project_dir / ".harness" / "mcp.toml"


def load_mcp_config(
    project_dir: Path | None = None, config_home: Path | None = None
) -> tuple[McpServerSpec, ...]:
    """Merged view: project entries shadow user entries of the same name."""
    merged: dict[str, McpServerSpec] = {}
    user_path = user_mcp_path(config_home)
    if user_path.exists():
        for spec in load_mcp_file(user_path, source="user"):
            merged[spec.name] = spec
    if project_dir is not None:
        proj_path = project_mcp_path(project_dir)
        if proj_path.exists():
            for spec in load_mcp_file(proj_path, source="project"):
                merged[spec.name] = spec
    return tuple(merged.values())


def resolve_env(refs: dict[str, str]) -> dict[str, str]:
    """Dereference env-var NAMES to values; missing variables fail loudly."""
    missing = sorted({var for var in refs.values() if var not in os.environ})
    if missing:
        raise McpConfigError(f"missing environment variables: {', '.join(missing)}")
    return {key: os.environ[var] for key, var in refs.items()}
