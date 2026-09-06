"""Tool protocol and registry. The dispatcher is the only thing that calls tools."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from harness.types import ToolName


class UnknownToolError(Exception):
    pass


def validate_arguments(spec: "ToolSpec", args: dict[str, Any]) -> None:
    """Validate the final rewritten arguments without fetching external schema references."""
    def local_references(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("$ref", "$dynamicRef") and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("tool validation: external schema references are unsupported")
                local_references(child)
        elif isinstance(value, list):
            for child in value:
                local_references(child)

    local_references(spec.parameters)
    try:
        Draft202012Validator.check_schema(spec.parameters)
        Draft202012Validator(spec.parameters).validate(args)
    except SchemaError:
        raise ValueError("tool validation: invalid registered schema") from None
    except ValidationError as exc:
        # The exception's message includes submitted values; keep them out of errors.
        raise ValueError(f"tool validation: arguments violate {exc.validator}") from None


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

    def __init__(self, parent: "ToolRegistry | FilteredRegistry", *, allowed: tuple[str, ...]) -> None:
        self._parent = parent
        self._allowed = frozenset(allowed)

    def get(self, name: ToolName) -> Tool:
        if str(name) not in self._allowed:
            raise UnknownToolError(str(name))
        return self._parent.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(s for s in self._parent.specs() if str(s.name) in self._allowed)
