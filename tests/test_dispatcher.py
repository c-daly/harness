# tests/test_dispatcher.py
import pytest

from harness.dispatcher import Dispatcher, ModelDispatchBlocked
from harness.events import (
    DispatchResolved,
    HookDecided,
    ModelCallCompleted,
    PermissionRequested,
    PermissionResolved,
    ToolCallCompleted,
    ToolCallProposed,
)
from harness.hooks import Ask, Block, HookBus, ProposedModelCall, ProposedToolCall, Rewrite
from harness.interaction import HeadlessResolver, ScriptedResolver
from harness.log import read_session
from harness.messages import Message
from harness.provider import FakeProvider, Usage, text_turn
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import CallId, ModelId, SessionId, ToolName


class SafeShell:
    spec = ToolSpec(name=ToolName("safe_shell"), description="safe", parameters={})

    async def __call__(self, args):
        return f"ran: {args['command']}"


class HugeTool:
    spec = ToolSpec(name=ToolName("huge"), description="big output", parameters={})

    async def __call__(self, args):
        return "x" * (64 * 1024)


def _kernel_bits(tmp_path, hooks=None, resolver=None):
    session = Session(tmp_path, SessionId("s1"))
    session.start()
    registry = ToolRegistry()
    registry.register(SafeShell())
    registry.register(HugeTool())
    dispatcher = Dispatcher(
        session=session,
        registry=registry,
        hooks=hooks or HookBus(),
        resolver=resolver or HeadlessResolver(),
    )
    return session, dispatcher


def _events(tmp_path):
    return [e.event for e in read_session(tmp_path, SessionId("s1"), repair=True)]


async def test_allowed_tool_call_executes_and_logs_full_sequence(tmp_path):
    session, dispatcher = _kernel_bits(tmp_path)
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("safe_shell"), args={"command": "ls"})
    )
    session.close()
    assert result.text == "ran: ls" and not result.is_error
    types = [type(e) for e in _events(tmp_path)]
    assert ToolCallProposed in types and DispatchResolved in types and ToolCallCompleted in types


async def test_blocked_tool_call_returns_error_result_and_records_decision(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch("guard", lambda a: Block(reason="not allowed"), priority=10)
    session, dispatcher = _kernel_bits(tmp_path, hooks=hooks)
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("safe_shell"), args={"command": "ls"})
    )
    session.close()
    assert result.is_error and "not allowed" in result.text
    events = _events(tmp_path)
    decided = [e for e in events if isinstance(e, HookDecided)]
    assert decided and decided[0].decision == {"kind": "block", "reason": "not allowed"}
    # blocked: no DispatchResolved, but a Completed error result for transcript pairing
    assert not any(isinstance(e, DispatchResolved) for e in events)


async def test_rewrite_executes_effective_call_and_logs_it(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch(
        "rewriter",
        lambda a: Rewrite(action=ProposedToolCall(
            call_id=a.call_id, tool=ToolName("safe_shell"), args={"command": "ls"})),
        priority=10,
    )
    session, dispatcher = _kernel_bits(tmp_path, hooks=hooks)
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("huge"), args={})
    )
    session.close()
    assert result.text == "ran: ls"
    resolved = [e for e in _events(tmp_path) if isinstance(e, DispatchResolved)]
    assert resolved[0].tool == "safe_shell" and resolved[0].args == {"command": "ls"}


async def test_ask_approved_executes(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch("asker", lambda a: Ask(reason="confirm?"), priority=10)
    session, dispatcher = _kernel_bits(tmp_path, hooks=hooks, resolver=ScriptedResolver([True]))
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("safe_shell"), args={"command": "ok"})
    )
    session.close()
    assert result.text == "ran: ok"
    events = _events(tmp_path)
    assert any(isinstance(e, PermissionRequested) for e in events)
    resolved = [e for e in events if isinstance(e, PermissionResolved)]
    assert resolved[0].allowed is True


async def test_ask_denied_headless_returns_error(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch("asker", lambda a: Ask(reason="confirm?"), priority=10)
    session, dispatcher = _kernel_bits(tmp_path, hooks=hooks)  # HeadlessResolver denies
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("safe_shell"), args={"command": "x"})
    )
    session.close()
    assert result.is_error and "denied" in result.text


async def test_large_result_spills_to_blob_sidecar(tmp_path):
    session, dispatcher = _kernel_bits(tmp_path)
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("huge"), args={})
    )
    session.close()
    assert result.blob is not None and result.text is None
    assert session.blobs.get(result.blob) == b"x" * (64 * 1024)
    completed = [e for e in _events(tmp_path) if isinstance(e, ToolCallCompleted)]
    assert completed[0].result_blob is not None and completed[0].result_text is None


async def test_tool_exception_becomes_typed_error_result(tmp_path):
    class Exploder:
        spec = ToolSpec(name=ToolName("boom"), description="", parameters={})

        async def __call__(self, args):
            raise RuntimeError("kaboom")

    session, dispatcher = _kernel_bits(tmp_path)
    dispatcher.registry.register(Exploder())
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("boom"), args={})
    )
    session.close()
    assert result.is_error and "kaboom" in result.text


async def test_model_dispatch_completes_and_logs_usage(tmp_path):
    session, dispatcher = _kernel_bits(tmp_path)
    provider = FakeProvider([text_turn("hi", usage=Usage(input_tokens=5, output_tokens=1))])
    message, usage = await dispatcher.dispatch_model(
        provider=provider, model=ModelId("fake"), messages=[Message.user_text("x")], tools=()
    )
    session.close()
    assert message.text() == "hi" and usage.input_tokens == 5
    completed = [e for e in _events(tmp_path) if isinstance(e, ModelCallCompleted)]
    assert completed[0].usage["input_tokens"] == 5


async def test_model_dispatch_blockable(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch("budget", lambda a: Block(reason="over budget"), priority=10)
    session, dispatcher = _kernel_bits(tmp_path, hooks=hooks)
    with pytest.raises(ModelDispatchBlocked, match="over budget"):
        await dispatcher.dispatch_model(
            provider=FakeProvider([text_turn("never")]),
            model=ModelId("fake"), messages=[], tools=(),
        )
    session.close()


async def test_cross_type_rewrite_is_refused_for_tools(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch(
        "type-changer",
        lambda a: Rewrite(action=ProposedModelCall(call_id=a.call_id, model=ModelId("other"))),
        priority=10,
    )
    session, dispatcher = _kernel_bits(tmp_path, hooks=hooks)
    result = await dispatcher.dispatch_tool(
        ProposedToolCall(call_id=CallId("c1"), tool=ToolName("safe_shell"), args={"command": "x"})
    )
    session.close()
    assert result.is_error and "action type" in result.text


async def test_cross_type_rewrite_is_refused_for_models(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch(
        "type-changer",
        lambda a: Rewrite(
            action=ProposedToolCall(call_id=a.call_id, tool=ToolName("safe_shell"), args={})
        ),
        priority=10,
    )
    session, dispatcher = _kernel_bits(tmp_path, hooks=hooks)
    with pytest.raises(ModelDispatchBlocked, match="action type"):
        await dispatcher.dispatch_model(
            provider=FakeProvider([text_turn("never")]),
            model=ModelId("fake"), messages=[], tools=(),
        )
    session.close()
