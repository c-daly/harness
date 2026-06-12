"""Workspace confinement: the single path-normalization law all file tools share."""

import pytest

from harness.hooks import Allow, Block, ProposedModelCall, ProposedToolCall, Rewrite
from harness.types import CallId, ModelId, ToolName
from harness.workspace import WorkspaceError, WorkspaceGuard, resolve_in_workspace


def test_relative_path_resolves_against_root_not_cwd(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    resolved = resolve_in_workspace(root, "sub/file.txt")
    assert resolved == (root / "sub" / "file.txt").resolve()


def test_absolute_path_inside_root_is_accepted(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.txt"
    assert resolve_in_workspace(root, str(target)) == target.resolve()


def test_dotdot_escape_is_blocked(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    with pytest.raises(WorkspaceError) as exc:
        resolve_in_workspace(root, "../secret")
    assert "outside the workspace root" in str(exc.value)


def test_absolute_path_outside_root_is_blocked(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(root, "/etc/passwd")


def test_symlink_pointing_outside_is_blocked(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (root / "link").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(root, "link/secret.txt")


def test_nonexistent_tail_resolves_for_writes(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    # write target does not exist yet; strict=False must not raise
    resolved = resolve_in_workspace(root, "new/deep/file.txt")
    assert resolved == (root / "new" / "deep" / "file.txt").resolve()


def test_empty_and_nul_rejected(tmp_path):
    root = tmp_path
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(root, "")
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(root, "a\x00b")


def test_whitespace_only_rejected(tmp_path):
    root = tmp_path
    with pytest.raises(WorkspaceError):
        resolve_in_workspace(root, "   ")


async def test_guard_rewrites_path_arg_to_canonical(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    guard = WorkspaceGuard(root)
    call = ProposedToolCall(
        call_id=CallId("c1"), tool=ToolName("read_file"), args={"file_path": "sub/a.txt"}
    )
    decision = await guard(call)
    assert isinstance(decision, Rewrite)
    assert decision.action.args["file_path"] == str((root / "sub" / "a.txt").resolve())


async def test_guard_blocks_escape_and_ignores_non_path_tools(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    guard = WorkspaceGuard(root)
    escape = ProposedToolCall(
        call_id=CallId("c2"), tool=ToolName("write_file"), args={"file_path": "../x"}
    )
    assert isinstance(await guard(escape), Block)
    # bash carries no path arg the guard owns -> Allow (untouched)
    bash = ProposedToolCall(call_id=CallId("c3"), tool=ToolName("bash"), args={"command": "ls"})
    assert isinstance(await guard(bash), Allow)
    # model calls -> Allow
    model = ProposedModelCall(call_id=CallId("c4"), model=ModelId("m"))
    assert isinstance(await guard(model), Allow)


async def test_guard_blocks_non_string_path_arg(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    guard = WorkspaceGuard(root)
    call = ProposedToolCall(
        call_id=CallId("c5"), tool=ToolName("read_file"), args={"file_path": 42}
    )
    decision = await guard(call)
    assert isinstance(decision, Block)
    assert "must be a string" in decision.reason


async def test_guard_allows_already_canonical_path(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    guard = WorkspaceGuard(root)
    canonical = str((root / "a.txt").resolve())
    call = ProposedToolCall(
        call_id=CallId("c6"), tool=ToolName("read_file"), args={"file_path": canonical}
    )
    decision = await guard(call)
    assert isinstance(decision, Allow)
