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
from harness.telemetry import index_envelopes, open_store, open_store_memory
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


def test_retries_indexed_for_reliability_queries(tmp_path):
    from harness.events import RetryAttempted
    conn = _store(tmp_path)
    index_envelopes(conn, [
        _env(1, SessionStarted()),
        _env(2, RetryAttempted(call_id=CallId("mc9"), attempt=1, reason="Overloaded: busy")),
        _env(3, RetryAttempted(call_id=CallId("mc9"), attempt=2, reason="Overloaded: busy")),
    ])
    rows = conn.execute("SELECT call_id, attempt, reason FROM retries ORDER BY seq").fetchall()
    assert rows == [("mc9", 1, "Overloaded: busy"), ("mc9", 2, "Overloaded: busy")]


# --- Task 2: Lenient rebuild + run rollups ---

def _write_log(base, sid, envelopes, torn_tail=''):
    sessions = base / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    lines = [e.model_dump_json() for e in envelopes]
    text = "\n".join(lines) + "\n" + torn_tail
    (sessions / f"{sid}.jsonl").write_text(text)


def test_rebuild_indexes_all_sessions_and_skips_torn_tails(tmp_path):
    from harness.telemetry import rebuild_index
    _write_log(tmp_path, "a", [
        Envelope(session_id=SessionId("a"), seq=1, ts=1.0, event=SessionStarted()),
    ], torn_tail='{"v": 1, "torn')
    _write_log(tmp_path, "b", [
        Envelope(session_id=SessionId("b"), seq=1, ts=1.0,
                 event=SessionStarted(parent_session_id=SessionId("a"), parent_seq=1)),
    ])
    conn, warnings = rebuild_index(tmp_path)
    assert warnings == []  # torn tail is NORMAL (live session), not a warning
    sids = sorted(r[0] for r in conn.execute("SELECT session_id FROM sessions"))
    assert sids == ["a", "b"]


def test_rebuild_skips_and_warns_on_corrupt_session(tmp_path):
    from harness.telemetry import rebuild_index
    _write_log(tmp_path, "good", [
        Envelope(session_id=SessionId("good"), seq=1, ts=1.0, event=SessionStarted()),
    ])
    sessions = tmp_path / "sessions"
    (sessions / "bad.jsonl").write_text('{"complete json": "but not an envelope"}\n')
    conn, warnings = rebuild_index(tmp_path)
    assert len(warnings) == 1 and "bad.jsonl" in warnings[0]
    assert [r[0] for r in conn.execute("SELECT session_id FROM sessions")] == ["good"]


def test_rebuild_is_destructive(tmp_path):
    from harness.telemetry import open_store, rebuild_index
    db = tmp_path / "telemetry.db"
    conn = open_store(db)
    conn.execute(
        "INSERT INTO sessions (session_id, started_ts) VALUES ('stale', 0)"
    )
    conn.commit()
    conn.close()
    conn, _ = rebuild_index(tmp_path)  # no sessions dir -> empty store
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_run_sessions_follows_descendants_recursively(tmp_path):
    from harness.telemetry import open_store, run_sessions
    conn = open_store(tmp_path / "t.db")
    for sid, parent in (("root", None), ("kid", "root"), ("grandkid", "kid"), ("other", None)):
        conn.execute(
            "INSERT INTO sessions (session_id, parent_session_id, started_ts) VALUES (?,?,1)",
            (sid, parent),
        )
    assert sorted(run_sessions(conn, "root")) == ["grandkid", "kid", "root"]
    assert run_sessions(conn, "other") == ["other"]


def test_run_rollup_aggregates_across_descendants(tmp_path):
    from harness.telemetry import open_store, index_envelopes, run_rollup
    conn = open_store(tmp_path / "t.db")
    pricing = {"input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}
    index_envelopes(conn, [
        Envelope(session_id=SessionId("root"), seq=1, ts=1.0, event=SessionStarted()),
        _mcc(2, inp=100, out=10, pricing=pricing).model_copy(
            update={"session_id": SessionId("root")}),
        Envelope(session_id=SessionId("kid"), seq=1, ts=2.0,
                 event=SessionStarted(parent_session_id=SessionId("root"), parent_seq=3)),
        _mcc(2, inp=50, out=5, pricing=pricing).model_copy(
            update={"session_id": SessionId("kid")}),
        Envelope(session_id=SessionId("root"), seq=9, ts=9.0,
                 event=SessionOutcome(status="ok", score=1.0, note="")),
    ])
    rollup = run_rollup(conn, "root")
    assert rollup["sessions"] == 2
    assert rollup["input_tokens"] == 150 and rollup["output_tokens"] == 15
    assert abs(rollup["cost"] - 165e-6) < 1e-12
    assert rollup["outcome"] == "ok" and rollup["score"] == 1.0
    assert rollup["retries"] == 0


def test_run_rollup_unknown_root_raises(tmp_path):
    import pytest
    from harness.telemetry import open_store, run_rollup
    conn = open_store(tmp_path / "t.db")
    with pytest.raises(KeyError, match="no such session"):
        run_rollup(conn, "typo-sid")


# --- Task 4: Query summaries + text renderers ---

def test_stats_summary_groups_and_filters_by_tag(tmp_path):
    from harness.telemetry import index_envelopes, open_store, stats_summary
    conn = open_store(tmp_path / "t.db")
    pricing = {"input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}
    index_envelopes(conn, [
        Envelope(session_id=SessionId("a"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("a"), seq=2, ts=1.0,
                 event=CustomEvent(namespace="harness", name="tag", data={"tag": "exp:x"})),
        _mcc(3, model="m1", inp=100, out=10, pricing=pricing).model_copy(
            update={"session_id": SessionId("a")}),
        Envelope(session_id=SessionId("b"), seq=1, ts=1.0, event=SessionStarted()),
        _mcc(2, model="m2", inp=999, out=99).model_copy(
            update={"session_id": SessionId("b")}),
    ])
    everything = stats_summary(conn)
    assert everything["sessions"] == 2 and len(everything["models"]) == 2
    tagged = stats_summary(conn, tag="exp:x")
    assert tagged["sessions"] == 1
    assert len(tagged["models"]) == 1 and tagged["models"][0][0] == "m1"


def test_render_stats_is_readable(tmp_path):
    from harness.telemetry import render_stats
    text = render_stats({
        "sessions": 2,
        "retries": 1,
        "models": [("m1", 3, 300, 30, 0, 0.00033), ("m2", 1, 999, 99, 0, None)],
        "tools": [("bash", 4, 1, 1, 2)],
    })
    assert "sessions: 2" in text
    assert "retries: 1" in text
    assert "m1" in text and "300" in text and "$0.000330" in text
    assert "m2" in text and "n/a" in text      # unpriced cost renders n/a, never 0
    assert "bash" in text and "errors=1" in text and "blocked=1" in text and "asked=2" in text


def test_render_compare_shows_deltas(tmp_path):
    from harness.telemetry import render_compare
    a = {"root": "aaa", "sessions": 2, "model_calls": 3, "input_tokens": 300,
         "output_tokens": 30, "cache_read_tokens": 0, "cost": 0.0003, "model_ms": 500,
         "tool_calls": 4, "tool_errors": 1, "blocked": 0, "asked": 1, "retries": 2,
         "outcome": "ok", "score": 1.0}
    b = {"root": "bbb", "sessions": 1, "model_calls": 1, "input_tokens": 100,
         "output_tokens": 10, "cache_read_tokens": 0, "cost": None, "model_ms": 100,
         "tool_calls": 1, "tool_errors": 0, "blocked": 0, "asked": 0, "retries": 0,
         "outcome": None, "score": None}
    text = render_compare(a, b)
    assert "aaa" in text and "bbb" in text
    assert "input_tokens" in text and "300" in text and "100" in text
    assert "outcome" in text and "ok" in text and "-" in text  # absent renders "-"


# --- Task 6: TelemetrySubscriber ---

def test_subscriber_drains_live_session_into_store(tmp_path):
    # sync on purpose: put_nowait/get_nowait need no running loop (py3.10+)
    from harness.session import Session
    from harness.telemetry import TelemetrySubscriber, open_store
    from harness.events import UserMessage
    conn = open_store(tmp_path / "live.db")
    sub = TelemetrySubscriber(conn)
    with Session(tmp_path, SessionId("live1")) as session:
        queue = session.bus.subscribe()
        session.start()
        session.append(UserMessage(text="hi"))
        drained = sub.drain(queue)
    assert drained == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id = 'live1'"
    ).fetchone()[0] == 1


def test_run_rollup_outcome_is_roots_only(tmp_path):
    from harness.telemetry import index_envelopes, open_store, run_rollup
    conn = open_store(tmp_path / "t.db")
    index_envelopes(conn, [
        Envelope(session_id=SessionId("root"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("kid"), seq=1, ts=2.0,
                 event=SessionStarted(parent_session_id=SessionId("root"), parent_seq=1)),
        Envelope(session_id=SessionId("kid"), seq=99, ts=3.0,
                 event=SessionOutcome(status="fail", score=0.0, note="subtask")),
        Envelope(session_id=SessionId("root"), seq=5, ts=4.0,
                 event=SessionOutcome(status="ok", score=1.0, note="run verdict")),
    ])
    rollup = run_rollup(conn, "root")
    assert rollup["outcome"] == "ok"  # the kid's high-seq fail must not shadow


def test_render_strips_ansi_from_names(tmp_path):
    from harness.telemetry import render_stats
    text = render_stats({
        "sessions": 1, "retries": 0,
        "models": [("\x1b[2Jevil-model", 1, 1, 1, 0, None)],
        "tools": [("\x1b[31mred-tool", 1, 0, 0, 0)],
    })
    assert "\x1b" not in text
    assert "evil-model" in text and "red-tool" in text


# --- Task 8: origin column + per-server stats ---

def test_tool_origin_derived_from_mcp_prefix(tmp_path):
    from harness.telemetry import rebuild_index
    _write_log(tmp_path, "s_mcp", [
        Envelope(session_id=SessionId("s_mcp"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("s_mcp"), seq=2, ts=2.0,
                 event=ToolCallProposed(call_id=CallId("c1"), tool=ToolName("mcp__github__search"), args={})),
        Envelope(session_id=SessionId("s_mcp"), seq=3, ts=3.0,
                 event=ToolCallProposed(call_id=CallId("c2"), tool=ToolName("echo"), args={})),
    ])
    conn, _ = rebuild_index(tmp_path)
    by_tool = dict(conn.execute("SELECT tool, origin FROM tool_calls").fetchall())
    assert by_tool["mcp__github__search"] == "github"
    assert by_tool["echo"] is None


def test_origin_handles_underscored_server_and_tool_names(tmp_path):
    from harness.telemetry import rebuild_index
    _write_log(tmp_path, "s_us", [
        Envelope(session_id=SessionId("s_us"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("s_us"), seq=2, ts=2.0,
                 event=ToolCallProposed(call_id=CallId("c1"), tool=ToolName("mcp__my_server__do_thing"), args={})),
    ])
    conn, _ = rebuild_index(tmp_path)
    by_tool = dict(conn.execute("SELECT tool, origin FROM tool_calls").fetchall())
    assert by_tool["mcp__my_server__do_thing"] == "my_server"


def test_origin_edge_cases(tmp_path):
    from harness.telemetry import rebuild_index
    _write_log(tmp_path, "s_edge", [
        Envelope(session_id=SessionId("s_edge"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("s_edge"), seq=2, ts=2.0,
                 event=ToolCallProposed(call_id=CallId("c1"), tool=ToolName("mcp__"), args={})),
        Envelope(session_id=SessionId("s_edge"), seq=3, ts=3.0,
                 event=ToolCallProposed(call_id=CallId("c2"), tool=ToolName("mcp__noseparator"), args={})),
        Envelope(session_id=SessionId("s_edge"), seq=4, ts=4.0,
                 event=ToolCallProposed(call_id=CallId("c3"), tool=ToolName("mcp____x"), args={})),
    ])
    conn, _ = rebuild_index(tmp_path)
    by_tool = dict(conn.execute("SELECT tool, origin FROM tool_calls").fetchall())
    assert by_tool["mcp__"] is None
    assert by_tool["mcp__noseparator"] is None
    assert by_tool["mcp____x"] is None  # empty server segment must not pass IS NOT NULL


def test_stats_render_shows_mcp_servers_section(tmp_path):
    from harness.telemetry import rebuild_index, stats_summary, render_stats
    _write_log(tmp_path, "s_srv", [
        Envelope(session_id=SessionId("s_srv"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("s_srv"), seq=2, ts=2.0,
                 event=ToolCallProposed(call_id=CallId("c1"), tool=ToolName("mcp__github__search"), args={})),
        Envelope(session_id=SessionId("s_srv"), seq=3, ts=3.0,
                 event=ToolCallCompleted(call_id=CallId("c1"), result_text="ok", is_error=False, duration_ms=10)),
    ])
    conn, _ = rebuild_index(tmp_path)
    summary = stats_summary(conn)
    rendered = render_stats(summary)
    assert "mcp servers" in rendered
    assert "github" in rendered


def test_stats_render_omits_mcp_section_when_no_mcp_tools(tmp_path):
    from harness.telemetry import rebuild_index, stats_summary, render_stats
    _write_log(tmp_path, "s_nomcp", [
        Envelope(session_id=SessionId("s_nomcp"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("s_nomcp"), seq=2, ts=2.0,
                 event=ToolCallProposed(call_id=CallId("c1"), tool=ToolName("bash"), args={})),
    ])
    conn, _ = rebuild_index(tmp_path)
    summary = stats_summary(conn)
    rendered = render_stats(summary)
    assert "mcp servers" not in rendered


def test_stats_summary_mcp_servers_respects_tag_filter(tmp_path):
    from harness.telemetry import rebuild_index, stats_summary
    # session "s_a" tagged "exp:y" has mcp__alpha__ tool
    # session "s_b" untagged has mcp__beta__ tool
    # tag filter should return only alpha
    _write_log(tmp_path, "s_a", [
        Envelope(session_id=SessionId("s_a"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("s_a"), seq=2, ts=1.0,
                 event=CustomEvent(namespace="harness", name="tag", data={"tag": "exp:y"})),
        Envelope(session_id=SessionId("s_a"), seq=3, ts=2.0,
                 event=ToolCallProposed(call_id=CallId("c1"), tool=ToolName("mcp__alpha__op"), args={})),
    ])
    _write_log(tmp_path, "s_b", [
        Envelope(session_id=SessionId("s_b"), seq=1, ts=1.0, event=SessionStarted()),
        Envelope(session_id=SessionId("s_b"), seq=2, ts=2.0,
                 event=ToolCallProposed(call_id=CallId("c2"), tool=ToolName("mcp__beta__op"), args={})),
    ])
    conn, _ = rebuild_index(tmp_path)
    tagged = stats_summary(conn, tag="exp:y")
    server_names = [row[0] for row in tagged["mcp_servers"]]
    assert server_names == ["alpha"]
    assert "beta" not in server_names


# --- Task 9: open_store_memory ---

def test_open_store_memory_has_schema():
    conn = open_store_memory()
    conn.execute("SELECT origin FROM tool_calls LIMIT 0")   # schema exists
