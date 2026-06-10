from harness.events import (
    CompactionApplied,
    DispatchResolved,
    Envelope,
    HookDecided,
    SessionStarted,
    TaskOutcome,
    ToolCallCompleted,
    ToolCallProposed,
    UnknownEvent,
    UserMessage,
    parse_envelope_line,
)
from harness.types import SCHEMA_VERSION, CallId, SessionId, ToolName


def _env(event) -> Envelope:
    return Envelope(session_id=SessionId("s1"), seq=1, ts=1000.0, event=event)


def test_envelope_round_trips_tool_call_proposed():
    ev = ToolCallProposed(call_id=CallId("c1"), tool=ToolName("bash"), args={"command": "ls"})
    line = _env(ev).model_dump_json()
    parsed = parse_envelope_line(line)
    assert isinstance(parsed.event, ToolCallProposed)
    assert parsed.event.tool == "bash"
    assert parsed.v == SCHEMA_VERSION
    assert parsed.seq == 1


def test_intent_events_know_they_are_intents():
    intent = ToolCallProposed(call_id=CallId("c1"), tool=ToolName("bash"), args={})
    fact = ToolCallCompleted(call_id=CallId("c1"), result_text="ok", is_error=False, duration_ms=1)
    assert intent.is_intent and not fact.is_intent


def test_unknown_event_type_is_preserved_not_dropped():
    line = (
        '{"v": 99, "session_id": "s1", "seq": 7, "ts": 1.0,'
        ' "event": {"type": "from_the_future", "payload": 42}}'
    )
    parsed = parse_envelope_line(line)
    assert isinstance(parsed.event, UnknownEvent)
    assert parsed.event.raw["type"] == "from_the_future"
    assert parsed.event.raw["payload"] == 42
    assert parsed.seq == 7


def test_hook_decided_carries_full_decision_payload():
    ev = HookDecided(
        call_id=CallId("c1"),
        hook="agent-swarm.enforce",
        decision={"kind": "rewrite", "tool": "safe_bash", "args": {"command": "ls"}},
    )
    parsed = parse_envelope_line(_env(ev).model_dump_json())
    assert parsed.event.decision["kind"] == "rewrite"
    assert parsed.event.decision["args"] == {"command": "ls"}


def test_compaction_carries_summary_and_range():
    ev = CompactionApplied(from_seq=3, to_seq=40, summary="earlier: setup discussion", model=None)
    parsed = parse_envelope_line(_env(ev).model_dump_json())
    assert parsed.event.summary.startswith("earlier")
    assert (parsed.event.from_seq, parsed.event.to_seq) == (3, 40)


def test_outcome_event():
    ev = TaskOutcome(status="ok", score=0.9, judge=None, note="manual")
    parsed = parse_envelope_line(_env(ev).model_dump_json())
    assert parsed.event.status == "ok"


def test_session_started_carries_parent_linkage():
    ev = SessionStarted(parent_session_id=SessionId("p1"), parent_seq=12, default_model=None)
    parsed = parse_envelope_line(_env(ev).model_dump_json())
    assert parsed.event.parent_session_id == "p1"
    assert parsed.event.parent_seq == 12


def test_dispatch_resolved_records_effective_call():
    ev = DispatchResolved(
        call_id=CallId("c1"), kind="tool", tool=ToolName("safe_bash"), args={"command": "ls"}, model=None
    )
    parsed = parse_envelope_line(_env(ev).model_dump_json())
    assert parsed.event.tool == "safe_bash"


def test_user_message():
    parsed = parse_envelope_line(_env(UserMessage(text="hi")).model_dump_json())
    assert parsed.event.text == "hi"
