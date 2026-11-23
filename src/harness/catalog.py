# src/harness/catalog.py
"""Model catalog: a TOML overlay, not a copy.

Aliases, routes, capability tags, and credentials config live locally;
pricing and context windows resolve from litellm's maintained cost map,
restated locally only for models it doesn't know (local models). Models are
`verified = false` until a conformance suite has passed against recorded
real streams - "litellm routes there" is not "the harness works there"."""

import tomllib
import json
import errno
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from harness.types import ModelId
from harness.resources import LocalProfile, local_endpoint


class UnknownAliasError(Exception):
    pass


class CatalogConfigError(ValueError):
    """Safe to display: identify the source and field without echoing any value."""


class UnknownBackendError(CatalogConfigError):
    pass


KNOWN_BACKENDS: frozenset[str] = frozenset({"claude-code", "codex", "antigravity"})
_MAX_CATALOG_BYTES = 2 * 1024 * 1024


def claude_cli_model_name(route: str) -> str | None:
    """Extract the bounded model argument accepted by the subscription transports."""
    name = route.removeprefix("claude-cli/")
    return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,180}", name) else None


def read_catalog_source(path: Path, *, follow_symlinks: bool = True) -> bytes:
    """A catalog can be edited mid-read. Check the open file and bound the actual read."""
    flags = os.O_RDONLY | os.O_NONBLOCK
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP and not follow_symlinks:
            raise CatalogConfigError(f"{path}: choose the catalog's actual path rather than a symlink") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CatalogConfigError(f"{path}: the catalog must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(_MAX_CATALOG_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_CATALOG_BYTES:
        raise CatalogConfigError(f"{path}: catalog exceeds the 2 MiB limit; reduce its size")
    return raw


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
    usage_accounting: Literal["reported", "unknown"] = "unknown"
    transport: Literal["claude-cli"] | None = None

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
    path: Path | None = field(default=None, compare=False, repr=False)

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        return cls.from_bytes(read_catalog_source(path), path=path)

    @classmethod
    def from_bytes(cls, source: bytes, *, path: Path) -> "Catalog":
        """Parse the same bytes a caller inspected, including its source in diagnostics."""
        try:
            data = tomllib.loads(source.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            # On Python 3.12 the parser exposes line/column only in the message.
            # The rest of the message can quote configuration content.
            position = re.search(r"\(at line (\d+), column (\d+)\)$", str(exc))
            location = f" at line {position[1]}, column {position[2]}" if position else ""
            raise CatalogConfigError(f"{path}: invalid TOML{location}; fix the file syntax") from None
        except UnicodeDecodeError:
            raise CatalogConfigError(f"{path}: save the TOML catalog as UTF-8") from None
        entries = data.get("models", {})
        if not isinstance(entries, dict):
            raise CatalogConfigError(f"{path}: models must be a table of [models.<alias>] entries")
        catalog = cls(entries=entries, path=path)
        for alias in entries:
            catalog._entry(alias)
        return catalog

    def _field(self, alias: str) -> str:
        key = alias if re.fullmatch(r"[\w-]+", alias) else ascii(alias)
        return f"{self.path or 'catalog'}: models.{key}"

    def _entry(self, alias: str) -> dict:
        """Validate shapes before pricing, indexing, or iterating over user data."""
        entry = self.entries[alias]
        field = self._field(alias)
        if not alias.strip():
            raise CatalogConfigError(f"{self.path or 'catalog'}: give the model alias a name")
        if not isinstance(entry, dict):
            raise CatalogConfigError(f"{field} must be a table containing a route")
        for key in ("route", "api_key_env", "api_base", "backend"):
            value = entry.get(key)
            if value is None and key != "route":
                continue
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise CatalogConfigError(f"{field}.{key} must be a nonempty string")
        if "tags" in entry:
            tags = entry["tags"]
            if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
                raise CatalogConfigError(f"{field}.tags must be an array of strings")
        if "verified" in entry and type(entry["verified"]) is not bool:
            raise CatalogConfigError(f"{field}.verified must be true or false")
        limit = entry.get("max_input_tokens")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise CatalogConfigError(f"{field}.max_input_tokens must be a positive integer")
        for key in ("input_cost_per_token", "output_cost_per_token"):
            cost = entry.get(key)
            if cost is not None and (type(cost) not in (int, float) or not 0 <= cost <= sys.float_info.max):
                raise CatalogConfigError(f"{field}.{key} must be a finite, nonnegative number")
        if "local" in entry and not isinstance(entry["local"], dict):
            raise CatalogConfigError(f"{field}.local must be a table of local runtime settings")
        return entry

    def resolve(self, alias: str) -> ResolvedModel:
        try:
            entry = self._entry(alias)
        except KeyError:
            raise UnknownAliasError(alias) from None
        route = entry["route"]
        backend = entry.get("backend")
        if backend is not None and backend not in KNOWN_BACKENDS:
            raise UnknownBackendError(
                f"{self._field(alias)}.backend: use one of {', '.join(sorted(KNOWN_BACKENDS))}"
            )
        transport = entry.get("transport")
        if transport is not None:
            if transport != "claude-cli":
                raise CatalogConfigError(f"{self._field(alias)}.transport: supported model CLI is claude-cli")
            if any(entry.get(key) is not None for key in ("backend", "api_base", "api_key_env", "local")):
                raise CatalogConfigError(f"{self._field(alias)}: a CLI model transport owns its own connection")
            if not route.startswith("claude-cli/") or claude_cli_model_name(route) is None:
                raise CatalogConfigError(f"{self._field(alias)}.route: use claude-cli/default or claude-cli/MODEL")
        elif route.startswith("claude-cli/"):
            raise CatalogConfigError(f'{self._field(alias)}: set transport = "claude-cli" for this route')
        # A subscription call is not priced by the API token rate.
        cost_info = {} if transport else _cost_map_lookup(route)
        inferred_kind = "agent" if backend else "inference"
        execution_kind = entry.get("execution_kind", inferred_kind)
        if execution_kind != inferred_kind:
            raise CatalogConfigError(
                f"{self._field(alias)}.execution_kind must be {inferred_kind!r} for this backend"
            )
        try:
            local = LocalProfile.model_validate(entry["local"]) if "local" in entry else None
        except ValidationError as exc:
            error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
            location = error["loc"]
            key = f".{location[0]}" if location and location[0] in LocalProfile.model_fields else ""
            # An extra-field error can include arbitrary text in its location.
            raise CatalogConfigError(f"{self._field(alias)}.local{key}: {error['msg']}") from None
        usage_accounting = entry.get("usage_accounting", "unknown")
        if usage_accounting not in ("reported", "unknown"):
            raise CatalogConfigError(f"{self._field(alias)}.usage_accounting must be reported or unknown")
        api_base = entry.get("api_base")
        if local is not None:
            if backend is not None or not route.startswith("openai/"):
                raise CatalogConfigError(f"{self._field(alias)}.local requires an OpenAI-compatible inference route")
            try:
                api_base = local_endpoint(api_base)
            except ValueError:
                raise CatalogConfigError(
                    f"{self._field(alias)}.api_base must be a loopback HTTP(S) URL without credentials or query"
                ) from None
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
            usage_accounting=usage_accounting,
            transport=transport,
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
