"""Explicit extra roots extend native file tools without granting writes to read roots."""

import pytest

from harness.cli import build_kernel, run_once
from harness.events import PermissionRequested, ToolCallCompleted
from harness.interaction import ScriptedResolver
from harness.log import read_session
from harness.native_tools import (EditFileTool, GlobTool, GrepTool, ReadFileTool,
                                  ReadState, ToolError, WriteFileTool)
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName
from harness.workspace import WorkspaceAccess, WorkspaceError
from tests.test_tui import make_app


def _roots(tmp_path):
    roots = [tmp_path / name for name in ("primary", "read", "write", "outside")]
    for root in roots:
        root.mkdir()
    return roots


def test_extra_roots_are_explicit_and_mode_specific(tmp_path):
    primary, read, write, outside = _roots(tmp_path)
    access = WorkspaceAccess(primary, (read,), (write,))
    reordered = WorkspaceAccess(primary, (write, read, read), (write, write))
    assert reordered.read_roots == tuple(sorted((read, write), key=str))
    assert reordered.write_roots == (write,)
    assert access.resolve_path("relative.txt") == primary / "relative.txt"
    assert access.resolve_path(str(read / "file.txt")) == read / "file.txt"
    assert access.resolve_path(str(write / "file.txt"), write=True) == write / "file.txt"
    with pytest.raises(WorkspaceError, match="writable workspace roots"):
        access.resolve_path(str(read / "file.txt"), write=True)
    with pytest.raises(WorkspaceError):
        access.resolve_path(str(outside / "file.txt"))
    (read / "escape").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        access.resolve_path(str(read / "escape" / "file.txt"))
    with pytest.raises(ValueError, match="not an existing directory"):
        WorkspaceAccess(primary, (tmp_path / "missing",))


def test_reordered_roots_have_the_same_handoff_scope(tmp_path):
    from harness.handoff import capture_scope

    primary, read, write, _ = _roots(tmp_path)
    scopes = []
    for index, read_roots in enumerate(((read, write), (write, read))):
        kernel = build_kernel(provider=FakeProvider([text_turn("done")]),
                              base_dir=tmp_path / f"state-{index}", model=ModelId("fake"),
                              native_tools=True, workspace_root=primary,
                              workspace_read_roots=read_roots)
        try:
            scopes.append(capture_scope(kernel.loop.dispatcher))
        finally:
            kernel.session.close()
    assert scopes[0]["workspaces"] == scopes[1]["workspaces"]
    assert scopes[0]["context_bindings"] == scopes[1]["context_bindings"]


async def test_direct_file_tools_enforce_extra_roots(tmp_path):
    primary, read, write, outside = _roots(tmp_path)
    (read / "note.txt").write_text("read me\n")
    (write / "edit.txt").write_text("old\n")
    access = WorkspaceAccess(primary, (read,), (write,))
    state = ReadState()
    common = {"workspace_root": primary, "workspace_access": access}
    reader = ReadFileTool(**common, read_state=state)
    writer = WriteFileTool(**common, read_state=state)
    editor = EditFileTool(**common, read_state=state)
    assert "read me" in await reader({"file_path": str(read / "note.txt")})
    assert str(read / "note.txt") in await GlobTool(**common)({"pattern": "*.txt", "path": str(read)})
    assert str(read / "note.txt") in await GrepTool(**common)({"pattern": "read me", "path": str(read)})
    with pytest.raises(ToolError, match="writable workspace roots"):
        await writer({"file_path": str(read / "note.txt"), "content": "changed"})
    with pytest.raises(ToolError, match="writable workspace roots"):
        await editor({"file_path": str(read / "note.txt"), "old_string": "read", "new_string": "write"})
    with pytest.raises(ToolError):
        await reader({"file_path": str(outside / "secret.txt")})
    await reader({"file_path": str(write / "edit.txt")})
    await editor({"file_path": str(write / "edit.txt"), "old_string": "old", "new_string": "new"})
    assert (write / "edit.txt").read_text() == "new\n"


async def test_kernel_guard_and_permissions_apply_to_extra_roots(tmp_path):
    primary, read, write, _ = _roots(tmp_path)
    (read / "note.txt").write_text("hello\n")
    kernel = build_kernel(
        provider=FakeProvider([
            tool_call_turn("read", ToolName("read_file"), {"file_path": str(read / "note.txt")}),
            tool_call_turn("deny", ToolName("write_file"),
                           {"file_path": str(read / "new.txt"), "content": "no"}),
            tool_call_turn("write", ToolName("write_file"),
                           {"file_path": str(write / "new.txt"), "content": "yes"}),
            text_turn("done"),
        ]),
        base_dir=tmp_path / "state", model=ModelId("fake"), native_tools=True,
        workspace_root=primary, workspace_read_roots=(read,), workspace_write_roots=(write,),
        resolver=ScriptedResolver([True]),
    )
    await run_once(kernel, "go")
    events = [item.event for item in read_session(tmp_path / "state", kernel.session.id)]
    completed = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert [event.is_error for event in completed] == [False, True, False]
    assert len([event for event in events if isinstance(event, PermissionRequested)]) == 1
    assert not (read / "new.txt").exists()
    assert (write / "new.txt").read_text() == "yes"


async def test_tui_clear_retains_extra_roots(tmp_path):
    primary, read, write, _ = _roots(tmp_path)
    app = make_app(tmp_path / "state", native_tools=True, workspace_root=primary,
                   workspace_read_roots=(read,), workspace_write_roots=(write,))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        before = app.kernel.session.id
        prompt = app.query_one("#prompt")
        prompt.value = "/clear"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.kernel.session.id != before
        access = app.kernel.registry.get("read_file")._access
        assert access.read_roots == (read,)
        assert access.write_roots == (write,)
