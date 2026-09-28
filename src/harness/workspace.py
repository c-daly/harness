"""Configured native file roots and the path-normalization law all file tools share.

WorkspaceAccess.resolve_path is the HARD floor, called inside every file tool
at exec time (defense-in-depth even when the engine is absent).
resolve_in_workspace keeps the original single-root API. WorkspaceGuard is the
SOFT layer: a
dispatch hook at priority 900 (before the engine at 1000) that
rewrites the path arg to its canonical absolute form so the engine, the resolver
prompt, DispatchResolved, and the tool all see ONE canonical path. Paths outside
the configured roots are blocked. bash is NOT path-confined.
"""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from harness.hooks import Allow, Block, DispatchDecision, ProposedAction, ProposedToolCall, Rewrite

# tool -> the single arg key carrying a path the workspace law owns.
#
# Frozen contract: this table is extended only by EDITING THIS LITERAL in later
# tasks (a source-level change), never mutated at runtime -- hence the
# MappingProxyType wrapper, which makes runtime mutation an error rather than a
# silent footgun. The permission desugarer primary-arg table (Task 7) must
# mirror this set manually; the two are kept in sync by review, not by import
# alone, so any edit here is a reminder to update there.
PATH_ARG: MappingProxyType[str, str] = MappingProxyType(
    {
        "read_file": "file_path",
        "write_file": "file_path",
        "edit_file": "file_path",
        "glob": "path",
        "grep": "path",
    }
)


class WorkspaceError(Exception):
    """Raised by resolve_in_workspace; tools let it surface as a ToolError-equivalent."""


@dataclass(frozen=True)
class WorkspaceAccess:
    """Native file-tool roots. Relative paths always use the primary workspace."""

    root: Path
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        for name in ("read_roots", "write_roots"):
            roots = tuple(sorted({Path(path).resolve() for path in getattr(self, name)}, key=str))
            for path in roots:
                if not path.is_dir():
                    raise ValueError(f"{name} entry is not an existing directory: {path}")
            object.__setattr__(self, name, roots)

    def resolve_path(self, raw: object, *, write: bool = False) -> Path:
        text = str(raw)
        if not text.strip():
            raise WorkspaceError("path is empty; pass a file path relative to the workspace root")
        if "\x00" in text:
            raise WorkspaceError("path contains a NUL byte")
        resolved = (self.root / Path(text)).resolve(strict=False)
        roots = (self.root, *self.write_roots)
        if not write:
            roots += self.read_roots
        if any(resolved == root or resolved.is_relative_to(root) for root in roots):
            return resolved
        scope = "writable workspace roots" if write else "workspace root or configured access roots"
        raise WorkspaceError(f"path resolves outside the {scope}: {resolved}. Pass an allowed path.")


def resolve_in_workspace(root: Path, raw: object) -> Path:
    """Resolve raw against the workspace root, confined to it. See Law L2."""
    return WorkspaceAccess(root).resolve_path(raw)


class WorkspaceGuard:
    """Priority-900 rewrite hook: canonicalize the path arg before the engine."""

    name = "workspace-guard"
    priority = 900  # before the permission engine at 1000

    def __init__(self, root: Path, access: WorkspaceAccess | None = None) -> None:
        self._access = access or WorkspaceAccess(root)
        self._root = self._access.root

    async def __call__(self, action: ProposedAction) -> DispatchDecision:
        if not isinstance(action, ProposedToolCall):
            return Allow()
        key = PATH_ARG.get(str(action.tool))
        if key is None or key not in action.args:
            return Allow()
        raw = action.args[key]
        if not isinstance(raw, str):
            return Block(reason=f"path arg {key!r} must be a string, got {type(raw).__name__}")
        try:
            resolved = self._access.resolve_path(raw, write=str(action.tool) in ("write_file", "edit_file"))
        except WorkspaceError as exc:
            return Block(reason=str(exc))
        new_str = str(resolved)
        if new_str == raw:
            return Allow()  # already canonical; no-op rewrite would emit a misleading event
        new_args = dict(action.args)
        new_args[key] = new_str
        return Rewrite(
            action=ProposedToolCall(call_id=action.call_id, tool=action.tool, args=new_args)
        )
