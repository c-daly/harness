"""The read-state projection is folded, so the read-before-edit gate survives resume."""

from harness.events import Envelope, ToolCallCompleted, ToolCallProposed
from harness.fold import fold
from harness.messages import FilePresentation
from harness.types import CallId, SessionId, ToolName

S = SessionId("s1")


def _env(seq, event):
    return Envelope(session_id=S, seq=seq, ts=float(seq), event=event)


def test_read_paths_projection_from_completed_read(tmp_path):
    envs = [
        _env(
            1,
            ToolCallProposed(
                call_id=CallId("c1"), tool=ToolName("read_file"), args={"file_path": "/w/a.txt"}
            ),
        ),
        _env(2, ToolCallCompleted(call_id=CallId("c1"), result_text="     1\thi", is_error=False)),
    ]
    state = fold(envs)
    assert "/w/a.txt" in state.read_paths


def test_read_paths_excludes_errored_read(tmp_path):
    envs = [
        _env(
            1,
            ToolCallProposed(
                call_id=CallId("c1"),
                tool=ToolName("read_file"),
                args={"file_path": "/w/missing.txt"},
            ),
        ),
        _env(
            2,
            ToolCallCompleted(
                call_id=CallId("c1"), result_text="tool error: file does not exist", is_error=True
            ),
        ),
    ]
    assert "/w/missing.txt" not in fold(envs).read_paths


def test_operator_links_do_not_seed_agent_read_paths_on_resume():
    events = []
    for purpose, display, operator_view, path in (
        ("context", True, True, "/w/operator.txt"),
        ("context", False, False, "/w/resident.txt"),
        ("context", True, False, "/w/context-display.txt"),
        ("conversation", True, False, "/w/agent.txt"),
    ):
        call = CallId(path)
        events.append(ToolCallProposed(call_id=call, tool=ToolName("read_file"), purpose=purpose,
                                       args={"file_path": path, "display": display}))
        events.append(ToolCallCompleted(call_id=call, result_text="source", operator_view=operator_view, presented_file=(
            FilePresentation(call_id=call, file_path=path) if display else None)))
    state = fold([_env(seq, event) for seq, event in enumerate(events, start=1)])
    assert state.read_paths == {"/w/resident.txt", "/w/context-display.txt", "/w/agent.txt"}


def test_write_completion_marks_path_read(tmp_path):
    envs = [
        _env(
            1,
            ToolCallProposed(
                call_id=CallId("c2"), tool=ToolName("write_file"), args={"file_path": "/w/new.txt"}
            ),
        ),
        _env(
            2,
            ToolCallCompleted(
                call_id=CallId("c2"), result_text="Created /w/new.txt (1 line).", is_error=False
            ),
        ),
    ]
    assert "/w/new.txt" in fold(envs).read_paths
