"""Native tools: read/write/edit/glob/grep/bash. Workspace-confined; raise on failure (L1).

Failure modes raise ToolError (the dispatcher renders "tool error: <msg>", is_error=True,
never blob-spilled). Informational non-failures return strings. Blocking I/O offloads via
asyncio.to_thread; bash uses an async subprocess (see BashTool, Task 5).
"""

import asyncio
import weakref
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
_WRITE_TMP_SUFFIX = ".harness.tmp"
_EDIT_SNIPPET_CONTEXT = 4  # lines before/after the edited region in the returned snippet


class ToolError(Exception):
    """A native-tool failure. The message is model-facing teaching text (L1):
    what failed / why / what to do instead."""


class ReadState:
    """In-process projection of which canonical paths have been read this session.
    Seeded from fold(envelopes).read_paths at build_kernel so the gate survives resume."""

    def __init__(self, paths: set[str] | None = None) -> None:
        self._paths = set(paths or ())

    def mark(self, path: str) -> None:
        self._paths.add(path)

    def was_read(self, path: str) -> bool:
        return path in self._paths


_PATH_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _path_lock(path: str) -> asyncio.Lock:
    lock = _PATH_LOCKS.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _PATH_LOCKS[path] = lock
    return lock


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

    def __init__(self, *, workspace_root: Path, read_state: ReadState | None = None) -> None:
        self._root = workspace_root
        self._rs = read_state
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
        windowed = bool(args.get("offset") or args.get("limit"))
        result = await asyncio.to_thread(self._read, path, offset, limit, windowed)
        if self._rs is not None:
            self._rs.mark(str(path))
        return result

    def _read(self, path: Path, offset: int, limit: int, windowed: bool) -> str:
        if not path.exists():
            raise ToolError(
                f"file does not exist: {path}. Check the path, or use glob/grep to find it."
            )
        if path.is_dir():
            raise ToolError(f"{path} is a directory. Use glob to list files in it.")
        try:
            size = path.stat().st_size
            if not windowed and size > _READ_MAX_FILE_BYTES:
                raise ToolError(
                    f"file is {size} bytes, too large to read at once. Pass offset and limit to "
                    f"read a range (e.g. offset: 1, limit: 500), or use grep to find the section."
                )
            data = path.read_bytes()
        except OSError as exc:
            raise ToolError(
                f"could not read {path}: {exc.strerror or exc}. The file may have restrictive "
                f"permissions or be unreadable. Check access, or use bash (e.g. ls -l) to inspect it."
            ) from exc
        text = data.decode("utf-8", errors="replace")
        if text == "":
            return "File exists but is empty."
        all_lines = text.split("\n")
        if all_lines and all_lines[-1] == "":
            all_lines = all_lines[:-1]  # drop trailing-newline artifact
        total = len(all_lines)
        if offset > total:
            raise ToolError(
                f"offset {offset} is beyond the end of the file ({total} lines). "
                f"Use a smaller offset or omit it to read from the start."
            )
        window = all_lines[offset - 1 : offset - 1 + limit]
        body = _format_numbered(window, offset)
        if len(body.encode()) > _READ_MAX_BYTES:
            raise ToolError(
                f"the requested window is over {_READ_MAX_BYTES} bytes. Narrow it with a "
                f"smaller limit, or use grep to find the relevant lines."
            )
        last = offset - 1 + len(window)
        if last < total:
            body += (
                f"\n\n(showing lines {offset}\u2013{last} of {total} \u2014 use offset to continue)"
            )
        return body


class WriteFileTool:
    def __init__(self, *, workspace_root: Path, read_state: ReadState) -> None:
        self._root = workspace_root
        self._rs = read_state
        self.spec = ToolSpec(
            name=ToolName("write_file"),
            description=(
                "Write a file (create, or overwrite a file already read this session). "
                "Parent directories are created automatically. Path is absolute or "
                "workspace-relative."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        path = _resolve(self._root, args.get("file_path", ""))
        content = str(args.get("content", ""))
        async with _path_lock(str(path)):
            return await asyncio.to_thread(self._write, path, content)

    def _write(self, path: Path, content: str) -> str:
        if path.is_dir():
            raise ToolError(f"{path} is a directory.")
        existed = path.exists()
        if existed and not self._rs.was_read(str(path)):
            raise ToolError(
                f"{path} already exists and has not been read in this session. read_file it "
                f"first so you know what you are overwriting; for a partial change use edit_file."
            )
        prev_lines = (
            len(path.read_text(encoding="utf-8", errors="replace").splitlines()) if existed else 0
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + _WRITE_TMP_SUFFIX)
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)  # atomic
        except OSError as exc:
            raise ToolError(
                f"could not write {path}: {exc.strerror or exc}. Check that the path is writable "
                f"and that the filesystem is not full."
            ) from exc
        self._rs.mark(str(path))  # a write counts as a read for chained edits
        n = len(content.splitlines())
        if existed:
            return f"Overwrote {path} ({n} lines, was {prev_lines})."
        return f"Created {path} ({n} lines)."


class EditFileTool:
    def __init__(self, *, workspace_root: Path, read_state: ReadState) -> None:
        self._root = workspace_root
        self._rs = read_state
        self.spec = ToolSpec(
            name=ToolName("edit_file"),
            description=(
                "Replace an exact substring in a file already read this session. old_string "
                "must match the file byte-for-byte (do NOT include read_file line-number "
                "prefixes). Set replace_all to change every occurrence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        path = _resolve(self._root, args.get("file_path", ""))
        old = str(args.get("old_string", ""))
        new = str(args.get("new_string", ""))
        replace_all = bool(args.get("replace_all", False))
        async with _path_lock(str(path)):
            return await asyncio.to_thread(self._edit, path, old, new, replace_all)

    def _edit(self, path: Path, old: str, new: str, replace_all: bool) -> str:
        if not path.exists():
            raise ToolError(f"file does not exist: {path}. read_file it first, then retry.")
        if not self._rs.was_read(str(path)):
            raise ToolError(
                f"{path} has not been read in this session. read_file it first, then retry."
            )
        if old == new:
            raise ToolError("old_string and new_string are identical; nothing to change.")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(
                f"could not read {path}: {exc.strerror or exc}. Check file permissions."
            ) from exc
        count = text.count(old)
        if count == 0:
            raise ToolError(
                f"old_string not found in {path}. It must match the file exactly, including "
                f"whitespace and indentation. If you copied from read_file output, strip the "
                f"line-number-and-tab prefix from each line."
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string matches {count} locations in {path}. Either add more surrounding "
                f"lines to make it unique, or pass replace_all: true to change every occurrence."
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            tmp = path.with_name(path.name + _WRITE_TMP_SUFFIX)
            tmp.write_text(updated, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ToolError(
                f"could not write {path}: {exc.strerror or exc}. Check that the path is writable."
            ) from exc
        self._rs.mark(str(path))
        return f"Edited {path}. Snippet of the result:\n{self._snippet(updated, new)}"

    def _snippet(self, text: str, needle: str) -> str:
        lines = text.split("\n")
        idx = next((i for i, ln in enumerate(lines) if needle.split("\n")[0] in ln), 0)
        lo = max(0, idx - _EDIT_SNIPPET_CONTEXT)
        hi = min(len(lines), idx + _EDIT_SNIPPET_CONTEXT + 1)
        return _format_numbered(lines[lo:hi], lo + 1)
