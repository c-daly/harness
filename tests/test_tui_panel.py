"""Pure fold helpers for the activity panel -- no Textual, synthetic
envelopes built from the real event classes."""

from harness.events import (
    Envelope,
    SubagentFinished,
    SubagentSpawned,
    ToolCallCompleted,
    ToolCallProposed,
)
from harness.tui_panel import FileRow, fold_agents, fold_files
from harness.types import ModelId, SessionId, ToolName, new_call_id, new_session_id

_SID = SessionId("s1")


def env(seq: int, event) -> Envelope:
    return Envelope(session_id=_SID, seq=seq, ts=float(seq), event=event)


# --- fold_files ---


def test_fold_files_read_then_edit_same_path_is_one_row_with_both_markers():
    cid1, cid2 = new_call_id(), new_call_id()
    events = [
        env(
            1,
            ToolCallProposed(call_id=cid1, tool=ToolName("read_file"), args={"file_path": "a.py"}),
        ),
        env(2, ToolCallCompleted(call_id=cid1, result_text="content")),
        env(
            3,
            ToolCallProposed(call_id=cid2, tool=ToolName("edit_file"), args={"file_path": "a.py"}),
        ),
        env(4, ToolCallCompleted(call_id=cid2, result_text="ok")),
    ]
    rows = fold_files(events)
    assert rows == [FileRow(path="a.py", markers=frozenset({"R", "E"}))]


def test_fold_files_newest_touched_path_first():
    c1, c2 = new_call_id(), new_call_id()
    events = [
        env(
            1,
            ToolCallProposed(call_id=c1, tool=ToolName("read_file"), args={"file_path": "old.py"}),
        ),
        env(2, ToolCallCompleted(call_id=c1, result_text="x")),
        env(
            3,
            ToolCallProposed(call_id=c2, tool=ToolName("read_file"), args={"file_path": "new.py"}),
        ),
        env(4, ToolCallCompleted(call_id=c2, result_text="y")),
    ]
    rows = fold_files(events)
    assert [r.path for r in rows] == ["new.py", "old.py"]


def test_fold_files_re_touching_a_path_moves_it_to_front_and_unions_markers():
    c1, c2, c3 = new_call_id(), new_call_id(), new_call_id()
    events = [
        env(
            1, ToolCallProposed(call_id=c1, tool=ToolName("read_file"), args={"file_path": "a.py"})
        ),
        env(2, ToolCallCompleted(call_id=c1, result_text="x")),
        env(
            3, ToolCallProposed(call_id=c2, tool=ToolName("read_file"), args={"file_path": "b.py"})
        ),
        env(4, ToolCallCompleted(call_id=c2, result_text="y")),
        env(
            5, ToolCallProposed(call_id=c3, tool=ToolName("write_file"), args={"file_path": "a.py"})
        ),
        env(6, ToolCallCompleted(call_id=c3, result_text="z")),
    ]
    rows = fold_files(events)
    assert [r.path for r in rows] == ["a.py", "b.py"]
    assert rows[0].markers == frozenset({"R", "W"})


def test_fold_files_ignores_failed_calls():
    c1 = new_call_id()
    events = [
        env(
            1, ToolCallProposed(call_id=c1, tool=ToolName("read_file"), args={"file_path": "a.py"})
        ),
        env(2, ToolCallCompleted(call_id=c1, result_text="boom", is_error=True)),
    ]
    assert fold_files(events) == []


def test_fold_files_ignores_non_file_tools():
    c1 = new_call_id()
    events = [
        env(1, ToolCallProposed(call_id=c1, tool=ToolName("bash"), args={"command": "ls"})),
        env(2, ToolCallCompleted(call_id=c1, result_text="a.py")),
    ]
    assert fold_files(events) == []


# --- fold_agents ---


def test_fold_agents_dispatch_proposal_without_completion_is_running():
    cid = new_call_id()
    events = [
        env(
            1,
            ToolCallProposed(
                call_id=cid, tool=ToolName("dispatch_agent"), args={"prompt": "do it"}
            ),
        ),
    ]
    rows = fold_agents(events)
    assert len(rows) == 1
    assert rows[0].call_id == str(cid)
    assert rows[0].status == "running"
    assert rows[0].model is None
    assert rows[0].strategy is None


def test_fold_agents_dispatch_completion_is_done():
    cid = new_call_id()
    events = [
        env(
            1,
            ToolCallProposed(
                call_id=cid,
                tool=ToolName("dispatch_agent"),
                args={"prompt": "do it", "model": "gpt"},
            ),
        ),
        env(2, ToolCallCompleted(call_id=cid, result_text="ok")),
    ]
    rows = fold_agents(events)
    assert rows[0].status == "done"
    assert rows[0].model == "gpt"


def test_fold_agents_dispatch_error_completion_is_error():
    cid = new_call_id()
    events = [
        env(
            1,
            ToolCallProposed(
                call_id=cid, tool=ToolName("dispatch_agent"), args={"prompt": "do it"}
            ),
        ),
        env(2, ToolCallCompleted(call_id=cid, result_text="boom", is_error=True)),
    ]
    rows = fold_agents(events)
    assert rows[0].status == "error"


def test_fold_agents_ensemble_experts_share_strategy_grouping_key():
    outer = new_call_id()
    child_a, child_b = new_session_id(), new_session_id()
    events = [
        env(
            1,
            ToolCallProposed(
                call_id=outer,
                tool=ToolName("ensemble"),
                args={"prompt": "p", "models": ["alpha", "beta"]},
            ),
        ),
        env(2, SubagentSpawned(child_session_id=child_a, model=ModelId("alpha"))),
        env(3, SubagentSpawned(child_session_id=child_b, model=ModelId("beta"))),
        env(4, SubagentFinished(child_session_id=child_a, status="ok")),
        env(5, SubagentFinished(child_session_id=child_b, status="ok")),
        env(6, ToolCallCompleted(call_id=outer, result_text="combined")),
    ]
    rows = fold_agents(events)
    # exactly two expert rows, no separate row for the outer ensemble call itself
    assert {r.call_id for r in rows} == {str(child_a), str(child_b)}
    strategies = {r.strategy for r in rows}
    assert strategies == {str(outer)}
    assert all(r.status == "done" for r in rows)
    assert {r.model for r in rows} == {"alpha", "beta"}


def test_fold_agents_ensemble_expert_error_status():
    outer = new_call_id()
    child = new_session_id()
    events = [
        env(
            1,
            ToolCallProposed(
                call_id=outer, tool=ToolName("ensemble"), args={"prompt": "p", "models": ["m"]}
            ),
        ),
        env(2, SubagentSpawned(child_session_id=child, model=ModelId("m"))),
        env(3, SubagentFinished(child_session_id=child, status="error")),
        env(4, ToolCallCompleted(call_id=outer, result_text="x")),
    ]
    rows = fold_agents(events)
    assert rows[0].status == "error"


def test_fold_agents_newest_first():
    c1, c2 = new_call_id(), new_call_id()
    events = [
        env(1, ToolCallProposed(call_id=c1, tool=ToolName("dispatch_agent"), args={"prompt": "a"})),
        env(2, ToolCallCompleted(call_id=c1, result_text="x")),
        env(3, ToolCallProposed(call_id=c2, tool=ToolName("dispatch_agent"), args={"prompt": "b"})),
    ]
    rows = fold_agents(events)
    assert [r.call_id for r in rows] == [str(c2), str(c1)]


def test_fold_agents_unrelated_subagent_spawned_outside_any_strategy_is_ignored():
    # dispatch_agent's own SubagentSpawned/Finished must not create a SECOND
    # row -- the outer ToolCallProposed/Completed pair is authoritative.
    cid = new_call_id()
    child = new_session_id()
    events = [
        env(
            1, ToolCallProposed(call_id=cid, tool=ToolName("dispatch_agent"), args={"prompt": "a"})
        ),
        env(2, SubagentSpawned(child_session_id=child, model=ModelId("m"))),
        env(3, SubagentFinished(child_session_id=child, status="ok")),
        env(4, ToolCallCompleted(call_id=cid, result_text="x")),
    ]
    rows = fold_agents(events)
    assert len(rows) == 1
    assert rows[0].call_id == str(cid)
