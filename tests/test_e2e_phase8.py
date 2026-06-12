"""Phase 8 milestone: the full native inventory end to end through permissions and resume."""

from harness.cli import build_kernel, run_once
from harness.events import PermissionRequested, TodoListUpdated, ToolCallCompleted
from harness.fold import fold
from harness.interaction import ScriptedResolver
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName


async def test_model_drives_write_edit_bash_todo_through_permissions(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    kernel = build_kernel(
        provider=FakeProvider(
            [
                tool_call_turn(
                    "plan",
                    ToolName("todo"),
                    {"todos": [{"content": "edit greeting", "status": "in_progress"}]},
                ),
                tool_call_turn(
                    "create", ToolName("write_file"), {"file_path": "hi.txt", "content": "hello\n"}
                ),
                tool_call_turn(
                    "tweak",
                    ToolName("edit_file"),
                    {"file_path": "hi.txt", "old_string": "hello", "new_string": "hi"},
                ),
                tool_call_turn("check", ToolName("bash"), {"command": "cat hi.txt"}),
                text_turn("done"),
            ]
        ),
        base_dir=data,
        model=ModelId("fake"),
        workspace_root=ws,
        native_tools=True,
        resolver=ScriptedResolver([True, True, True]),  # write, edit, bash asks
    )
    result = await run_once(kernel, "make a greeting file")
    assert result == "done"
    assert (ws / "hi.txt").read_text() == "hi\n"
    events = [e.event for e in read_session(data, kernel.session.id)]
    assert any(isinstance(e, PermissionRequested) for e in events)
    assert any(isinstance(e, TodoListUpdated) for e in events)
    bash_done = [e for e in events if isinstance(e, ToolCallCompleted)][-1]
    assert "hi" in (bash_done.result_text or "")


async def test_todo_survives_resume(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    k1 = build_kernel(
        provider=FakeProvider(
            [
                tool_call_turn(
                    "plan",
                    ToolName("todo"),
                    {"todos": [{"content": "ship it", "status": "pending"}]},
                ),
                text_turn("ok"),
            ]
        ),
        base_dir=data,
        model=ModelId("fake"),
        workspace_root=ws,
        native_tools=True,
        resolver=ScriptedResolver([True]),
    )
    await run_once(k1, "plan")
    sid = k1.session.id
    k1.session.close()
    # resume: fold the log and assert the todo state reconstructs
    state = fold(list(read_session(data, sid)))
    assert state.todos == [{"content": "ship it", "status": "pending"}]


async def test_deny_write_prevents_side_effect(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "data"
    kernel = build_kernel(
        provider=FakeProvider(
            [
                tool_call_turn(
                    "write", ToolName("write_file"), {"file_path": "blocked.txt", "content": "x"}
                ),
                text_turn("done"),
            ]
        ),
        base_dir=data,
        model=ModelId("fake"),
        workspace_root=ws,
        native_tools=True,
        resolver=ScriptedResolver([False]),  # deny the write ask
    )
    await run_once(kernel, "try to write")
    assert not (ws / "blocked.txt").exists()  # the SAFETY property: deny => no side effect
