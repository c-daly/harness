"""todo: whole-list replacement, validation, rendered echo, event emission."""

import pytest

from harness.events import TodoListUpdated
from harness.todo import TodoTool, ToolError
from harness.types import ToolName


def _tool():
    emitted = []
    tool = TodoTool(emit=emitted.append)
    return tool, emitted


async def test_happy_replace_emits_event_and_renders(tmp_path):
    tool, emitted = _tool()
    out = await tool(
        {
            "todos": [
                {"content": "write parser", "status": "completed"},
                {"content": "wire CLI", "status": "in_progress"},
                {"content": "add tests", "status": "pending"},
            ]
        }
    )
    assert len(emitted) == 1 and isinstance(emitted[0], TodoListUpdated)
    assert emitted[0].items[0]["content"] == "write parser"
    assert "[x] write parser" in out
    assert "[>] wire CLI" in out
    assert "[ ] add tests" in out
    assert "1 done, 1 in progress, 1 pending" in out


async def test_invalid_status_indexed_error(tmp_path):
    tool, _ = _tool()
    with pytest.raises(ToolError) as exc:
        await tool({"todos": [{"content": "x", "status": "done"}]})
    assert "todos[0].status" in str(exc.value)
    assert "done" in str(exc.value)


async def test_empty_list_clears(tmp_path):
    tool, emitted = _tool()
    out = await tool({"todos": []})
    assert isinstance(emitted[0], TodoListUpdated) and emitted[0].items == []
    assert "0 done" in out or "cleared" in out.lower()


async def test_multiple_in_progress_is_soft_warning(tmp_path):
    tool, emitted = _tool()
    out = await tool(
        {
            "todos": [
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ]
        }
    )
    assert len(emitted) == 1  # still succeeded
    assert "in_progress" in out and "keep one" in out


async def test_missing_content_raises(tmp_path):
    tool, _ = _tool()
    with pytest.raises(ToolError) as exc:
        await tool({"todos": [{"status": "pending"}]})
    assert "content" in str(exc.value)


async def test_empty_content_rejected(tmp_path):
    tool, emitted = _tool()
    with pytest.raises(ToolError) as exc:
        await tool({"todos": [{"content": "", "status": "pending"}]})
    assert "todos[0].content" in str(exc.value)
    assert emitted == []
    with pytest.raises(ToolError) as exc:
        await tool({"todos": [{"content": "   ", "status": "pending"}]})
    assert "todos[0].content" in str(exc.value)
    assert emitted == []


async def test_oversize_list_rejected(tmp_path):
    tool, emitted = _tool()
    big = [{"content": f"task {i}", "status": "pending"} for i in range(201)]
    with pytest.raises(ToolError) as exc:
        await tool({"todos": big})
    assert "too large" in str(exc.value)
    assert emitted == []


def test_spec_name():
    tool, _ = _tool()
    assert tool.spec.name == ToolName("todo")
    assert tool.spec.parameters["required"] == ["todos"]
