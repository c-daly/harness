"""todo: whole-list task tracking, event-sourced through TodoListUpdated (L7).

The tool is given an `emit` callable (session.append in production) so it appends its event;
fold reconstructs the list last-write-wins. Whole-list replacement matches Claude Code’s
TodoWrite, so the Phase-9 TodoWrite->todo rewrite is not a hidden degradation.
"""

from typing import Any, Callable

from harness.events import TodoListUpdated
from harness.native_tools import ToolError
from harness.tools import ToolSpec
from harness.types import ToolName

_STATUSES = ("pending", "in_progress", "completed")
_GLYPH = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}


class TodoTool:
    def __init__(self, *, emit: Callable[[TodoListUpdated], Any]) -> None:
        self._emit = emit
        self.spec = ToolSpec(
            name=ToolName("todo"),
            description=(
                "Record the current task list. Pass the COMPLETE list each call (whole-list "
                "replacement); statuses are pending, in_progress, completed. Keep one task "
                "in_progress at a time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "minLength": 1},
                                "status": {"type": "string", "enum": list(_STATUSES)},
                            },
                            "required": ["content", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        raw = args.get("todos")
        if not isinstance(raw, list):
            raise ToolError("todos must be a list of {content, status} objects.")
        items: list[dict[str, Any]] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict) or "content" not in item:
                raise ToolError(f"todos[{i}].content is required.")
            status = item.get("status")
            if status not in _STATUSES:
                raise ToolError(f"todos[{i}].status must be one of {_STATUSES} (got {status!r}).")
            items.append({"content": str(item["content"]), "status": status})
        self._emit(TodoListUpdated(items=items))
        return self._render(items)

    def _render(self, items: list[dict[str, Any]]) -> str:
        if not items:
            return "Todos cleared. (0 done, 0 in progress, 0 pending)"
        lines = ["Todos updated:"]
        done = prog = pend = 0
        for it in items:
            lines.append(f"  {_GLYPH[it['status']]} {it['content']}")
            done += it["status"] == "completed"
            prog += it["status"] == "in_progress"
            pend += it["status"] == "pending"
        lines.append(f"({done} done, {prog} in progress, {pend} pending)")
        if prog > 1:
            lines.append(
                f"Note: {prog} tasks are in_progress; keep one in_progress at a time so "
                f"progress stays legible."
            )
        return "\n".join(lines)
