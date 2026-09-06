"""Typed external execution retains authority, transcript fidelity, and cleanup."""

import asyncio

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.agent_runtime import bind_agent_runtime
from harness.cli import build_kernel
from harness.errors import ProviderError
from harness.fold import fold
from harness.hooks import Allow, Block, HookBus, ProposedModelCall, Rewrite
from harness.log import read_session
from harness.messages import Message
from harness.provider import StreamStop, TextDelta, ThinkingDelta, Usage, UsageReport
from harness.provider_codex import CodexProvider
from harness.session import Session
from harness.types import ModelId, SessionId
from tests.test_agent_stream_limits import ExternalAgent, route
from tests.test_inference import make_dispatcher


class ScriptedCodex(ExternalAgent, CodexProvider):

    def complete(self, **kwargs):
        self.received = kwargs
        return super().complete(**kwargs)


@pytest.mark.parametrize("kind", ["direct", "codex"])
async def test_interface_uses_typed_runtime_and_replays_one_response(tmp_path, monkeypatch, kind):
    backend = ScriptedCodex([ThinkingDelta("reasoning", signature="signature"), TextDelta("done"),
                             UsageReport(Usage(output_tokens=3)), StreamStop("end_turn")])
    provider = route(backend, kind, monkeypatch)
    kernel = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("external"))
    try:
        await kernel.loop.start()
        progress = []
        result = await kernel.loop.run_task(AgentTask(
            prompt="work", context=(Message.system_text("ephemeral context"),),
            acceptance_criteria=("held-out checks pass",)), on_progress=progress.append)
        assert result.status == "completed" and result.acceptance == "unverified"
        assert result.read_text(kernel.session.blobs) == "done"
        events = read_session(tmp_path, kernel.session.id)
        starts = [e.event for e in events if e.event.type == "agent_run_started"]
        parent, child = starts
        assert parent.runtime == "harness" and child.runtime == "codex"
        assert child.parent_run_id == parent.run_id
        assert child.capabilities["qualification"] == "unverified"
        assert child.capabilities["resume"] is False
        assert child.capabilities["internal_iteration_limit"] is False
        state = fold(events)
        assert kernel.loop.history == state.messages
        assert [m.text() for m in state.messages] == ["work", "done"]
        assert state.agent_runs[child.run_id].response == state.messages[-1]
        assert state.agent_runs[child.run_id].remaining_criteria == ("held-out checks pass",)
        assert state.messages[-1].blocks[0].provider_extras["signature"] == "signature"
        assert "ephemeral context" not in str([e.event.model_dump() for e in events])
        sent = [m.text() for m in backend.received["messages"]]
        assert sent.count("work") == 1 and "ephemeral context" in sent
        assert any("held-out checks pass" in text for text in sent)
        calls = [e.event for e in events if e.event.type == "model_call_completed"]
        assert len(calls) == 1 and calls[0].purpose == "agent-task"
        assert calls[0].agent_run_id == child.run_id
        assert result.usage.output_tokens == 3
        assert result.usage.input_tokens is None
        from harness.sessions import list_sessions
        assert list_sessions(tmp_path)[0].last_model == "external"
        from harness.telemetry import index_envelopes, open_store_memory, run_rollup
        conn = open_store_memory()
        try:
            index_envelopes(conn, events)
            stats = run_rollup(conn, str(kernel.session.id))
            assert stats["model_calls"] == 1 and stats["output_tokens"] == 3
            assert stats["input_tokens"] is None and stats["cost"] is None
        finally:
            conn.close()
        assert any(p.phase == "execution" and p.run_id == child.run_id for p in progress)
        assert not state.open_agent_runs and not state.open_model_intents
    finally:
        kernel.session.close()


@pytest.mark.parametrize("policy", ["deny", "route-to-inference"])
async def test_bound_runtime_cannot_bypass_policy_or_change_kind(tmp_path, monkeypatch, policy):
    backend = ScriptedCodex([TextDelta("must not run"), StreamStop("end_turn")])
    provider = route(backend, "codex", monkeypatch)
    hooks = HookBus()
    hooks.register_dispatch("policy", lambda action: Block(reason="blocked") if policy == "deny"
                            else Rewrite(action=ProposedModelCall(call_id=action.call_id,
                                                                  model=ModelId("raw"))))
    with Session(tmp_path, SessionId("blocked")) as session:
        session.start()
        runtime = bind_agent_runtime(provider, ModelId("external"), make_dispatcher(session, hooks))
        with pytest.raises(Exception, match="blocked|runtime"):
            await runtime.run_task(AgentTask(prompt="work"))
        assert backend.calls == 0
        state = fold(read_session(tmp_path, session.id))
        result, = state.agent_runs.values()
        assert result.status == "failed"
        assert not state.open_agent_runs and not state.open_model_intents


@pytest.mark.parametrize("mode", ["bytes", "cancel", "deadline"])
async def test_external_run_closes_before_terminal(tmp_path, monkeypatch, mode):
    backend = ScriptedCodex([TextDelta("x" * 2000)], hang=mode != "bytes")
    with Session(tmp_path, SessionId("bounded")) as session:
        session.start()
        runtime = bind_agent_runtime(backend, ModelId("codex/default"), make_dispatcher(session))
        append = session.append

        def checked_append(event):
            if event.type == "agent_run_finished":
                assert backend.closed
            return append(event)

        monkeypatch.setattr(session, "append", checked_append)
        work = asyncio.create_task(runtime.run_task(AgentTask(prompt="work", limits=TaskLimits(
            timeout_seconds=0.05 if mode == "deadline" else 10, max_response_bytes=1000))))
        await backend.entered.wait()
        if mode == "cancel":
            work.cancel()
        expected = {"bytes": ProviderError, "cancel": asyncio.CancelledError, "deadline": TimeoutError}
        with pytest.raises(expected[mode]):
            await work
        assert backend.calls == 1
        state = fold(read_session(tmp_path, session.id))
        assert not state.open_agent_runs and not state.open_model_intents
        assert len(state.agent_runs) == 1


async def test_unexecuted_agent_tool_proposals_fail_without_a_second_loop(tmp_path):
    from harness.provider import tool_call_turn
    from harness.types import ToolName

    backend = ScriptedCodex(tool_call_turn("", ToolName("write_file"), {}))
    kernel = build_kernel(base_dir=tmp_path, provider=backend, model=ModelId("external"))
    try:
        await kernel.loop.start()
        with pytest.raises(ProviderError, match="unexecuted tool proposals"):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        events = read_session(tmp_path, kernel.session.id)
        assert not any(e.event.type in {"tool_call_proposed", "model_call_completed"} for e in events)
        assert backend.calls == 1 and backend.closed
        assert kernel.loop.history == fold(events).messages
    finally:
        kernel.session.close()


async def test_zero_iterations_does_not_launch_external_agent(tmp_path):
    backend = ScriptedCodex([TextDelta("unused"), StreamStop("end_turn")])
    with Session(tmp_path, SessionId("zero")) as session:
        session.start()
        runtime = bind_agent_runtime(backend, ModelId("codex/default"), make_dispatcher(session))
        result = await runtime.run_task(AgentTask(prompt="work", limits=TaskLimits(max_iterations=0)))
        assert result.status == "incomplete" and result.reason == "iteration_limit"
        assert backend.calls == 0 and result.usage.output_tokens == 0


@pytest.mark.parametrize("blocked, cancel_during_tool", [(False, False), (True, False), (False, True)])
async def test_codex_process_calls_scoped_mcp_tools_and_preserves_transcript(
    tmp_path, monkeypatch, blocked, cancel_during_tool,
):
    import json
    from pathlib import Path

    from harness.hooks import ProposedToolCall
    from harness.tools import ToolSpec
    from harness.types import ToolName
    from tests.test_provider_codex import _PREAMBLE, _fake_codex

    # This is a real child process and real MCP HTTP request; no subscription use.
    script = _PREAMBLE + '''
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
url_arg = next(a for a in sys.argv if a.startswith("mcp_servers.harness.url="))
url = json.loads(url_arg.split("=", 1)[1])
async def run():
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("echo", {"text": "from child"})
            text = "denied" if result.isError else result.content[0].text
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}})
asyncio.run(run())
'''
    binary = _fake_codex(tmp_path, script)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    backend = CodexProvider(binary=binary)
    kernel = build_kernel(base_dir=tmp_path / "sessions", provider=backend, model=ModelId("codex/default"))
    invoked = []
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class Echo:
        spec = ToolSpec(name=ToolName("echo"), description="Echo", parameters={"type": "object"})

        async def __call__(self, args):
            try:
                invoked.append(args)
                entered.set()
                if cancel_during_tool:
                    await asyncio.Event().wait()
                return "echoed: " + args["text"]
            finally:
                cleaned.set()

    kernel.registry.register(Echo())
    kernel.loop.hooks.register_dispatch("test-policy", lambda action:
        Block(reason="blocked") if blocked and isinstance(action, ProposedToolCall) else Allow())
    try:
        await kernel.loop.start()
        append = kernel.session.append

        def checked_append(event):
            if cancel_during_tool and event.type == "agent_run_finished":
                assert cleaned.is_set(), "the tool must settle before either task's terminal fact"
            return append(event)

        monkeypatch.setattr(kernel.session, "append", checked_append)
        work = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="use echo")))
        if cancel_during_tool:
            try:
                await asyncio.wait_for(entered.wait(), 3)
            finally:
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(work, 2)
        else:
            result = await work
            assert result.read_text(kernel.session.blobs) == ("denied" if blocked else "echoed: from child")
        assert len(invoked) == (0 if blocked else 1)
        events = read_session(tmp_path / "sessions", kernel.session.id)
        child = next(e.event for e in events if e.event.type == "agent_run_started" and e.event.runtime == "codex")
        tool = next(e.event for e in events if e.event.type == "tool_call_proposed")
        assert tool.purpose == "agent-task" and tool.agent_run_id == child.run_id
        assert tool.task_id == child.task_id
        terminal_type = "tool_call_cancelled" if cancel_during_tool else "tool_call_completed"
        assert any(e.event.type == terminal_type for e in events)
        assert kernel.loop.history == fold(events).messages
        assert len(kernel.loop.history) == (1 if cancel_during_tool else 2)
        assert not fold(events).open_agent_runs and not fold(events).open_intents
        assert not Path(json.loads(Path(binary + ".cwd").read_text())["path"]).exists()
        assert not Path(json.loads(Path(binary + ".codexhome").read_text())["value"]).exists()
    finally:
        kernel.session.close()


async def test_cancel_reaps_codex_process_before_task_terminal(tmp_path, monkeypatch):
    import os

    from tests.test_provider_codex import SLEEPER, _fake_codex, _read_pid

    binary = _fake_codex(tmp_path, SLEEPER)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    with Session(tmp_path / "sessions", SessionId("cancelled")) as session:
        session.start()
        runtime = bind_agent_runtime(CodexProvider(binary=binary), ModelId("codex/default"),
                                     make_dispatcher(session))
        work = asyncio.create_task(runtime.run_task(AgentTask(prompt="wait")))
        try:
            pid = await _read_pid(binary + ".pid")
            append = session.append

            def checked_append(event):
                if event.type == "agent_run_finished":
                    with pytest.raises(ProcessLookupError):
                        os.kill(pid, 0)
                return append(event)

            monkeypatch.setattr(session, "append", checked_append)
        finally:
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        state = fold(read_session(tmp_path / "sessions", session.id))
        result, = state.agent_runs.values()
        assert result.status == "cancelled" and not state.open_agent_runs


def test_resume_aborts_external_run_and_tools_without_fabricating_conversation(tmp_path):
    from harness.events import AgentRunStarted, ToolCallProposed, UserMessage
    from harness.fold import resume_repairs
    from harness.types import CallId, ToolName

    with Session(tmp_path, SessionId("resume")) as session:
        session.start()
        session.append(UserMessage(text="work"))
        session.append(AgentRunStarted(task_id="task", run_id="run", runtime="codex",
                                       acceptance_criteria=("external check",)))
        session.append(ToolCallProposed(call_id=CallId("tool"), tool=ToolName("write_file"),
                                        args={}, purpose="agent-task", agent_run_id="run"))
        for repair in resume_repairs(fold(read_session(tmp_path, session.id))):
            session.append(repair)
        state = fold(read_session(tmp_path, session.id))
        assert [m.text() for m in state.messages] == ["work"]
        assert not state.open_agent_runs and not state.open_intents
        assert state.agent_runs["run"].status == "aborted"
        assert state.agent_runs["run"].remaining_criteria == ("external check",)
