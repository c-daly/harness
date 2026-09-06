# src/harness/catalog.py
"""Model catalog: a TOML overlay, not a copy.

Aliases, routes, capability tags, and credentials config live locally;
pricing and context windows resolve from litellm's maintained cost map,
restated locally only for models it doesn't know (local models). Models are
`verified = false` until a conformance suite has passed against recorded
real streams - "litellm routes there" is not "the harness works there"."""

import tomllib
import json
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Literal

from harness.types import ModelId
from harness.resources import LocalProfile, local_endpoint


class UnknownAliasError(Exception):
    pass


class UnknownBackendError(Exception):
    pass


KNOWN_BACKENDS: frozenset[str] = frozenset({"claude-code", "codex", "antigravity"})


@dataclass(frozen=True)
class ResolvedModel:
    alias: str
    route: ModelId
    backend: str | None
    tags: tuple[str, ...]
    api_key_env: str | None
    api_base: str | None
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    max_input_tokens: int | None
    verified: bool
    execution_kind: Literal["inference", "agent"] = "inference"
    local: LocalProfile | None = None

    def pricing_dict(self) -> dict[str, float]:
        """Stamp-ready pricing for ModelCallCompleted; empty when unknown."""
        if self.input_cost_per_token is None or self.output_cost_per_token is None:
            return {}
        return {
            "input_cost_per_token": self.input_cost_per_token,
            "output_cost_per_token": self.output_cost_per_token,
        }


@dataclass
class Catalog:
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return cls(entries=data.get("models", {}))

    def resolve(self, alias: str) -> ResolvedModel:
        try:
            entry = self.entries[alias]
        except KeyError:
            raise UnknownAliasError(alias) from None
        route = entry["route"]
        backend = entry.get("backend")
        if backend is not None and backend not in KNOWN_BACKENDS:
            raise UnknownBackendError(
                f"model {alias!r}: unknown backend {backend!r}; known: {sorted(KNOWN_BACKENDS)}"
            )
        cost_info = _cost_map_lookup(route)
        inferred_kind = "agent" if backend else "inference"
        execution_kind = entry.get("execution_kind", inferred_kind)
        if execution_kind != inferred_kind:
            raise ValueError(
                f"model {alias!r}: execution_kind must be {inferred_kind!r} for this backend"
            )
        local = LocalProfile.model_validate(entry["local"]) if "local" in entry else None
        api_base = entry.get("api_base")
        if local is not None:
            if backend is not None or not route.startswith("openai/"):
                raise ValueError("local profiles require an OpenAI-compatible inference route")
            api_base = local_endpoint(api_base)
        return ResolvedModel(
            alias=alias,
            route=ModelId(route),
            backend=backend,
            tags=tuple(entry.get("tags", ())),
            api_key_env=entry.get("api_key_env"),
            api_base=api_base,
            input_cost_per_token=entry.get(
                "input_cost_per_token", cost_info.get("input_cost_per_token")
            ),
            output_cost_per_token=entry.get(
                "output_cost_per_token", cost_info.get("output_cost_per_token")
            ),
            max_input_tokens=entry.get("max_input_tokens", cost_info.get("max_input_tokens")),
            verified=entry.get("verified", False),
            execution_kind=execution_kind,
            local=local,
        )

    def aliases(self) -> tuple[str, ...]:
        return tuple(self.entries)


@cache
def _packaged_cost_map() -> dict:
    """Read installed metadata without importing a runtime or fetching remote prices."""
    try:
        path = distribution("litellm").locate_file("litellm/model_prices_and_context_window_backup.json")
        cost = json.loads(path.read_text(encoding="utf-8"))
    except (PackageNotFoundError, OSError, ValueError):
        return {}  # Missing metadata means unknown, never zero-cost inference.
    aliases = {}
    for entry in cost.values():
        if isinstance(entry, dict) and isinstance(entry.get("aliases"), list):
            for alias in entry["aliases"]:
                if isinstance(alias, str) and alias not in cost:
                    aliases.setdefault(alias, entry)
    return {**cost, **aliases}


def _cost_map_lookup(route: str) -> dict:
    """Installed snapshot keyed by bare or provider-prefixed names; overrides win."""
    cost = _packaged_cost_map()
    for key in (route, route.split("/", 1)[-1]):
        if key in cost:
            return cost[key]
    return {}
