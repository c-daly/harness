"""Workspace confinement: the one path-normalization law all file tools share.

resolve_in_workspace is the HARD floor, called inside every file tool at exec
time (defense-in-depth even when the engine is absent). WorkspaceGuard is the
SOFT layer: a dispatch hook at priority 900 (before the engine at 1000) that
rewrites the path arg to its canonical absolute form so the engine, the resolver
prompt, DispatchResolved, and the tool all see ONE canonical path. Out-of-root
from the guard is a Block (fail closed). bash is NOT path-confined.
"""

from pathlib import Path

from harness.hooks import Allow, Block, DispatchDecision, ProposedAction, ProposedToolCall, Rewrite

# tool -> the single arg key carrying a path the workspace law owns (frozen: the
# permission desugarer primary-arg table mirrors this).
PATH_ARG: dict[str, str] = {
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "glob": "path",
    "grep": "path",
}


class WorkspaceError(Exception):
    """Raised by resolve_in_workspace; tools let it surface as a ToolError-equivalent."""


def resolve_in_workspace(root: Path, raw: object) -> Path:
    """Resolve raw against the workspace root, confined to it. See Law L2."""
    text = str(raw)
    if not text:
        raise WorkspaceError("path is empty; pass a file path relative to the workspace root")
    if "\x00" in text:
        raise WorkspaceError("path contains a NUL byte")
    candidate = root / Path(text)  # absolute text wins; relative resolves against root
    resolved = candidate.resolve(strict=False)
    root_r = root.resolve()
    if not (resolved == root_r or resolved.is_relative_to(root_r)):
        raise WorkspaceError(
            f"path resolves outside the workspace root: {resolved}. Pass a path inside {root_r}."
        )
    return resolved


class WorkspaceGuard:
    """Priority-900 rewrite hook: canonicalize the path arg before the engine."""

    name = "workspace-guard"
    priority = 900  # before the permission engine at 1000

    def __init__(self, root: Path) -> None:
        self._root = root

    async def __call__(self, action: ProposedAction) -> DispatchDecision:
        if not isinstance(action, ProposedToolCall):
            return Allow()
        key = PATH_ARG.get(str(action.tool))
        if key is None or key not in action.args:
            return Allow()
        try:
            resolved = resolve_in_workspace(self._root, action.args[key])
        except WorkspaceError as exc:
            return Block(reason=str(exc))
        new_args = dict(action.args)
        new_args[key] = str(resolved)
        return Rewrite(
            action=ProposedToolCall(call_id=action.call_id, tool=action.tool, args=new_args)
        )
