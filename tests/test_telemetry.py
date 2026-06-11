# tests/test_telemetry.py
from harness.events import (
    CustomEvent,
    Envelope,
    HookDecided,
    ModelCallCompleted,
    SessionEnded,
    SessionOutcome,
    SessionResumed,
    SessionStarted,
    ToolCallAborted,
    ToolCallCompleted,
    ToolCallProposed,
)
from harness.telemetry import index_envelopes, open_store
from harness.types import CallId, ModelId, SessionId, ToolName

S = SessionId("s1")


def _env(seq, event, ts=None):
    return Envelope(session_id=S, seq=seq, ts=float(ts if ts is not None else seq), event=event)


def _store(tmp_path):
    return open_store(tmp_path / "telemetry.db")


def _mcc(seq, model="m1", inp=100, out=10, pricing=None, **kw):
    return _env(seq, ModelCallCompleted(
        call_id=CallId(f"mc{seq}"), model=ModelId(model), message={"role": "assistant", "blocks": []},
        usage={"input_tokens": inp, "output_tokens": out, "cache_read_tokens": kw.get("cached", 0),
               "cache_write_tokens": 0},
        pricing=pricing or {}, stop_reason=kw.get("stop", "end_turn"), duration_ms=kw.get("ms", 50),
    ))


def test_session_lifecycle_rows(tmp_path):
    conn = _store(tmp_path)
    index_envelopes(conn, [
        _env(1, SessionStarted(default_model=ModelId("m1"))),
        _env(2, SessionEnded(), ts=9),
        _env(3, SessionResumed()),
        _env(4, SessionEnded(), ts=20),
    ])
    row = conn.execute("SELECT started_ts, ended_ts, resumed_count FROM sessions").fetchone()
    assert row == (1.0, 20.0, 1)  # last SessionEnded wins; one resume counted


def test_model_call_cost_computed_from_stamped_pricing(tmp_path):
    conn = _store(tmp_path)
    pricing = {"input_cost_per_token": 2e-6, "output_cost_per_token": 4e-6}
    index_envelopes(conn, [_env(1, SessionStarted()), _mcc(2, inp=1000, out=100, pricing=pricing)])
    cost, = conn.execute("SELECT cost FROM model_calls").fetchone()
    assert abs(cost - (1000 * 2e-6 + 100 * 4e-6)) < 1e-12


def test_model_call_cost_null_when_unpriced(tmp_path):
    conn = _store(tmp_path)
    index_envelopes(conn, [_env(1, SessionStarted()), _mcc(2)])
    cost, = conn.execute("SELECT cost FROM model_calls").fetchone()
    assert cost is None


def test_tool_call_rows_with_block_and_ask_flags(tmp_path):
    conn = _store(tmp_path)
    index_envelopes(conn, [
        _env(1, SessionStarted()),
        _env(2, ToolCallProposed(call_id=CallId("c1"), tool=ToolName("bash"), args={})),
        _env(3, HookDecided(call_id=CallId("c1"), hook="permissions",
                            decision={"kind": "block", "reason": "no"})),
        _env(4, ToolCallCompleted(call_id=CallId("c1"), result_text="blocked", is_error=True)),
        _env(5, ToolCallProposed(call_id=CallId("c2"), tool=ToolName("deploy"), args={})),
        _env(6, HookDecided(call_id=CallId("c2"), hook="permissions",
                            decision={"kind": "ask", "reason": "?"})),
        _env(7, ToolCallCompleted(call_id=CallId("c2"), result_text="ok", is_error=False,
                                  duration_ms=7)),
    ])
    rows = conn.execute(
        "SELECT tool, is_error, blocked, asked FROM tool_calls ORDER BY call_id"
    ).fetchall()
    assert rows == [("bash", 1, 1, 0), ("deploy", 0, 0, 1)]
    kinds = [r[0] for r in conn.execute("SELECT kind FROM hook_decisions ORDER BY seq")]
    assert kinds == ["block", "ask"]


def test_aborted_tool_call_marks_error(tmp_path):
    conn = _store(tmp_path)
    index_envelopes(conn, [
        _env(1, SessionStarted()),
        _env(2, ToolCallProposed(call_id=CallId("c9"), tool=ToolName("bash"), args={})),
        _env(3, ToolCallAborted(call_id=CallId("c9"), reason="crash")),
    ])
    is_error, = conn.execute("SELECT is_error FROM tool_calls").fetchone()
    assert is_error == 1


def test_tags_extracted_from_harness_namespace_only(tmp_path):
    conn = _store(tmp_path)
    index_envelopes(conn, [
        _env(1, SessionStarted()),
        _env(2, CustomEvent(namespace="harness", name="tag", data={"tag": "exp:a"})),
        _env(3, CustomEvent(namespace="memory", name="tag", data={"tag": "not-a-tag"})),
        _env(4, CustomEvent(namespace="harness", name="tag", data={"tag": "exp:a"})),  # dup ignored
    ])
    tags = [r[0] for r in conn.execute("SELECT tag FROM tags")]
    assert tags == ["exp:a"]


def test_outcomes_recorded_with_scope(tmp_path):
    conn = _store(tmp_path)
    from harness.events import TaskOutcome
    index_envelopes(conn, [
        _env(1, SessionStarted()),
        _env(2, TaskOutcome(status="ok", score=0.5, note="step")),
        _env(3, SessionOutcome(status="fail", score=None, note="overall")),
    ])
    rows = conn.execute("SELECT scope, status, score FROM outcomes ORDER BY seq").fetchall()
    assert rows == [("task", "ok", 0.5), ("session", "fail", None)]


def test_indexing_is_idempotent(tmp_path):
    conn = _store(tmp_path)
    envs = [_env(1, SessionStarted()), _mcc(2)]
    index_envelopes(conn, envs)
    index_envelopes(conn, envs)  # re-index same envelopes (rebuild semantics)
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
