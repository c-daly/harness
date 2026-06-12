"""Tool protocol and registry. The dispatcher is the only thing that calls tools."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from harness.types import ToolName


class UnknownToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    description: str
    parameters: dict[str, Any]  # JSON Schema; frozen protects the ref, not the dict contents


# runtime_checkable verifies attribute presence only; __call__ being async is not checked
@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def __call__(self, args: dict[str, Any]) -> str: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[ToolName, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Silent overwrite on name collision — callers own
        name uniqueness (the plugin loader will make collisions loud)."""
        self._tools[tool.spec.name] = tool

    def get(self, name: ToolName) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(str(name)) from None

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(t.spec for t in self._tools.values())


class FilteredRegistry:
    """Read-only narrowed view for agent definitions: restricts advertisement
    (specs) AND execution (get) without touching the parent registry. Narrows
    only — the shared HookBus enforcement still applies to children."""

    def __init__(self, parent: ToolRegistry, *, allowed: tuple[str, ...]) -> None:
        self._parent = parent
        self._allowed = frozenset(allowed)

    def get(self, name: ToolName) -> Tool:
        if str(name) not in self._allowed:
            raise UnknownToolError(str(name))
        return self._parent.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(s for s in self._parent.specs() if str(s.name) in self._allowed)
