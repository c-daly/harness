"""A context window narrows requests without erasing history or widening authority."""

import pytest

from harness.context import ContextPolicy, prepare_context
from harness.errors import ContextOverflow
from harness.messages import Message, Role, TextBlock, ToolCallBlock
from harness.types import CallId, ModelId, ToolName


def assistant(text):
    return Message(role=Role.ASSISTANT, blocks=(TextBlock(text=text),))


def test_window_keeps_whole_tool_turn_and_pinned_project_context():
    history = [Message.user_text("old question"), assistant("old answer"),
               Message.user_text("inspect"),
               Message(role=Role.ASSISTANT, blocks=(ToolCallBlock(call_id=CallId("read"),
                   tool=ToolName("read_file"), args={"path": "FACTS.txt"}),)),
               Message.tool_result(CallId("read"), text="fact"),
               Message.user_text("continue")]
    original = list(history)
    pinned = [Message.system_text("instructions"), Message.user_text("project context")]
    prepared = prepare_context(pinned, history, (), ContextPolicy(history_turns=2),
                               max_input_bytes=32768)
    assert all(message in prepared.messages for message in pinned + history[2:])
    assert history[0] not in prepared.messages and history[1] not in prepared.messages
    assert prepared.omitted_turns == 1
    assert history == original
    assert any("1 earlier turn" in m.text() for m in prepared.messages)


def test_byte_budget_drops_old_turns_but_never_current_turn_or_system_context():
    history = [Message.user_text("old"), assistant("x" * 3000),
               Message.user_text("current")]
    prepared = prepare_context([Message.system_text("rules")], history, (),
                               ContextPolicy(history_turns=4), max_input_bytes=1024)
    assert prepared.omitted_turns == 1 and prepared.input_bytes <= 1024
    assert history[-1] in prepared.messages
    with pytest.raises(ContextOverflow):
        prepare_context([Message.system_text("rules" * 1000)], history, (),
                        ContextPolicy(), max_input_bytes=1024)
    with pytest.raises(ContextOverflow):
        prepare_context([], [Message.user_text("current" * 1000)], (),
                        ContextPolicy(), max_input_bytes=1024)


def test_compaction_summary_before_history_is_retained():
    summary = Message.system_text("Earlier work summary")
    prepared = prepare_context([], [summary, Message.user_text("old"),
        assistant("answer"), Message.user_text("current")], (),
        ContextPolicy(history_turns=1), max_input_bytes=4096)
    assert summary in prepared.messages


@pytest.mark.parametrize("config", [
    {"history_turns": 0}, {"history_turns": True}, {"max_input_bytes": 0},
    {"tools": ["*"]}, {"tools": [""]}, {"tools": ["read_file", "read_file"]},
    {"unknown": True},
])
def test_policy_rejects_ambiguous_or_invalid_configuration(config):
    with pytest.raises(ValueError):
        ContextPolicy.model_validate(config)


async def test_profile_bounds_requests_but_resume_preserves_full_history(tmp_path):
    from harness.cli import build_kernel
    from harness.fold import fold
    from harness.log import read_session
    from harness.provider import FakeProvider, text_turn

    provider = FakeProvider([text_turn("one"), text_turn("two"), text_turn("three")])
    policy = ContextPolicy(history_turns=1, tools=())
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"),
                          context_policy=policy)
    try:
        await kernel.loop.start()
        await kernel.loop.run_turn("first")
        await kernel.loop.run_turn("second")
        assert not any(m.text() == "first" for m in provider.calls[-1])
        assert any(m.text() == "first" for m in kernel.loop.history)
        events = read_session(tmp_path, kernel.session.id)
        assert fold(events).messages == kernel.loop.history
        prepared = [e.event for e in events if e.event.type == "context_prepared"]
        assert prepared[-1].omitted_turns == 1 and prepared[-1].tools == ()
        await kernel.loop.end()
    finally:
        kernel.session.close()
    resumed = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"),
                           resume_session_id=kernel.session.id)
    try:
        assert any(m.text() == "first" for m in resumed.loop.history)
        await resumed.loop.run_turn("third")
        assert not any(m.text() in ("first", "second") for m in provider.calls[-1])
        assert len(resumed.loop.history) == 6
    finally:
        resumed.session.close()


async def test_tool_profile_restricts_late_registration_and_rewritten_execution(tmp_path):
    from harness.cli import build_kernel
    from harness.hooks import ProposedToolCall, Rewrite
    from harness.provider import EchoProvider
    from harness.tools import ToolSpec

    called = []

    class Tool:
        def __init__(self, name):
            self.spec = ToolSpec(ToolName(name), name, {"type": "object"})

        async def __call__(self, args):
            called.append(self.spec.name)
            return "ok"

    kernel = build_kernel(base_dir=tmp_path, provider=EchoProvider(), model=ModelId("test"),
                          context_policy=ContextPolicy(tools=("allowed",)))
    try:
        kernel.registry.register(Tool("allowed"))  # MCP registers after construction too
        kernel.registry.register(Tool("hidden"))
        assert [s.name for s in kernel.loop.registry.specs()] == ["allowed"]
        kernel.hooks.register_dispatch("rewrite", lambda c: Rewrite(action=ProposedToolCall(
            call_id=c.call_id, tool=ToolName("hidden"), args=c.args)))
        await kernel.loop.start()
        outcome = await kernel.loop.dispatcher.dispatch_tool(ProposedToolCall(
            call_id=CallId("blocked"), tool=ToolName("allowed"), args={}))
        assert outcome.is_error and not called
    finally:
        kernel.session.close()


async def test_current_turn_overflow_fails_before_provider_call(tmp_path):
    from harness.cli import build_kernel
    from harness.provider import FakeProvider

    provider = FakeProvider([])
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"),
                          context_policy=ContextPolicy(max_input_bytes=256, tools=()))
    try:
        await kernel.loop.start()
        with pytest.raises(ContextOverflow):
            await kernel.loop.run_turn("x" * 1024)
        assert not provider.calls
    finally:
        kernel.session.close()


def test_old_blob_turn_is_dropped_before_reading_large_sidecar(tmp_path, monkeypatch):
    from harness.blobs import BlobStore
    blobs = BlobStore(tmp_path)
    ref = blobs.put(b"x" * 8192)
    history = [Message.user_text("old"),
               Message(role=Role.ASSISTANT, blocks=(ToolCallBlock(
                   call_id=CallId("old-read"), tool=ToolName("read_file"), args={}),)),
               Message.tool_result(CallId("old-read"), blob=ref), Message.user_text("current")]
    monkeypatch.setattr(blobs, "get", lambda ref: pytest.fail("oversized sidecar was read"))
    prepared = prepare_context([], history, (), ContextPolicy(), max_input_bytes=1024, blobs=blobs)
    assert prepared.omitted_turns == 1 and prepared.input_bytes <= 1024


async def test_child_inherits_context_and_cannot_widen_tool_profile(tmp_path):
    from harness.cli import build_kernel
    from harness.frontmatter import AgentDef
    from harness.provider import FakeProvider, text_turn, tool_call_turn
    from harness.tools import ToolSpec
    from harness.log import read_session

    called = []

    class Hidden:
        spec = ToolSpec(ToolName("hidden"), "must not run", {"type": "object"})

        async def __call__(self, args):
            called.append(True)
            return "escaped"

    provider = FakeProvider([tool_call_turn("try", ToolName("hidden"), {}), text_turn("done")])
    policy = ContextPolicy(history_turns=1, tools=("dispatch_agent",))
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("test"),
                          context_policy=policy)
    kernel.registry.register(Hidden())
    kernel.runner.agents["wide"] = AgentDef(name="wide", description="", body="", tools=("hidden",))
    try:
        await kernel.loop.start()
        result = await kernel.runner.run(prompt="child", model=None, parent=kernel.session, agent="wide")
        assert result == "done"
        assert not called
        spawned = next(e.event for e in read_session(tmp_path, kernel.session.id)
                       if e.event.type == "subagent_spawned")
        child_events = read_session(tmp_path, spawned.child_session_id)
        policy_events = [e.event for e in child_events if e.event.type == "context_policy_configured"]
        assert policy_events[0].policy == policy
        prepared = [e.event for e in child_events if e.event.type == "context_prepared"]
        assert prepared and all(e.tools == () for e in prepared)
    finally:
        kernel.session.close()


def test_cli_profile_persists_on_resume_and_can_be_explicitly_cleared(tmp_path, monkeypatch):
    import sys
    from harness.cli import main
    from harness.fold import fold
    from harness.log import read_session
    from harness.sessions import list_sessions

    path = tmp_path / "context.toml"
    path.write_text('history_turns = 1\ntools = []\n')
    base = ["harness", "--base-dir", str(tmp_path), "--no-mcp", "--no-plugins"]
    monkeypatch.setattr(sys, "argv", [*base, "--context-profile", str(path), "-p", "first"])
    main()
    sid = list_sessions(tmp_path)[0].session_id
    monkeypatch.setattr(sys, "argv", [*base, "--continue", "-p", "second"])
    main()
    state = fold(read_session(tmp_path, sid))
    assert state.context_policy == ContextPolicy(history_turns=1, tools=())
    assert len(state.messages) == 4
    monkeypatch.setattr(sys, "argv", [*base, "--continue", "--no-context-profile", "-p", "third"])
    main()
    assert fold(read_session(tmp_path, sid)).context_policy is None


def test_context_events_round_trip_without_changing_old_or_unknown_logs():
    from harness.events import ContextPolicyConfigured, ContextPrepared, Envelope, parse_envelope_line
    from harness.types import SessionId
    for event in (ContextPolicyConfigured(policy=ContextPolicy(tools=())), ContextPrepared()):
        envelope = Envelope(session_id=SessionId("context"), seq=1, ts=1, event=event)
        assert parse_envelope_line(envelope.model_dump_json()) == envelope
    old = '{"session_id":"old","seq":1,"ts":1,"event":{"type":"session_started"}}'
    assert parse_envelope_line(old).event.type == "session_started"
    assert parse_envelope_line(old.replace("session_started", "newer_unknown")).event.type == "unknown"
