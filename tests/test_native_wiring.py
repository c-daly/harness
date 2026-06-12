"""build_kernel wires natives, the guard hooks, the baseline engine, and ReadState."""

from harness.cli import build_kernel, run_once
from harness.events import PermissionRequested, ToolCallCompleted
from harness.fold import fold
from harness.interaction import ScriptedResolver
from harness.log import read_session
from harness.permissions import PermissionEngine
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.types import ModelId, SessionId, ToolName

# a structurally incomplete JSON line (torn tail) that read_session refuses without repair
GARBAGE_TAIL = '{"seq": 999, torn-incomplete-no-close'


def _kernel(tmp_path, script, **kw):
    return build_kernel(
        provider=FakeProvider(script),
        base_dir=tmp_path / "data",
        model=ModelId("fake"),
        workspace_root=tmp_path / "ws",
        native_tools=True,
        resolver=ScriptedResolver([True] * 8),
        **kw,
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
    kernel = _kernel(
        tmp_path,
        [
            tool_call_turn("read", ToolName("read_file"), {"file_path": "f.txt"}),
            tool_call_turn(
                "edit",
                ToolName("edit_file"),
                {"file_path": "f.txt", "old_string": "alpha", "new_string": "beta"},
            ),
            text_turn("done"),
        ],
    )
    result = await run_once(kernel, "go")
    assert result == "done"
    assert (ws / "f.txt").read_text() == "beta\n"


async def test_baseline_engine_asks_on_bash_when_no_config(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    kernel = _kernel(
        tmp_path,
        [
            tool_call_turn("run", ToolName("bash"), {"command": "printf hi"}),
            text_turn("done"),
        ],
    )
    await run_once(kernel, "go")
    events = [e.event for e in read_session(tmp_path / "data", kernel.session.id)]
    assert any(isinstance(e, PermissionRequested) for e in events)


async def test_workspace_escape_is_blocked_end_to_end(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    kernel = _kernel(
        tmp_path,
        [
            tool_call_turn("peek", ToolName("read_file"), {"file_path": "../../etc/passwd"}),
            text_turn("done"),
        ],
    )
    await run_once(kernel, "go")
    events = [e.event for e in read_session(tmp_path / "data", kernel.session.id)]
    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert completed[0].is_error  # WorkspaceGuard blocked OR the tool raised


async def test_resume_seeds_read_state_no_regate(tmp_path):
    # Seeding law end to end: a file read in the first session must satisfy the
    # read-before-overwrite gate after resume, proving fold.read_paths ->
    # resolve_in_workspace -> ReadState survives the rebuild (cli.py seeding).
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "f.txt").write_text("alpha\n")
    kernel = _kernel(
        tmp_path,
        [
            tool_call_turn("read", ToolName("read_file"), {"file_path": "f.txt"}),
            text_turn("read done"),
        ],
    )
    sid = kernel.session.id
    await run_once(kernel, "read it")
    # the recorded path is canonical (WorkspaceGuard ran); fold sees it as read
    first = fold(list(read_session(tmp_path / "data", sid)))
    assert str((ws / "f.txt").resolve()) in first.read_paths

    resumed = build_kernel(
        provider=FakeProvider(
            [
                tool_call_turn(
                    "write", ToolName("write_file"), {"file_path": "f.txt", "content": "beta\n"}
                ),
                text_turn("wrote"),
            ]
        ),
        base_dir=tmp_path / "data",
        model=ModelId("fake"),
        workspace_root=ws,
        native_tools=True,
        resolver=ScriptedResolver([True] * 8),
        resume_session_id=sid,
    )
    await run_once(resumed, "overwrite it")
    # the write must NOT be gated as unread -- the seeded ReadState covers it
    events = [e.event for e in read_session(tmp_path / "data", sid)]
    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    write_done = completed[-1]
    assert not write_done.is_error
    assert (ws / "f.txt").read_text() == "beta\n"


async def test_double_build_kernel_does_not_duplicate_baseline(tmp_path):
    # A caller-supplied engine reused across rebuilds (the TUI pattern) must gain the
    # baseline layer exactly once -- never accumulate one per build_kernel call.
    (tmp_path / "ws").mkdir(parents=True)
    engine = PermissionEngine([])
    before = len(engine.layers)
    build_kernel(
        provider=FakeProvider([text_turn("a")]),
        base_dir=tmp_path / "data",
        model=ModelId("fake"),
        workspace_root=tmp_path / "ws",
        native_tools=True,
        permissions=engine,
    )
    build_kernel(
        provider=FakeProvider([text_turn("b")]),
        base_dir=tmp_path / "data",
        model=ModelId("fake"),
        workspace_root=tmp_path / "ws",
        native_tools=True,
        permissions=engine,
    )
    assert len(engine.layers) == before + 1


async def test_resume_with_torn_log_seeds_empty(tmp_path):
    # A torn/garbage tail in the session log must not block kernel construction:
    # seeding is best-effort, so the kernel builds (seeded from the valid prefix or
    # empty) rather than raising.
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "f.txt").write_text("alpha\n")
    kernel = _kernel(
        tmp_path,
        [
            tool_call_turn("read", ToolName("read_file"), {"file_path": "f.txt"}),
            text_turn("read done"),
        ],
    )
    sid = kernel.session.id
    await run_once(kernel, "read it")

    # corrupt the tail: append a structurally incomplete (non-JSON) final line
    log_path = tmp_path / "data" / "sessions" / f"{sid}.jsonl"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(GARBAGE_TAIL)

    resumed = build_kernel(
        provider=FakeProvider([text_turn("ok")]),
        base_dir=tmp_path / "data",
        model=ModelId("fake"),
        workspace_root=ws,
        native_tools=True,
        resolver=ScriptedResolver([True] * 8),
        resume_session_id=SessionId(str(sid)),
    )
    # the kernel built without raising; that is the contract under a torn log
    assert resumed.session is not None
    assert resumed.resumed is True
