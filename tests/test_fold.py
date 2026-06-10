from harness.events import (
    CompactionApplied,
    DispatchResolved,
    Envelope,
    ModelCallCompleted,
    SessionStarted,
    ToolCallCompleted,
    ToolCallProposed,
    UserMessage,
)
from harness.fold import fold, resume_repairs
from harness.messages import Message, Role
from harness.types import CallId, ModelId, SessionId, ToolName

S = SessionId("s1")


def _env(seq, event):
    return Envelope(session_id=S, seq=seq, ts=float(seq), event=event)


def _assistant(text: str, *calls) -> dict:
    from harness.messages import TextBlock, ToolCallBlock
    blocks = [TextBlock(text=text)] + [
        ToolCallBlock(call_id=c, tool=ToolName("bash"), args={}) for c in calls
    ]
    return Message(role=Role.ASSISTANT, blocks=tuple(blocks)).model_dump()


def test_fold_builds_transcript():
    envs = [
        _env(1, SessionStarted()),
        _env(2, UserMessage(text="hi")),
        _env(3, ModelCallCompleted(
            call_id=CallId("m1"), model=ModelId("fake"), message=_assistant("hello"), usage={}
        )),
    ]
    state = fold(envs)
    assert [m.role for m in state.messages] == [Role.USER, Role.ASSISTANT]
    assert state.messages[1].text() == "hello"
    assert state.last_seq == 3


def test_fold_appends_tool_results():
    envs = [
        _env(1, SessionStarted()),
        _env(2, UserMessage(text="ls please")),
        _env(3, ModelCallCompleted(
            call_id=CallId("m1"), model=ModelId("fake"),
            message=_assistant("on it", CallId("c1")), usage={},
        )),
        _env(4, ToolCallProposed(call_id=CallId("c1"), tool=ToolName("bash"), args={})),
        _env(5, ToolCallCompleted(call_id=CallId("c1"), result_text="file.txt", is_error=False)),
    ]
    state = fold(envs)
    assert state.messages[-1].role == Role.TOOL
    assert state.open_intents == {}


def test_fold_reports_dangling_intent_and_repairs_synthesize_aborts():
    envs = [
        _env(1, SessionStarted()),
        _env(2, ToolCallProposed(call_id=CallId("c9"), tool=ToolName("bash"), args={})),
        _env(3, DispatchResolved(call_id=CallId("c9"), kind="tool", tool=ToolName("bash"), args={})),
    ]
    state = fold(envs)
    assert CallId("c9") in state.open_intents
    repairs = resume_repairs(state)
    assert len(repairs) == 1
    assert repairs[0].call_id == "c9"
    assert "crash" in repairs[0].reason or "dangling" in repairs[0].reason


def test_fold_applies_compaction_without_resummarizing():
    envs = [
        _env(1, SessionStarted()),
        _env(2, UserMessage(text="old stuff")),
        _env(3, ModelCallCompleted(
            call_id=CallId("m1"), model=ModelId("fake"), message=_assistant("old reply"), usage={}
        )),
        _env(4, CompactionApplied(from_seq=2, to_seq=3, summary="they greeted each other")),
        _env(5, UserMessage(text="new stuff")),
    ]
    state = fold(envs)
    assert state.messages[0].role == Role.SYSTEM
    assert "greeted" in state.messages[0].text()
    assert [m.text() for m in state.messages[1:]] == ["new stuff"]
