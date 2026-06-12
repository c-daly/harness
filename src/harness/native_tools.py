"""Native tools: read/write/edit/glob/grep/bash. Workspace-confined; raise on failure (L1).

Failure modes raise ToolError (the dispatcher renders "tool error: <msg>", is_error=True,
never blob-spilled). Informational non-failures return strings. Blocking I/O offloads via
asyncio.to_thread; bash uses an async subprocess (see BashTool, Task 5).
"""

import asyncio
from pathlib import Path
from typing import Any

from harness.tools import ToolSpec
from harness.types import ToolName
from harness.workspace import WorkspaceError, resolve_in_workspace

# Output caps (L3) — module constants, tunable, not contract.
_READ_DEFAULT_LIMIT = 2000  # lines
_READ_MAX_BYTES = 50_000  # refuse a window larger than this
_READ_MAX_FILE_BYTES = 256 * 1024  # refuse a whole-file read above this with no window
_LINE_MAX_CHARS = 2000  # per-line truncation


class ToolError(Exception):
    """A native-tool failure. The message is model-facing teaching text (L1):
    what failed / why / what to do instead."""


def _resolve(root: Path, raw: object) -> Path:
    try:
        return resolve_in_workspace(root, raw)
    except WorkspaceError as exc:
        raise ToolError(str(exc)) from exc


def _format_numbered(lines: list[str], start: int) -> str:
    out = []
    for i, line in enumerate(lines):
        if len(line) > _LINE_MAX_CHARS:
            line = line[:_LINE_MAX_CHARS] + " … [line truncated]"
        out.append(f"{start + i:6d}\t{line}")
    return "\n".join(out)


class ReadFileTool:
    """read_file: cat -n style, absolute line numbers, offset/limit windowing."""

    def __init__(self, *, workspace_root: Path) -> None:
        self._root = workspace_root
        self.spec = ToolSpec(
            name=ToolName("read_file"),
            description=(
                "Read a text file with line numbers. Accepts an absolute path or one relative "
                "to the workspace root. Use offset (1-based start line) and limit (max lines, "
                "default 2000) to window large files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["file_path"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        path = _resolve(self._root, args.get("file_path", ""))
        offset = int(args["offset"]) if args.get("offset") else 1
        limit = int(args["limit"]) if args.get("limit") else _READ_DEFAULT_LIMIT
        return await asyncio.to_thread(self._read, path, offset, limit, args)

    def _read(self, path: Path, offset: int, limit: int, args: dict[str, Any]) -> str:
        if not path.exists():
            raise ToolError(
                f"file does not exist: {path}. Check the path, or use glob/grep to find it."
            )
        if path.is_dir():
            raise ToolError(f"{path} is a directory. Use glob to list files in it.")
        size = path.stat().st_size
        windowed = args.get("offset") or args.get("limit")
        if not windowed and size > _READ_MAX_FILE_BYTES:
            raise ToolError(
                f"file is {size} bytes, too large to read at once. Pass offset and limit to "
                f"read a range (e.g. offset: 1, limit: 500), or use grep to find the section."
            )
        data = path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        if text == "":
            return "File exists but is empty."
        all_lines = text.split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines = all_lines[:-1]  # drop trailing-newline artifact
        total = len(all_lines)
        if offset > total:
            raise ToolError(f"offset {offset} is beyond the end of the file ({total} lines).")
        window = all_lines[offset - 1 : offset - 1 + limit]
        body = _format_numbered(window, offset)
        if len(body.encode()) > _READ_MAX_BYTES:
            raise ToolError(
                f"the requested window is over {_READ_MAX_BYTES} bytes. Narrow it with a "
                f"smaller limit, or use grep to find the relevant lines."
            )
        last = offset - 1 + len(window)
        if last < total:
            body += f"\n\n(showing lines {offset}–{last} of {total} — use offset to continue)"
        return body
