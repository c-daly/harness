"""Regressions for complete execution histories, including internal inference."""

import asyncio
import importlib

import pytest

from harness.dispatcher import Dispatcher, ModelDispatchBlocked
from harness.errors import AuthFailed, Overloaded
from harness.events import ModelCallProposed, PermissionResolved, ToolCallProposed
from harness.fold import fold
from harness.hooks import Ask, Block, HookBus
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.messages import Message
from harness.provider import FakeProvider, collect, text_turn
from harness.resume import resume_session
from harness.session import Session
from harness.telemetry import index_envelopes, open_store_memory, run_rollup
from harness.tools import ToolRegistry
from harness.types import CallId, ModelId, SessionId, ToolName


MODEL_TERMINALS = {
    "model_call_completed", "model_call_failed", "model_call_cancelled", "model_call_aborted"
}


def make_dispatcher(session, hooks=None, resolver=None):
    return Dispatcher(
        session=session, registry=ToolRegistry(), hooks=hooks or HookBus(),
        resolver=resolver or HeadlessResolver(), retry_delays=(),
    )


@pytest.mark.parametrize("failure", [AuthFailed("bad key"), Overloaded("busy"), TimeoutError()])
async def test_model_failure_has_one_terminal_fact(tmp_path, failure):
    class BrokenProvider:
        async def complete(self, **kwargs):
            raise failure
            yield  # pragma: no cover

    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        with pytest.raises(type(failure)):
            await make_dispatcher(session).dispatch_model(
                provider=BrokenProvider(), model=ModelId("fake"), messages=[], tools=(),
            )
    events = read_session(tmp_path, SessionId("s"))
    proposed = [e.event for e in events if e.event.type == "model_call_proposed"]
    terminal = [e.event for e in events if e.event.type in MODEL_TERMINALS]
    assert len(proposed) == len(terminal) == 1
    assert terminal[0].type == "model_call_failed"
    assert terminal[0].call_id == proposed[0].call_id
    assert not fold(events).open_model_intents


async def test_blocked_model_has_terminal_fact_without_provider_call(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch("deny", lambda _: Block(reason="no"))
    provider = FakeProvider([text_turn("must not run")])
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        with pytest.raises(ModelDispatchBlocked):
            await make_dispatcher(session, hooks).dispatch_model(
                provider=provider, model=ModelId("fake"), messages=[], tools=(),
            )
    events = read_session(tmp_path, SessionId("s"))
    terminal = [e.event for e in events if e.event.type in MODEL_TERMINALS]
    assert len(terminal) == 1 and terminal[0].error_type == "policy_denied"
    assert not any(e.event.type == "model_call_started" for e in events)
    assert not fold(events).messages


async def test_cancel_during_permission_closes_permission_and_model(tmp_path):
    entered = asyncio.Event()

    class WaitingResolver:
        name = "waiting"

        async def resolve(self, request):
            entered.set()
            await asyncio.Event().wait()

    hooks = HookBus()
    hooks.register_dispatch("ask", lambda _: Ask(reason="ask"))
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        task = asyncio.create_task(make_dispatcher(session, hooks, WaitingResolver()).dispatch_model(
            provider=FakeProvider([]), model=ModelId("fake"), messages=[], tools=(),
        ))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    events = read_session(tmp_path, SessionId("s"))
    terminal = [e.event for e in events if e.event.type in MODEL_TERMINALS]
    assert len(terminal) == 1 and terminal[0].type == "model_call_cancelled"
    permissions = [e.event for e in events if isinstance(e.event, PermissionResolved)]
    assert len(permissions) == 1 and not permissions[0].allowed


async def test_internal_inference_is_accounted_without_becoming_conversation(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        message, _ = await make_dispatcher(session).dispatch_model(
            provider=FakeProvider([text_turn("summary")]), model=ModelId("fake"),
            messages=[Message.user_text("summarize")], tools=(), purpose="compaction",
        )
        assert message.text() == "summary"
    events = read_session(tmp_path, SessionId("s"))
    state = fold(events)
    assert state.messages == []
    assert not state.open_model_intents
    completed = next(e.event for e in events if e.event.type == "model_call_completed")
    assert completed.purpose == "compaction"
    conn = open_store_memory()
    try:
        index_envelopes(conn, events)
        assert run_rollup(conn, "s")["model_calls"] == 1
    finally:
        conn.close()


def test_resume_repairs_both_kinds_once_in_causal_order(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        session.append(ModelCallProposed(call_id=CallId("z"), model=ModelId("fake")))
        session.append(ToolCallProposed(call_id=CallId("a"), tool=ToolName("write_file"), args={}))
    resumed, _ = resume_session(tmp_path, SessionId("s"))
    resumed.close()
    events = read_session(tmp_path, SessionId("s"))
    repairs = [e.event for e in events if e.event.type.endswith("_aborted")]
    assert [(r.type, r.call_id) for r in repairs] == [
        ("model_call_aborted", "z"), ("tool_call_aborted", "a"),
    ]
    resumed, _ = resume_session(tmp_path, SessionId("s"))
    resumed.close()
    assert sum(e.event.type.endswith("_aborted") for e in read_session(tmp_path, SessionId("s"))) == 2


async def test_failed_calls_are_visible_in_idempotent_telemetry(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch("deny", lambda _: Block(reason="no"))
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        with pytest.raises(ModelDispatchBlocked):
            await make_dispatcher(session, hooks).dispatch_model(
                provider=FakeProvider([]), model=ModelId("fake"), messages=[], tools=(),
            )
    events = read_session(tmp_path, SessionId("s"))
    conn = open_store_memory()
    try:
        index_envelopes(conn, events)
        index_envelopes(conn, events)
        assert conn.execute("SELECT model, status, error_type FROM model_calls").fetchall() == [
            ("fake", "failed", "policy_denied"),
        ]
        assert run_rollup(conn, "s")["model_failed"] == 1
    finally:
        conn.close()


def test_outdated_telemetry_schema_requires_explicit_rebuild(tmp_path):
    import sqlite3
    from harness.telemetry import open_store

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE model_calls (session_id TEXT)")
    with pytest.raises(RuntimeError, match="harness stats"):
        open_store(path)


@pytest.mark.parametrize("backend,class_name", [
    ("claude_code", "ClaudeCodeProvider"), ("codex", "CodexProvider"),
    ("antigravity", "AntigravityProvider"),
])
async def test_cancel_during_mcp_start_always_stops_server(monkeypatch, backend, class_name):
    entered = asyncio.Event()
    stopped = []

    class Server:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            stopped.append(True)

    module = importlib.import_module(f"harness.provider_{backend}")
    monkeypatch.setattr(module, "McpToolServer", Server)
    provider = getattr(module, class_name)()
    provider._dispatch = lambda _: None
    task = asyncio.create_task(collect(provider.complete(model=ModelId("fake"), messages=[])))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == [True]
