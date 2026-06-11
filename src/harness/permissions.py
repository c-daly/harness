"""Permission engine: pattern rules behind the Ask channel.

The engine registers as the INNERMOST dispatch hook (priority 1000): plugin
enforcement hooks run first and may Block or Rewrite; the engine rules on the
EFFECTIVE call. Verdicts map onto the existing decision vocabulary
(allow -> Allow, deny -> Block, ask -> Ask); the dispatcher's interaction
channel handles Ask resolution.

Precedence law: deny is absolute — a deny match in ANY layer (including one a
session grant would otherwise shadow) wins before allow/ask is considered.
Otherwise first match in layer order (session grants, then provided layers)
wins; otherwise the first layer declaring a default; otherwise "ask".
Model calls are rule-addressable as pseudo-tool "model:<route>".

Rule-author notes: patterns are fnmatch globs — CASE-SENSITIVE on Linux
("bash" will not match "BASH"); "[...]" is a character CLASS, not a literal
bracket (write "model:expensive/*", never "model:[expensive]*"); arg values
are coerced via str() before matching, so dict/list args match against their
Python repr.
"""

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from harness.hooks import (
    Allow,
    Ask,
    Block,
    DispatchDecision,
    ProposedAction,
    ProposedToolCall,
)

_ACTIONS = ("allow", "deny", "ask")


def _toml_str(value: str) -> str:
    """A TOML basic string literal — naive interpolation would let a quote or
    newline in a tool name corrupt the grants file unparseably."""
    escaped = (
        value.replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
        .replace('\r', '\\r')
        .replace('\t', '\\t')
    )
    return f'"{escaped}"'


@dataclass(frozen=True)
class PermissionRule:
    action: str
    tool: str = "*"  # fnmatch glob on the tool name (or "model:<route>")
    match: Mapping[str, str] = field(default_factory=dict)  # arg name -> glob on str(value)

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError(f"action must be one of {_ACTIONS}, not {self.action!r}")

    def matches(self, tool: str, args: Mapping[str, Any]) -> bool:
        if not fnmatch.fnmatch(tool, self.tool):
            return False
        return all(
            fnmatch.fnmatch(str(args.get(key, "")), pattern)
            for key, pattern in self.match.items()
        )


@dataclass
class RuleSet:
    rules: list[PermissionRule] = field(default_factory=list)
    default: str | None = None  # "allow" | "deny" | "ask" | None (no opinion)

    def __post_init__(self) -> None:
        if self.default is not None and self.default not in _ACTIONS:
            raise ValueError(f"default must be one of {_ACTIONS} or absent, not {self.default!r}")

    @classmethod
    def load(cls, path: Path) -> "RuleSet":
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        rules = [
            PermissionRule(
                action=r["action"], tool=r.get("tool", "*"), match=r.get("match", {})
            )
            for r in data.get("rules", [])
        ]
        return cls(rules=rules, default=data.get("default"))


class PermissionEngine:
    """Layered rules. Register via hooks.register_dispatch(engine.name, engine,
    priority=engine.priority) — __call__ satisfies the dispatch-hook contract."""

    name = "permissions"
    priority = 1000  # innermost: after plugin enforcement hooks

    def __init__(
        self,
        layers: list[RuleSet] | None = None,
        *,
        grants_path: Path | None = None,
    ) -> None:
        self.session_grants = RuleSet()
        self.layers = list(layers or [])
        self._grants_path = grants_path

    def decide(self, tool: str, args: Mapping[str, Any]) -> str:
        layers = (self.session_grants, *self.layers)
        for layer in layers:
            for rule in layer.rules:
                if rule.action == "deny" and rule.matches(tool, args):
                    return "deny"
        for layer in layers:
            for rule in layer.rules:
                if rule.matches(tool, args):
                    return rule.action
        for layer in layers:
            if layer.default is not None:
                return layer.default
        return "ask"

    async def __call__(self, action: ProposedAction) -> DispatchDecision:
        if isinstance(action, ProposedToolCall):
            tool, args = str(action.tool), action.args
        else:
            tool, args = f"model:{action.model}", {}
        verdict = self.decide(tool, args)
        if verdict == "allow":
            return Allow()
        if verdict == "deny":
            return Block(reason=f"permission rule: deny {tool}")
        return Ask(reason=f"permission rule: ask {tool}")

    def grant(
        self, tool: str, match: Mapping[str, str] | None = None, *, persist: bool = False
    ) -> None:
        """Record an always-allow (session scope; persist=True also appends to
        the grants file). Cannot override a deny — decide() checks denies first."""
        rule = PermissionRule(action="allow", tool=tool, match=dict(match or {}))
        self.session_grants.rules.append(rule)
        if persist and self._grants_path is not None:
            self._append_grant(rule)

    def _append_grant(self, rule: PermissionRule) -> None:
        self._grants_path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["", "[[rules]]", 'action = "allow"', f"tool = {_toml_str(rule.tool)}"]
        if rule.match:
            pairs = ", ".join(f"{k} = {_toml_str(v)}" for k, v in rule.match.items())
            lines.append(f"match = {{ {pairs} }}")
        with open(self._grants_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def default_engine(
    project_dir: Path | None = None, config_home: Path | None = None
) -> PermissionEngine | None:
    """Build an engine from the standard locations; None when no config exists
    (legacy allow-all behavior — the engine is strictly opt-in by presence).

    Layer order: project .harness/permissions.toml, then the user grants file,
    then user permissions.toml."""
    config_home = config_home or Path.home() / ".config" / "harness"
    grants_path = config_home / "grants.toml"
    candidates = []
    if project_dir is not None:
        candidates.append(project_dir / ".harness" / "permissions.toml")
    candidates.append(grants_path)
    candidates.append(config_home / "permissions.toml")
    layers = [RuleSet.load(p) for p in candidates if p.exists()]
    if not layers:
        return None
    return PermissionEngine(layers, grants_path=grants_path)
