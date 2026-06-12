"""build_kernel wires natives, the guard hooks, the baseline engine, and ReadState."""

from harness.cli import build_kernel, run_once
from harness.events import PermissionRequested, ToolCallCompleted
from harness.fold import fold
from harness.interaction import ScriptedResolver
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, ToolName


def _kernel(tmp_path, script, **kw):
    return build_kernel(
        provider=FakeProvider(script), base_dir=tmp_path / "data", model=ModelId("fake"),
        workspace_root=tmp_path / "ws", native_tools=True,
        resolver=ScriptedResolver([True] * 8), **kw,
    )


async def test_natives_registered_and_advertised(tmp_path):
    (tmp_path / "ws").mkdir(parents=True)
    kernel = _kernel(tmp_path, [text_turn("hi")])
    names = {str(s.name) for s in kernel.registry.specs()}
    assert {"read_file", "write_file", "edit_file", "glob", "grep", "bash", "todo"} <= names
    # the two pre-existing natives are still present
    assert "dispatch_agent" in names


async def test_read_then_edit_through_full_stack(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "f.txt").write_text("alpha\n")
    kernel = _kernel(tmp_path, [
        tool_call_turn("read", ToolName("read_file"), {"file_path": "f.txt"}),
        tool_call_turn("edit", ToolName("edit_file"),
                       {"file_path": "f.txt", "old_string": "alpha", "new_string": "beta"}),
        text_turn("done"),
    ])
    result = await run_once(kernel, "go")
    assert result == "done"
    assert (ws / "f.txt").read_text() == "beta\n"


async def test_baseline_engine_asks_on_bash_when_no_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    kernel = _kernel(tmp_path, [
        tool_call_turn("run", ToolName("bash"), {"command": "printf hi"}),
        text_turn("done"),
    ])
    await run_once(kernel, "go")
    events = [e.event for e in read_session(tmp_path / "data", kernel.session.id)]
    assert any(isinstance(e, PermissionRequested) for e in events)


async def test_workspace_escape_is_blocked_end_to_end(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    kernel = _kernel(tmp_path, [
        tool_call_turn("peek", ToolName("read_file"), {"file_path": "../../etc/passwd"}),
        text_turn("done"),
    ])
    await run_once(kernel, "go")
    events = [e.event for e in read_session(tmp_path / "data", kernel.session.id)]
    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert completed[0].is_error  # WorkspaceGuard blocked OR the tool raised
