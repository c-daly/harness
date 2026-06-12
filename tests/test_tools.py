import pytest

from harness.tools import Tool, ToolRegistry, ToolSpec, UnknownToolError
from harness.types import ToolName


class EchoTool:
    spec = ToolSpec(
        name=ToolName("echo"),
        description="Echo the input back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    async def __call__(self, args: dict) -> str:
        return args["text"]


def test_registry_registers_and_gets():
    reg = ToolRegistry()
    tool = EchoTool()
    reg.register(tool)
    assert reg.get(ToolName("echo")) is tool


def test_registry_unknown_tool_raises():
    with pytest.raises(UnknownToolError):
        ToolRegistry().get(ToolName("nope"))


def test_registry_specs():
    reg = ToolRegistry()
    reg.register(EchoTool())
    assert [s.name for s in reg.specs()] == ["echo"]


def test_echo_tool_satisfies_protocol():
    assert isinstance(EchoTool(), Tool)


class OtherTool:
    spec = ToolSpec(
        name=ToolName("other"),
        description="Another tool",
        parameters={"type": "object", "properties": {}},
    )

    async def __call__(self, args: dict) -> str:
        return "other"


def test_filtered_registry_narrows_specs_and_get():
    from harness.tools import FilteredRegistry

    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(OtherTool())
    view = FilteredRegistry(registry, allowed=("echo",))
    assert [str(s.name) for s in view.specs()] == ["echo"]
    with pytest.raises(UnknownToolError):
        view.get(ToolName("other"))
    # narrowing only: a name not in the parent stays unknown even if allowed
    view2 = FilteredRegistry(registry, allowed=("ghost",))
    assert view2.specs() == ()
