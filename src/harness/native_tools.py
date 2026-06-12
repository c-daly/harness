"""Native tools: read/write/edit/glob/grep/bash. Workspace-confined; raise on failure (L1).

Failure modes raise ToolError (the dispatcher renders "tool error: <msg>", is_error=True,
never blob-spilled). Informational non-failures return strings. Blocking I/O offloads via
asyncio.to_thread; bash uses an async subprocess (see BashTool, Task 5).
"""

import asyncio
import fnmatch as _fnmatch
import os
import re
import signal
import weakref
from pathlib import Path
from typing import Any

from harness.hooks import Allow, Ask, DispatchDecision, ProposedAction, ProposedToolCall
from harness.permissions import PermissionRule, RuleSet
from harness.tools import ToolSpec
from harness.types import ToolName
from harness.workspace import PATH_ARG, WorkspaceError, resolve_in_workspace

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
        # WARNING: paths must already be canonical. fold.read_paths is as-recorded,
        # canonical only when WorkspaceGuard ran; resume seeding MUST resolve each
        # against the workspace root (resolve_in_workspace) and silently drop
        # unresolvable ones before passing them here (wiring: Task 8).
        self._paths = set(paths or ())

    def mark(self, path: str) -> None:
        self._paths.add(path)

    def was_read(self, path: str) -> bool:
        return path in self._paths


# asyncio.Lock is not loop-bound since Python 3.10; safe to cache across event-loop instances.
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
        tmp = path.with_name(path.name + _WRITE_TMP_SUFFIX)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)  # atomic
        except OSError as exc:
            raise ToolError(
                f"could not write {path}: {exc.strerror or exc}. Check that the path is writable "
                f"and that the filesystem is not full."
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)  # no-op after a successful replace; cleans leaks on failure
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
        tmp = path.with_name(path.name + _WRITE_TMP_SUFFIX)
        try:
            tmp.write_text(updated, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ToolError(
                f"could not write {path}: {exc.strerror or exc}. Check that the path is writable."
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)  # no-op after a successful replace; cleans leaks on failure
        self._rs.mark(str(path))
        return f"Edited {path}. Snippet of the result:\n{self._snippet(updated, new)}"

    def _snippet(self, text: str, needle: str) -> str:
        lines = text.split("\n")
        # needle is guaranteed present (we just replaced old->new), so the 0 fallback
        # is unreachable by construction; it only satisfies the default-value argument.
        idx = next((i for i, ln in enumerate(lines) if needle.split("\n")[0] in ln), 0)
        lo = max(0, idx - _EDIT_SNIPPET_CONTEXT)
        hi = min(len(lines), idx + _EDIT_SNIPPET_CONTEXT + 1)
        return _format_numbered(lines[lo:hi], lo + 1)


_IGNORE_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
_GLOB_CAP = 100
_GREP_FILE_CAP = 100
_GREP_LINE_CAP = 100
_GREP_LINE_MAX = 250


def _safe_mtime(p: Path) -> float:
    """mtime for sort ordering, TOCTOU-tolerant: a file deleted between the walk and
    the sort (another process, a temp cleaner, test teardown) sorts to the bottom
    instead of raising FileNotFoundError (an OSError) past the tool boundary."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _walk_workspace(base: Path):
    """Yield files under base, skipping the shared ignore set. The ONE walk glob and
    grep share so their world-views never diverge (R-C6)."""
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            # the walk never follows symlinks — a link pointing outside the root would
            # leak content past the L2 confinement (file links included: grep reads what
            # the walk yields).
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _IGNORE_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                yield entry


class GlobTool:
    def __init__(self, *, workspace_root: Path) -> None:
        self._root = workspace_root
        self.spec = ToolSpec(
            name=ToolName("glob"),
            description=(
                "Find files by glob pattern, newest first. Patterns: *, ?, [..] match "
                "within a path component; a bare name pattern like *.py matches files "
                "by name at any depth; **/ matches one or more leading directories (not "
                "zero). Searches the workspace root (or path). Skips .git, node_modules, "
                "__pycache__, .venv. Returns absolute paths."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        base = _resolve(self._root, args["path"]) if args.get("path") else self._root.resolve()
        return await asyncio.to_thread(self._glob, base, str(args["pattern"]))

    def _glob(self, base: Path, pattern: str) -> str:
        if not base.exists():
            raise ToolError(f"search path does not exist: {base}")
        matched = [
            p
            for p in _walk_workspace(base)
            if _fnmatch.fnmatch(str(p.relative_to(base)), pattern)
            or _fnmatch.fnmatch(p.name, pattern)
        ]
        matched.sort(key=_safe_mtime, reverse=True)
        if not matched:
            return f"No files matched {pattern!r} under {base}."
        shown = matched[:_GLOB_CAP]
        body = "\n".join(str(p) for p in shown)
        if len(matched) > _GLOB_CAP:
            body += (
                f"\n\nShowing {_GLOB_CAP} of {len(matched)} matches (newest first). "
                f"Narrow the pattern or path."
            )
        return body


class GrepTool:
    def __init__(self, *, workspace_root: Path) -> None:
        self._root = workspace_root
        self.spec = ToolSpec(
            name=ToolName("grep"),
            description=(
                "Search file contents by regular expression (Python re syntax: no lookbehind/"
                "backreferences). output_mode files_with_matches (default) or content. Optional "
                "glob filter. Skips the same ignore set as glob. Returns absolute paths."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content"],
                    },
                    "case_insensitive": {"type": "boolean", "default": False},
                },
                "required": ["pattern"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        base = _resolve(self._root, args["path"]) if args.get("path") else self._root.resolve()
        return await asyncio.to_thread(self._grep, base, args)

    def _grep(self, base: Path, args: dict[str, Any]) -> str:
        if not base.exists():
            raise ToolError(f"search path does not exist: {base}")
        flags = re.IGNORECASE if args.get("case_insensitive") else 0
        pattern = str(args["pattern"])
        try:
            rx = re.compile(pattern, flags)
        except re.error as exc:
            raise ToolError(
                f"invalid regex: {pattern!r} — {exc}. Python re syntax does not support "
                f"lookbehind or backreferences; restructure the pattern."
            ) from exc
        file_glob = args.get("glob")
        mode = args.get("output_mode", "files_with_matches")
        if base.is_dir():
            files = list(_walk_workspace(base))
        else:
            files = [base]
        if file_glob:
            files = [p for p in files if _fnmatch.fnmatch(p.name, file_glob)]
        if mode == "content":
            return self._grep_content(rx, files, pattern, base)
        return self._grep_files(rx, files, pattern, base)

    def _grep_files(self, rx, files, pattern, base) -> str:
        hits = []
        for p in files:
            try:
                if rx.search(p.read_text(encoding="utf-8", errors="replace")):
                    hits.append(str(p))
            except OSError:
                continue
            if len(hits) >= _GREP_FILE_CAP:
                break
        if not hits:
            return f"No matches found for {pattern!r} in {base} (respecting the ignore set)."
        body = "\n".join(hits)
        if len(hits) >= _GREP_FILE_CAP:
            body += (
                f"\n\nShowing first {_GREP_FILE_CAP} files with matches; more exist. "
                f"Narrow the pattern or path."
            )
        return body

    def _grep_content(self, rx, files, pattern, base) -> str:
        out, total = [], 0
        for p in files:
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                if rx.search(line):
                    shown = line if len(line) <= _GREP_LINE_MAX else line[:_GREP_LINE_MAX] + "…"
                    out.append(f"{p}:{n}:{shown}")
                    total += 1
                    if total >= _GREP_LINE_CAP:
                        break
            if total >= _GREP_LINE_CAP:
                break
        if not out:
            return f"No matches found for {pattern!r} in {base} (respecting the ignore set)."
        body = "\n".join(out)
        if total >= _GREP_LINE_CAP:
            body += (
                f"\n\nShowing {_GREP_LINE_CAP} matching lines (cap). Refine the pattern, add a "
                f"glob filter, or search a subdirectory."
            )
        return body


# ---------------------------------------------------------------------------
# BashTool
# ---------------------------------------------------------------------------

_BASH_DEFAULT_TIMEOUT_MS = 120_000
_BASH_MAX_TIMEOUT_MS = 600_000
_BASH_OUTPUT_CAP = 30_000  # chars; head ~10k + tail ~20k (L3)
_BASH_HEAD = 10_000
_SCRUB_SUFFIX = "_API_KEY"  # never-literals: we name the SHAPE, not specific secrets


def _scrubbed_env() -> dict[str, str]:
    """Inherit the harness env minus any *_API_KEY the provider layer holds (R-C12), plus
    non-interactive defaults. Names the shape of secret vars, never embeds a literal."""
    env = {k: v for k, v in os.environ.items() if not k.endswith(_SCRUB_SUFFIX)}
    env.update(
        {
            "NO_COLOR": "1",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "CI": "true",
        }
    )
    return env


def _truncate_output(text: str) -> str:
    if len(text) <= _BASH_OUTPUT_CAP:
        return text
    omitted = len(text) - _BASH_OUTPUT_CAP
    head = text[:_BASH_HEAD]
    tail = text[-(_BASH_OUTPUT_CAP - _BASH_HEAD) :]
    return (
        f"{head}\n[... output truncated: {omitted} chars omitted. Re-run with a filter "
        f"(grep/head/tail) for the omitted middle ...]\n{tail}"
    )


class BashTool:
    def __init__(self, *, workspace_root: Path) -> None:
        self._root = workspace_root
        self.spec = ToolSpec(
            name=ToolName("bash"),
            description=(
                "Run a shell command via bash -c at the workspace root. The working directory "
                "resets each call (stateless) \u2014 use absolute paths or cd dir && your-command "
                "compounds. stderr is merged into stdout. Default timeout 120s, max 600s."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _BASH_MAX_TIMEOUT_MS,
                    },
                    "description": {"type": "string"},
                },
                "required": ["command"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("command is empty.")
        timeout_ms = min(
            int(args.get("timeout_ms") or _BASH_DEFAULT_TIMEOUT_MS), _BASH_MAX_TIMEOUT_MS
        )
        tokens = command.split()
        # heuristic: best-effort; compound forms suppress the teachback
        bare_cd = (
            bool(tokens)
            and tokens[0] == "cd"
            and not any(op in command for op in ("&&", ";", "||", "\n"))
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(self._root.resolve()),
                env=_scrubbed_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise ToolError(
                f"could not start bash: {exc}. Check that /bin/bash exists and is executable."
            ) from exc
        try:
            # communicate() buffers the full output in memory before truncation
            # (accepted v1 trade per the plan's resolved risks).
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            self._kill_group(proc)
            await proc.wait()
            raise ToolError(
                f"command timed out after {timeout_ms}ms (timeout_ms can be raised to "
                f"{_BASH_MAX_TIMEOUT_MS}). The process group was killed."
            ) from None
        except asyncio.CancelledError:
            self._kill_group(proc)
            # shield the reap: a second cancellation during the (near-instant,
            # post-SIGKILL) wait must not skip it and leave a zombie.
            await asyncio.shield(proc.wait())
            raise
        text = _truncate_output(stdout.decode("utf-8", errors="replace"))
        rc = int(proc.returncode)
        if bare_cd:
            note = (
                "Note: cd has no effect across calls \u2014 the working directory resets each call. "
                "Combine it: cd dir && your-command."
            )
            text = (text + "\n" + note) if text else note
        elif not text:
            text = "(no output)"
        if rc != 0:
            text += f"\nExit code: {rc}"
        return text

    @staticmethod
    def _kill_group(proc) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------------------
# Permission integration (Task 7)
# ---------------------------------------------------------------------------

# Primary-arg table for the compact-form desugarer (frozen, R-Sa6). Paths come from
# workspace.PATH_ARG; bash/glob/grep add their own primary arg.
_PRIMARY_ARG = {**PATH_ARG, "bash": "command", "glob": "pattern", "grep": "pattern"}
_COMPOUND_TOKENS = (";", "&&", "||", "|", "$(", "`", "\n")


def baseline_ruleset() -> RuleSet:
    """Shipped baseline (DATA, not a code denylist, L4): read-only allow, write/edit/bash ask,
    default ask. A user permissions.toml layers OVER this (place it as the innermost provided
    layer so explicit user rules win)."""
    return RuleSet(
        rules=[
            PermissionRule(action="allow", tool="read_file"),
            PermissionRule(action="allow", tool="glob"),
            PermissionRule(action="allow", tool="grep"),
            PermissionRule(action="ask", tool="write_file"),
            PermissionRule(action="ask", tool="edit_file"),
            PermissionRule(action="ask", tool="bash"),
        ],
        default="ask",
    )


def desugar_pattern(pattern: str) -> PermissionRule:
    """Expand Claude-Code-style compact form `tool(argpattern)` into a PermissionRule.
    A bare string with no parens is a tool-name glob (back-compat with todays --allow)."""
    pattern = pattern.strip()
    if pattern.endswith(")") and "(" in pattern:
        tool, inner = pattern[:-1].split("(", 1)
        tool = tool.strip()
        key = _PRIMARY_ARG.get(tool)
        if key is None:
            raise ValueError(
                f"compact grant {pattern!r}: tool {tool!r} has no primary arg; "
                f"use explicit [[rules]] with a match table."
            )
        return PermissionRule(action="allow", tool=tool, match={key: inner})
    return PermissionRule(action="allow", tool=pattern)


class CompoundCommandGuard:
    """Ask hook at priority 950: force a prompt on compound bash commands even when a
    bash(prefix *) allow would match (R-Sa3). Ask survives a later Allow (first-Ask-wins);
    deny stays absolute. Heuristic, NOT containment -- documented."""

    name = "bash-compound-guard"
    priority = 950  # below the engine at 1000, above plugin default 100

    async def __call__(self, action: ProposedAction) -> DispatchDecision:
        if not isinstance(action, ProposedToolCall) or str(action.tool) != "bash":
            return Allow()
        command = str(action.args.get("command", ""))
        if any(tok in command for tok in _COMPOUND_TOKENS):
            return Ask(reason="compound bash command (chained/piped) -- review before running")
        return Allow()
