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
    parameters: dict[str, Any]  # JSON Schema


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def __call__(self, args: dict[str, Any]) -> str: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[ToolName, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def get(self, name: ToolName) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(str(name)) from None

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(t.spec for t in self._tools.values())
