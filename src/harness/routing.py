"""Context-based model routing as a dispatch hook.

Routing rewrites the model of an UNPINNED ProposedModelCall by precedence:
  1. explicit pin (--model / /model / dispatch_agent model= / AgentDef.model) -> untouched
  2. first matching rule -> its target alias
  3. no match, leeway off -> baseline (untouched)
  4. no match, leeway on -> a cheap router alias (the "let the agent decide" mode)

The engine is a dispatch hook (priority 500): it runs AFTER plugin enforcement
hooks (default 100) and BEFORE the permission engine (1000), so the permission
check sees the FINAL routed alias as the pseudo-tool model:<alias>. It is
opt-in by presence of a routing.toml, exactly like the permission engine.

Modeled on permissions.py: layered TOML rule sets, fnmatch globs, first match
in layer order wins.
"""

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from harness.hooks import (
    Allow,
    DispatchDecision,
    ProposedAction,
    ProposedModelCall,
    Rewrite,
)
from harness.types import ModelId


@dataclass(frozen=True)
class RoutingContext:
    """The per-turn signals a rule matches against. Cheap to compute; gathered
    by a closure over the live session at dispatch time."""

    tags: tuple[str, ...] = ()
    prompt: str = ""
    paths: tuple[str, ...] = ()  # in-scope file paths touched this session


@dataclass(frozen=True)
class RoutingRule:
    """All declared signals must match (AND). An absent signal is a wildcard."""

    target: str  # destination alias
    tags: tuple[str, ...] = ()           # match if ANY rule tag is in the context tags
    path_globs: tuple[str, ...] = ()     # match if ANY context path matches ANY glob
    prompt_contains: str | None = None   # case-insensitive substring on the prompt

    def matches(self, ctx: RoutingContext) -> bool:
        if self.tags and not (set(self.tags) & set(ctx.tags)):
            return False
        if self.path_globs and not any(
            fnmatch.fnmatch(p, g) for p in ctx.paths for g in self.path_globs
        ):
            return False
        if self.prompt_contains is not None:
            if self.prompt_contains.lower() not in ctx.prompt.lower():
                return False
        return True


@dataclass
class RoutingRuleSet:
    rules: list[RoutingRule] = field(default_factory=list)
    default: str | None = None     # routable baseline alias (used as the unpinned model)
    leeway: bool = False           # if no rule matches, defer to the router alias
    router: str | None = None      # cheap alias consulted under leeway

    @classmethod
    def load(cls, path: Path) -> "RoutingRuleSet":
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        rules = [
            RoutingRule(
                target=r["target"],
                tags=tuple(r.get("tags", ())),
                path_globs=tuple(r.get("path_globs", ())),
                prompt_contains=r.get("prompt_contains"),
            )
            for r in data.get("rules", [])
        ]
        return cls(
            rules=rules,
            default=data.get("default"),
            leeway=bool(data.get("leeway", False)),
            router=data.get("router"),
        )

    @classmethod
    def merge(cls, sets: list["RoutingRuleSet"]) -> "RoutingRuleSet":
        """Layer user->project: rules concatenate in order; the first set that
        declares default/router/leeway wins (mirrors permissions' layer order)."""
        rules: list[RoutingRule] = []
        default = router = None
        leeway = False
        for s in sets:
            rules.extend(s.rules)
            if default is None:
                default = s.default
            if router is None:
                router = s.router
            leeway = leeway or s.leeway
        return cls(rules=rules, default=default, leeway=leeway, router=router)


def load_routing(
    project_dir: Path | None = None, config_home: Path | None = None
) -> RoutingRuleSet | None:
    """Build a rule set from the standard locations; None when no config exists
    (routing is strictly opt-in by presence). Layer order: project first, then
    user -- a project routing.toml's default/router shadows the user's."""
    config_home = config_home or Path.home() / ".config" / "harness"
    candidates: list[Path] = []
    if project_dir is not None:
        candidates.append(project_dir / ".harness" / "routing.toml")
    candidates.append(config_home / "routing.toml")
    sets = [RoutingRuleSet.load(p) for p in candidates if p.exists()]
    if not sets:
        return None
    return RoutingRuleSet.merge(sets)


class RoutingEngine:
    """Dispatch hook. Register via
    hooks.register_dispatch(engine.name, engine, priority=engine.priority)."""

    name = "routing"
    priority = 500  # after plugin enforcement (100), before permissions (1000)

    def __init__(self, rules: RoutingRuleSet, signals: Callable[[], RoutingContext]) -> None:
        self.rules = rules
        self._signals = signals

    def _route(self, ctx: RoutingContext) -> str | None:
        for rule in self.rules.rules:
            if rule.matches(ctx):
                return rule.target
        if self.rules.leeway and self.rules.router is not None:
            return self.rules.router
        return None

    async def __call__(self, action: ProposedAction) -> DispatchDecision:
        if not isinstance(action, ProposedModelCall):
            return Allow()           # routing only governs model calls
        if action.pinned:
            return Allow()           # explicit choice wins
        target = self._route(self._signals())
        if target is None or target == str(action.model):
            return Allow()
        return Rewrite(
            ProposedModelCall(call_id=action.call_id, model=ModelId(target), pinned=action.pinned)
        )
