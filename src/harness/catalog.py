# src/harness/catalog.py
"""Model catalog: a TOML overlay, not a copy.

Aliases, routes, capability tags, and credentials config live locally;
pricing and context windows resolve from litellm's maintained cost map,
restated locally only for models it doesn't know (local models). Models are
`verified = false` until a conformance suite has passed against recorded
real streams - "litellm routes there" is not "the harness works there"."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from harness.types import ModelId


class UnknownAliasError(Exception):
    pass


@dataclass(frozen=True)
class ResolvedModel:
    alias: str
    route: ModelId
    tags: tuple[str, ...]
    api_key_env: str | None
    api_base: str | None
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    max_input_tokens: int | None
    verified: bool

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
        cost_info = _cost_map_lookup(route)
        return ResolvedModel(
            alias=alias,
            route=ModelId(route),
            tags=tuple(entry.get("tags", ())),
            api_key_env=entry.get("api_key_env"),
            api_base=entry.get("api_base"),
            input_cost_per_token=entry.get(
                "input_cost_per_token", cost_info.get("input_cost_per_token")
            ),
            output_cost_per_token=entry.get(
                "output_cost_per_token", cost_info.get("output_cost_per_token")
            ),
            max_input_tokens=entry.get("max_input_tokens", cost_info.get("max_input_tokens")),
            verified=entry.get("verified", False),
        )

    def aliases(self) -> tuple[str, ...]:
        return tuple(self.entries)


def _cost_map_lookup(route: str) -> dict:
    """litellm.model_cost keyed by bare or provider-prefixed names; try both."""
    import litellm  # deferred: importing litellm is slow; only catalog users pay it

    cost = litellm.model_cost
    for key in (route, route.split("/", 1)[-1]):
        if key in cost:
            return cost[key]
    return {}
