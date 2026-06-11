# tests/test_e2e_phase4.py
"""Phase 4 milestone: tagged runs (one spawning a subagent) -> rebuild ->
stats aggregates, run-rollup compare with descendants, user-recorded outcome."""

from harness.cli import build_kernel, run_once
from harness.events import SessionOutcome
from harness.provider import FakeProvider, Usage, text_turn, tool_call_turn
from harness.resume import append_events
from harness.telemetry import rebuild_index, render_compare, render_stats, run_rollup, stats_summary
from harness.types import ModelId, ToolName

PRICING = {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
_U = Usage(input_tokens=100, output_tokens=10)


async def test_telemetry_end_to_end(tmp_path):
    # run A: parent + subagent child, tagged
    kernel_a = build_kernel(
        provider=FakeProvider([
            tool_call_turn("delegating", ToolName("dispatch_agent"), {"prompt": "sub"}, usage=_U),
            text_turn("child done", usage=_U),
            text_turn("parent done", usage=_U),
        ]),
        base_dir=tmp_path, model=ModelId("fake"), pricing=PRICING, tags=["exp:a"],
    )
    sid_a = str(kernel_a.session.id)
    assert await run_once(kernel_a, "go") == "parent done"

    # run B: plain, tagged differently
    kernel_b = build_kernel(
        provider=FakeProvider([text_turn("solo done", usage=_U)]),
        base_dir=tmp_path, model=ModelId("fake"), pricing=PRICING, tags=["exp:b"],
    )
    sid_b = str(kernel_b.session.id)
    assert await run_once(kernel_b, "solo") == "solo done"

    # outcome on run A (closed session, bookkeeping append)
    append_events(tmp_path, kernel_a.session.id, [SessionOutcome(status="ok", score=0.8)])

    conn, warnings = rebuild_index(tmp_path)
    assert warnings == []

    # stats: 3 sessions total (parent A, child of A, B); tag filter narrows
    summary = stats_summary(conn)
    assert summary["sessions"] == 3
    text = render_stats(summary)
    assert "fake" in text and "dispatch_agent" in text
    tagged = stats_summary(conn, tag="exp:a")
    assert tagged["sessions"] == 1  # tags attach to the tagged session only

    # compare: run A includes its child (2 sessions, 3 model calls); B is solo
    rollup_a = run_rollup(conn, sid_a)
    rollup_b = run_rollup(conn, sid_b)
    assert rollup_a["sessions"] == 2 and rollup_b["sessions"] == 1
    assert rollup_a["model_calls"] == 3 and rollup_b["model_calls"] == 1
    assert rollup_a["cost"] is not None and rollup_a["cost"] > rollup_b["cost"]
    assert rollup_a["outcome"] == "ok" and rollup_a["score"] == 0.8
    text = render_compare(rollup_a, rollup_b)
    assert "outcome" in text and "ok" in text


async def test_rebuild_with_live_session_in_progress(tmp_path):
    """A still-open (locked, possibly mid-write) session must not break stats."""
    from harness.events import UserMessage
    from harness.session import Session
    from harness.types import SessionId

    kernel = build_kernel(
        provider=FakeProvider([text_turn("done")]), base_dir=tmp_path, model=ModelId("fake"),
    )
    await run_once(kernel, "finished run")
    live = Session(tmp_path, SessionId("still-open"))
    live.start()
    live.append(UserMessage(text="mid-flight"))
    try:
        conn, warnings = rebuild_index(tmp_path)
        assert warnings == []
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    finally:
        live.close()
