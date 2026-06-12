"""todo fold: last-write-wins, dangling-intent parity, compaction survival, purity."""

from harness.events import (
    CompactionApplied,
    Envelope,
    TodoListUpdated,
    ToolCallProposed,
)
from harness.fold import fold
from harness.types import CallId, SessionId, ToolName

S = SessionId("s1")


def _env(seq, event):
    return Envelope(session_id=S, seq=seq, ts=float(seq), event=event)


def test_empty_log_empty_todos():
    assert fold([]).todos == []


def test_last_write_wins():
    envs = [
        _env(1, TodoListUpdated(items=[{"content": "a", "status": "pending"}])),
        _env(2, TodoListUpdated(items=[{"content": "b", "status": "completed"}])),
    ]
    assert fold(envs).todos == [{"content": "b", "status": "completed"}]


def test_dangling_proposal_without_event_does_not_mutate():
    envs = [
        _env(1, TodoListUpdated(items=[{"content": "a", "status": "pending"}])),
        _env(2, ToolCallProposed(call_id=CallId("c1"), tool=ToolName("todo"), args={"todos": []})),
    ]
    # the proposal carries no TodoListUpdated -> state stays at the last real event
    assert fold(envs).todos == [{"content": "a", "status": "pending"}]


def test_todos_survive_compaction():
    envs = [
        _env(1, TodoListUpdated(items=[{"content": "a", "status": "pending"}])),
        _env(2, CompactionApplied(from_seq=1, to_seq=1, summary="earlier")),
    ]
    # todos derive from the full log, not the post-compaction message tail
    assert fold(envs).todos == [{"content": "a", "status": "pending"}]


def test_fold_is_pure():
    envs = [_env(1, TodoListUpdated(items=[{"content": "a", "status": "pending"}]))]
    assert fold(envs).todos == fold(envs).todos
