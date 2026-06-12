"""Plugin loader: typed manifests, eight primitives, load-time validation.

THE LAW: manifest and dependency errors surface at load time, never runtime.
A plugin that fails validation fails loudly and nothing from it registers.

TRUST MODEL (explicit): installing a plugin means trusting its code. Hook
modules run in-process via importlib; mcp servers run as subprocesses. The
permission engine gates tool/model dispatch — it cannot gate plugin Python.
Runtime blast radius is bounded by the kernel contracts (dispatch hooks fail
closed with timeouts, lifecycle hooks fail open, tool exceptions become
results), but load = execute.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from harness.frontmatter import (
    AgentDef,
    CommandDef,
    FrontmatterError,
    SkillDef,
    load_agent,
    load_command,
    load_skill,
)
from harness.hooks import LifecyclePoint
from harness.mcp_config import McpConfigError, McpServerSpec, _parse_server

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
RESERVED_NAMESPACES = frozenset({"harness", "mcp", "annotation", "plugin"})
_FIRED_POINTS = frozenset({LifecyclePoint.SESSION_START, LifecyclePoint.SESSION_END})
_PLUGIN_KEYS = frozenset({"name", "version", "description", "depends"})
_MANIFEST_KEYS = frozenset({"plugin", "hooks", "mcp", "emitters", "subscribers"})


class PluginError(Exception):
    pass


@dataclass(frozen=True)
class DispatchHookDef:
    name: str
    function: str
    priority: int = 100


@dataclass(frozen=True)
class LifecycleHookDef:
    name: str
    function: str
    point: LifecyclePoint


@dataclass(frozen=True)
class SubscriberDef:
    name: str
    module: str
    function: str


@dataclass
class Plugin:
    name: str
    version: str
    description: str
    root: Path
    depends: tuple[str, ...] = ()
    skills: tuple[SkillDef, ...] = ()
    commands: tuple[CommandDef, ...] = ()
    agents: tuple[AgentDef, ...] = ()
    hooks_module: str | None = None
    dispatch_hooks: tuple[DispatchHookDef, ...] = ()
    lifecycle_hooks: tuple[LifecycleHookDef, ...] = ()
    mcp_servers: tuple[McpServerSpec, ...] = ()
    namespaces: tuple[str, ...] = ()
    subscribers: tuple[SubscriberDef, ...] = ()
    # filled by hook-module loading (Task 3): hook name -> callable
    dispatch_callables: dict = field(default_factory=dict)
    lifecycle_callables: dict = field(default_factory=dict)
    subscriber_callables: dict = field(default_factory=dict)


@dataclass
class LoadedPlugins:
    plugins: list[Plugin] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def skills(self) -> list[SkillDef]:
        return [s for p in self.plugins for s in p.skills]

    @property
    def commands(self) -> list[CommandDef]:
        return [c for p in self.plugins for c in p.commands]

    @property
    def agents(self) -> list[AgentDef]:
        return [a for p in self.plugins for a in p.agents]

    @property
    def mcp_servers(self) -> list[McpServerSpec]:
        return [s for p in self.plugins for s in p.mcp_servers]


def _require_str(plugin: str, table: dict, key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise PluginError(f"plugin {plugin!r}: [plugin].{key} is required and must be a string")
    return value


def _substitute_root(value: str, root: Path) -> str:
    return value.replace("${PLUGIN_ROOT}", str(root))


def _parse_mcp_servers(plugin: str, root: Path, table: dict) -> tuple[McpServerSpec, ...]:
    servers = table.get("servers", {})
    if not isinstance(servers, dict):
        raise PluginError(f"plugin {plugin!r}: [mcp.servers] must be a table")
    specs = []
    for name, body in servers.items():
        if not isinstance(body, dict):
            raise PluginError(f"plugin {plugin!r}: mcp server {name!r} must be a table")
        substituted = dict(body)
        for key in ("command", "cwd"):
            if isinstance(substituted.get(key), str):
                substituted[key] = _substitute_root(substituted[key], root)
        if isinstance(substituted.get("args"), list):
            substituted["args"] = [
                _substitute_root(a, root) if isinstance(a, str) else a
                for a in substituted["args"]
            ]
        try:
            specs.append(_parse_server(name, substituted, source="plugin"))
        except McpConfigError as exc:
            raise PluginError(f"plugin {plugin!r}: {exc}") from exc
    return tuple(specs)


def _parse_dispatch_hooks(plugin: str, entries: list) -> tuple[DispatchHookDef, ...]:
    defs = []
    for entry in entries:
        name = _require_hook_field(plugin, entry, "name", kind="dispatch hook")
        function = _require_hook_field(plugin, entry, "function", kind="dispatch hook")
        priority = entry.get("priority", 100)
        if not isinstance(priority, int) or not (0 <= priority < 1000):
            raise PluginError(
                f"plugin {plugin!r}: dispatch hook {name!r} priority must be an int < 1000"
                " (1000 is the permission engine, innermost)"
            )
        defs.append(DispatchHookDef(name=name, function=function, priority=priority))
    return tuple(defs)


def _parse_lifecycle_hooks(
    plugin: str, entries: list, warnings: list[str]
) -> tuple[LifecycleHookDef, ...]:
    defs = []
    for entry in entries:
        name = _require_hook_field(plugin, entry, "name", kind="lifecycle hook")
        function = _require_hook_field(plugin, entry, "function", kind="lifecycle hook")
        raw_point = entry.get("point")
        try:
            point = LifecyclePoint(raw_point)
        except ValueError:
            valid = ", ".join(p.value for p in LifecyclePoint)
            raise PluginError(
                f"plugin {plugin!r}: lifecycle hook {name!r} point {raw_point!r}"
                f" is not one of: {valid}"
            ) from None
        if point not in _FIRED_POINTS:
            warnings.append(
                f"plugin {plugin!r}: lifecycle hook {name!r} on {point.value} will"
                " never fire (point not yet emitted by the loop)"
            )
        defs.append(LifecycleHookDef(name=name, function=function, point=point))
    return tuple(defs)


def _require_hook_field(plugin: str, entry: dict, key: str, *, kind: str) -> str:
    if not isinstance(entry, dict):
        raise PluginError(f"plugin {plugin!r}: each {kind} entry must be a table")
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise PluginError(f"plugin {plugin!r}: {kind} entries require a string {key!r}")
    return value


def _parse_subscribers(plugin: str, entries: list) -> tuple[SubscriberDef, ...]:
    defs = []
    for entry in entries:
        name = _require_hook_field(plugin, entry, "name", kind="subscriber")
        module = _require_hook_field(plugin, entry, "module", kind="subscriber")
        function = _require_hook_field(plugin, entry, "function", kind="subscriber")
        defs.append(SubscriberDef(name=name, module=module, function=function))
    return tuple(defs)


def _parse_namespaces(plugin: str, table: dict) -> tuple[str, ...]:
    namespaces = table.get("namespaces", [])
    if not isinstance(namespaces, list) or not all(isinstance(n, str) for n in namespaces):
        raise PluginError(f"plugin {plugin!r}: [emitters].namespaces must be a string array")
    for namespace in namespaces:
        if namespace in RESERVED_NAMESPACES:
            raise PluginError(
                f"plugin {plugin!r}: namespace {namespace!r} is reserved"
                f" ({', '.join(sorted(RESERVED_NAMESPACES))})"
            )
        if not _NAME_RE.fullmatch(namespace) or "__" in namespace:
            raise PluginError(
                f"plugin {plugin!r}: invalid emitter namespace {namespace!r}"
                " (must match [A-Za-z0-9_-]+ without '__')"
            )
    return tuple(namespaces)


def _discover_defs(plugin: str, root: Path, subdir: str, loader):
    directory = root / subdir
    if not directory.is_dir():
        return ()
    defs = []
    for path in sorted(directory.glob("*.md")):
        try:
            defs.append(loader(path))
        except FrontmatterError as exc:
            raise PluginError(f"plugin {plugin!r}: {exc}") from exc
    return tuple(defs)


def _parse_manifest(plugin_dir: Path) -> tuple[Plugin, list[str]]:
    manifest_path = plugin_dir / "plugin.toml"
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PluginError(f"{manifest_path}: TOML error: {exc}") from exc
    except OSError as exc:
        raise PluginError(f"{manifest_path}: {exc}") from exc

    unknown_sections = sorted(set(data) - _MANIFEST_KEYS)
    if unknown_sections:
        raise PluginError(
            f"{manifest_path}: unknown manifest sections: {', '.join(unknown_sections)}"
        )
    table = data.get("plugin")
    if not isinstance(table, dict):
        raise PluginError(f"{manifest_path}: missing [plugin] table")
    unknown = sorted(set(table) - _PLUGIN_KEYS)
    if unknown:
        raise PluginError(f"{manifest_path}: unknown [plugin] keys: {', '.join(unknown)}")
    name = _require_str(str(plugin_dir.name), table, "name")
    if not _NAME_RE.fullmatch(name) or "__" in name:
        raise PluginError(
            f"{manifest_path}: plugin name {name!r} must match [A-Za-z0-9_-]+ without '__'"
        )
    version = _require_str(name, table, "version")
    description = _require_str(name, table, "description")
    depends = table.get("depends", [])
    if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
        raise PluginError(f"plugin {name!r}: depends must be a string array")

    warnings: list[str] = []
    hooks_table = data.get("hooks", {})
    if not isinstance(hooks_table, dict):
        raise PluginError(f"plugin {name!r}: [hooks] must be a table")
    hooks_module = hooks_table.get("module")
    dispatch_hooks = _parse_dispatch_hooks(name, hooks_table.get("dispatch", []))
    lifecycle_hooks = _parse_lifecycle_hooks(name, hooks_table.get("lifecycle", []), warnings)
    if (dispatch_hooks or lifecycle_hooks) and not isinstance(hooks_module, str):
        raise PluginError(f"plugin {name!r}: [hooks].module is required when hooks are declared")

    plugin = Plugin(
        name=name,
        version=version,
        description=description,
        root=plugin_dir,
        depends=tuple(depends),
        skills=_discover_defs(name, plugin_dir, "skills", load_skill),
        commands=_discover_defs(name, plugin_dir, "commands", load_command),
        agents=_discover_defs(name, plugin_dir, "agents", load_agent),
        hooks_module=hooks_module,
        dispatch_hooks=dispatch_hooks,
        lifecycle_hooks=lifecycle_hooks,
        mcp_servers=_parse_mcp_servers(name, plugin_dir, data.get("mcp", {})),
        namespaces=_parse_namespaces(name, data.get("emitters", {})),
        subscribers=_parse_subscribers(name, data.get("subscribers", [])),
    )
    return plugin, warnings


def _order_by_depends(plugins: list[Plugin]) -> list[Plugin]:
    by_name = {p.name: p for p in plugins}
    for plugin in plugins:
        missing = [d for d in plugin.depends if d not in by_name]
        if missing:
            raise PluginError(
                f"plugin {plugin.name!r} depends on missing plugin(s): {', '.join(missing)}"
            )
    ordered: list[Plugin] = []
    placed: set[str] = set()
    remaining = list(plugins)
    while remaining:
        progressed = False
        for plugin in list(remaining):
            if all(d in placed for d in plugin.depends):
                ordered.append(plugin)
                placed.add(plugin.name)
                remaining.remove(plugin)
                progressed = True
        if not progressed:
            cycle = ", ".join(p.name for p in remaining)
            raise PluginError(f"plugin dependency cycle among: {cycle}")
    return ordered


def _check_cross_plugin_collisions(plugins: list[Plugin]) -> None:
    for kind, extract in (
        ("plugin", lambda p: (p.name,)),
        ("skill", lambda p: tuple(s.name for s in p.skills)),
        ("command", lambda p: tuple(c.name for c in p.commands)),
        ("agent", lambda p: tuple(a.name for a in p.agents)),
        ("mcp server", lambda p: tuple(s.name for s in p.mcp_servers)),
        ("emitter namespace", lambda p: p.namespaces),
    ):
        seen: dict[str, str] = {}
        for plugin in plugins:
            for name in extract(plugin):
                if name in seen:
                    raise PluginError(
                        f"{kind} name collision: {name!r} declared by both"
                        f" {seen[name]!r} and {plugin.name!r}"
                    )
                seen[name] = plugin.name


def load_plugins(dirs: Sequence[Path]) -> LoadedPlugins:
    """Discover and validate plugins under each dir (a plugin is a CHILD
    directory containing plugin.toml; anything else is silently skipped).
    Everything that can fail, fails here — loudly."""
    plugins: list[Plugin] = []
    warnings: list[str] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or not (child / "plugin.toml").is_file():
                continue
            plugin, plugin_warnings = _parse_manifest(child)
            plugins.append(plugin)
            warnings.extend(plugin_warnings)
    _check_cross_plugin_collisions(plugins)
    ordered = _order_by_depends(plugins)
    return LoadedPlugins(plugins=ordered, warnings=warnings)
