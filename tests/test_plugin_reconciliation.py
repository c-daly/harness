"""Integration tests for plugin workflow reconciliation.

Exercises the real reference plugins/memory server and a stub agent-swarm
workflow server (tests/fixtures/agent_swarm_stub.py) over the actual MCP
protocol, through the same Dispatcher and McpHost core uses in production.
No installed user plugin is touched; the stub is in-memory only and lives
under tests/fixtures.
"""

import importlib.util
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_client_server_memory_streams

from harness.dispatcher import Dispatcher
from harness.events import DispatchResolved, Envelope, ToolCallCompleted, ToolCallProposed
from harness.hooks import HookBus, ProposedToolCall, Rewrite
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.mcp_host import McpHost, McpTool
from harness.plugin_reconciliation import (
    PluginWorkflowRef,
    count_accepted_records,
    count_memory_contributions,
    main as plugins_main,
    project_plugin_workflows,
    reconcile,
    reconcile_from_log,
    render_reconciliation,
)
from harness.resume import resume_session
from harness.session import Session
from harness.tasks import TaskService
from harness.tools import ToolRegistry
from harness.types import CallId, SessionId, ToolName, new_call_id, new_session_id

_MEMORY_ROOT = Path(__file__).parent.parent / "plugins" / "memory"
_STUB_PATH = Path(__file__).parent / "fixtures" / "agent_swarm_stub.py"


def _load_fastmcp(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.mcp


@asynccontextmanager
async def _memory_transport(fastmcp):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        lowlevel = fastmcp._mcp_server
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: lowlevel.run(
                    server_read, server_write, lowlevel.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                yield (client_read, client_write)
            finally:
                tg.cancel_scope.cancel()


@dataclass
class _FakeLoop:
    dispatcher: Dispatcher


@dataclass
class _FakeKernel:
    session: Session
    mcp: McpHost | None
    loop: _FakeLoop


def _spec(name: str) -> McpServerSpec:
    return McpServerSpec(name=name, transport="stdio", command="unused")


def _register_connected_tools(registry: ToolRegistry, host: McpHost) -> None:
    for conn in host.connections.values():
        for tool in conn.tools:
            registry.register(McpTool(conn, tool))


async def _build_host(session, monkeypatch, tmp_path, *, include_memory=True, include_stub=True):
    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(tmp_path / "memory-store"))
    stub_fastmcp = _load_fastmcp("agent_swarm_stub_test", _STUB_PATH)
    memory_fastmcp = _load_fastmcp("memory_server_test", _MEMORY_ROOT / "server.py")

    def factory(spec):
        fastmcp = stub_fastmcp if spec.name == "stub" else memory_fastmcp
        return _memory_transport(fastmcp)

    specs = []
    if include_stub:
        specs.append(_spec("stub"))
    if include_memory:
        specs.append(_spec("memory"))
    registry = ToolRegistry()
    hooks = HookBus()
    host = McpHost(
        specs, registry=registry, hooks=hooks, session=session, transport_factory=factory
    )
    warnings = await host.start()
    assert warnings == []
    return host, registry, hooks


async def _dispatch(dispatcher, tool, args, *, purpose=None):
    call = ProposedToolCall(call_id=new_call_id(), tool=ToolName(tool), args=args)
    return await dispatcher.dispatch_tool(call, purpose=purpose)


def _append_workflow_call(session, call_id, tool, args, result, *, is_error=False):
    session.append(ToolCallProposed(call_id=CallId(call_id), tool=ToolName(tool), args=args))
    session.append(
        DispatchResolved(call_id=CallId(call_id), kind="tool", tool=ToolName(tool), args=args)
    )
    session.append(
        ToolCallCompleted(call_id=CallId(call_id), result_text=json.dumps(result), is_error=is_error)
    )


@pytest.mark.parametrize("is_error,result", [
    (True, {"workflow_id": "missing", "phase": "failed"}),
    (False, {"error": "workflow not found"}),
    (False, {"error": {"code": "not_found"}}),
])
def test_failed_workflow_calls_neither_create_refs_nor_replace_observations(tmp_path, is_error, result):
    with Session(tmp_path, SessionId("failed-workflows")) as session:
        session.start()
        _append_workflow_call(
            session, "started", "mcp__stub__workflow__workflow_start",
            {"workflow_id": "known"}, {"workflow_id": "known", "phase": "start"},
        )
        before = project_plugin_workflows(read_session(tmp_path, session.id))
        for workflow_id in ("known", "missing"):
            _append_workflow_call(
                session, workflow_id, "mcp__stub__workflow__workflow_get_state",
                {"workflow_id": workflow_id}, result, is_error=is_error,
            )
        assert project_plugin_workflows(read_session(tmp_path, session.id)) == before


@pytest.mark.parametrize("proposed,effective,purpose,reads,writes", [
    ("read", "workflow", "context", 0, 0),
    ("workflow", "read", "context", 1, 0),
    ("workflow", "read", "conversation", 0, 0),
    ("write", "workflow", "context", 0, 0),
    ("workflow", "write", "context", 0, 1),
    ("read", "write", "context", 0, 1),
    ("write", "read", "context", 1, 0),
])
async def test_memory_counters_follow_dispatched_rewrites(
    tmp_path, monkeypatch, proposed, effective, purpose, reads, writes,
):
    calls = {
        "read": ("mcp__memory__memory_list", {}),
        "write": ("mcp__memory__memory_write", {
            "entry_type": "project", "name": "note", "subject": "wf1",
            "description": "progress", "body": "started",
        }),
        "workflow": ("mcp__stub__workflow__workflow_get_state", {"workflow_id": "wf1"}),
    }
    with Session(tmp_path, SessionId("rewritten-memory")) as session:
        session.start()
        host, registry, hooks = await _build_host(session, monkeypatch, tmp_path)
        dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=HeadlessResolver(),
        )
        try:
            await _dispatch(dispatcher, "mcp__stub__workflow__workflow_start", {"workflow_id": "wf1"})
            tool, args = calls[effective]
            hooks.register_dispatch("redirect", lambda call: Rewrite(action=ProposedToolCall(
                call_id=call.call_id, tool=ToolName(tool), args=args,
            )))
            result = await _dispatch(dispatcher, *calls[proposed], purpose=purpose)
            assert not result.is_error and not result.read_text().startswith("error:")
            envelopes = read_session(tmp_path, session.id)
            assert count_memory_contributions(envelopes) == reads
            assert count_accepted_records(envelopes) == writes
        finally:
            await host.stop()


@pytest.mark.parametrize("tool,args", [
    ("mcp__stub__workflow__workflow_get_state", {"workflow_id": "wf2"}),
    ("mcp__stub__workflow__workflow_start", {"workflow_id": "wf3"}),
    ("mcp__memory__memory_list", {}),
])
async def test_live_reconciliation_does_not_attribute_redirected_results_to_original_ref(
    tmp_path, monkeypatch, tool, args,
):
    with Session(tmp_path, SessionId("redirected-check")) as session:
        session.start()
        host, registry, hooks = await _build_host(session, monkeypatch, tmp_path)
        dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=HeadlessResolver(),
        )
        try:
            await _dispatch(dispatcher, "mcp__stub__workflow__workflow_start", {"workflow_id": "wf1"})
            # Another operator created wf2; it is not yet a ref in this session.
            await host.connections["stub"].call_tool("workflow__workflow_start", {"workflow_id": "wf2"})
            hooks.register_dispatch("redirect", lambda call: Rewrite(action=ProposedToolCall(
                call_id=call.call_id, tool=ToolName(tool), args=args,
            )))
            report = await reconcile(_FakeKernel(session, host, _FakeLoop(dispatcher)))
            assert report.statuses["stub:wf1"] == "unknown"
            assert report.non_resumable == ()
            assert report.live_checks == ("stub:wf1",)
            assert "live check inconclusive" in render_reconciliation(report)
            if tool == "mcp__memory__memory_list":
                assert report.memory_contributions == 1
            else:
                assert report.statuses[f"stub:{args['workflow_id']}"] == "unknown"
                assert len(report.refs) == 2
        finally:
            await host.stop()


@pytest.mark.parametrize("status,verb,phase", [
    ("active", "workflow_advance_phase", "implementing"),
    ("finished", "workflow_stop", "stopped"),
])
async def test_live_report_includes_new_phase_and_log_only_status_stays_unknown(
    tmp_path, monkeypatch, status, verb, phase,
):
    with Session(tmp_path, SessionId("fresh-phase")) as session:
        session.start()
        host, registry, hooks = await _build_host(session, monkeypatch, tmp_path)
        dispatcher = Dispatcher(
            session=session, registry=registry, hooks=hooks, resolver=HeadlessResolver(),
        )
        try:
            await _dispatch(dispatcher, "mcp__stub__workflow__workflow_start", {"workflow_id": "wf1"})
            args = {"workflow_id": "wf1"}
            if verb == "workflow_advance_phase":
                args["phase"] = phase
            await host.connections["stub"].call_tool(f"workflow__{verb}", args)
            report = await reconcile(_FakeKernel(session, host, _FakeLoop(dispatcher)))
            assert report.statuses == {"stub:wf1": status}
            assert report.refs[0].last_phase == phase
            rendered = render_reconciliation(report)
            assert f"phase={phase}; status={status}; live check performed" in rendered
            assert "no live check performed" not in rendered
            replay = reconcile_from_log(read_session(tmp_path, session.id))
            assert replay.refs == report.refs
            assert replay.statuses == {"stub:wf1": "unknown"}
            assert replay.live_checks == ()
            assert "no live check performed" in render_reconciliation(replay)
        finally:
            await host.stop()


def test_project_plugin_workflows_folds_the_latest_completed_call_by_seq():
    env1 = Envelope(
        session_id=SessionId("s1"), seq=1, ts=0.0,
        event=ToolCallProposed(
            call_id=CallId("c1"), tool=ToolName("mcp__stub__workflow__workflow_start"),
            args={"workflow_id": "wf1", "phase": "start"},
        ),
    )
    env2 = Envelope(
        session_id=SessionId("s1"), seq=2, ts=0.0,
        event=ToolCallCompleted(
            call_id=CallId("c1"),
            result_text=json.dumps({"workflow_id": "wf1", "phase": "start", "status": "active"}),
            is_error=False,
        ),
    )
    env3 = Envelope(
        session_id=SessionId("s1"), seq=3, ts=0.0,
        event=ToolCallProposed(
            call_id=CallId("c2"), tool=ToolName("mcp__stub__workflow__workflow_advance_phase"),
            args={"workflow_id": "wf1", "phase": "implementing"},
        ),
    )
    env4 = Envelope(
        session_id=SessionId("s1"), seq=4, ts=0.0,
        event=ToolCallCompleted(
            call_id=CallId("c2"),
            result_text=json.dumps(
                {"workflow_id": "wf1", "phase": "implementing", "status": "active"}
            ),
            is_error=False,
        ),
    )
    refs = project_plugin_workflows([env1, env2, env3, env4])
    assert len(refs) == 1
    ref = refs[0]
    assert (ref.server, ref.workflow_id, ref.last_phase) == ("stub", "wf1", "implementing")
    assert ref.observed_seq == 4
    assert ref.observed_call_id == "c2"


def test_project_plugin_workflows_unknown_shape_yields_last_phase_none_without_raising():
    env1 = Envelope(
        session_id=SessionId("s1"), seq=1, ts=0.0,
        event=ToolCallProposed(
            call_id=CallId("c1"), tool=ToolName("mcp__stub__workflow__workflow_get_state"),
            args={"workflow_id": "wf2"},
        ),
    )
    env2 = Envelope(
        session_id=SessionId("s1"), seq=2, ts=0.0,
        event=ToolCallCompleted(call_id=CallId("c1"), result_text="not json at all", is_error=False),
    )
    refs = project_plugin_workflows([env1, env2])
    assert refs == (
        PluginWorkflowRef(
            server="stub", workflow_id="wf2", last_phase=None, observed_seq=2, observed_call_id="c1"
        ),
    )


def test_reconcile_from_log_classifies_every_ref_unknown_and_never_dispatches():
    env1 = Envelope(
        session_id=SessionId("s1"), seq=1, ts=0.0,
        event=ToolCallProposed(
            call_id=CallId("c1"), tool=ToolName("mcp__stub__workflow__workflow_start"),
            args={"workflow_id": "wf1", "phase": "start"},
        ),
    )
    env2 = Envelope(
        session_id=SessionId("s1"), seq=2, ts=0.0,
        event=ToolCallCompleted(
            call_id=CallId("c1"),
            result_text=json.dumps({"workflow_id": "wf1", "phase": "start", "status": "active"}),
            is_error=False,
        ),
    )
    report = reconcile_from_log([env1, env2])
    assert len(report.refs) == 1
    assert report.statuses == {"stub:wf1": "unknown"}
    assert report.non_resumable == ()
    assert report.memory_contributions == 0
    assert report.accepted_records == 0


def test_count_memory_contributions_and_accepted_records_from_log():
    envelopes = []
    seq = 0

    def add(event):
        nonlocal seq
        seq += 1
        envelopes.append(Envelope(session_id=SessionId("s1"), seq=seq, ts=0.0, event=event))

    add(ToolCallProposed(
        call_id=CallId("read-context"), tool=ToolName("mcp__memory__memory_list"),
        args={}, purpose="context",
    ))
    add(ToolCallCompleted(call_id=CallId("read-context"), result_text="(no entries)", is_error=False))

    add(ToolCallProposed(
        call_id=CallId("read-conversation"), tool=ToolName("mcp__memory__memory_list"),
        args={}, purpose="conversation",
    ))
    add(ToolCallCompleted(
        call_id=CallId("read-conversation"), result_text="(no entries)", is_error=False
    ))

    add(ToolCallProposed(
        call_id=CallId("write-ok"), tool=ToolName("mcp__memory__memory_write"),
        args={"entry_type": "project", "name": "n1", "subject": "s1",
              "description": "d", "body": "b"},
    ))
    add(ToolCallCompleted(
        call_id=CallId("write-ok"), result_text="s1/2026-01-01-n1.md", is_error=False
    ))

    add(ToolCallProposed(
        call_id=CallId("write-rejected"), tool=ToolName("mcp__memory__memory_write"),
        args={"entry_type": "project", "name": "n1", "subject": "s1",
              "description": "d", "body": "b"},
    ))
    add(ToolCallCompleted(
        call_id=CallId("write-rejected"), result_text="error: n1 already exists", is_error=False
    ))

    assert count_memory_contributions(envelopes) == 1
    assert count_accepted_records(envelopes) == 1


def test_reconcile_from_log_on_session_with_no_mcp_calls_is_empty(tmp_path):
    base_dir = tmp_path / "base"
    session_id = new_session_id()
    session = Session(base_dir, session_id)
    session.start()
    session.close()
    envelopes = read_session(base_dir, session_id, repair=False)
    report = reconcile_from_log(envelopes)
    assert report.refs == ()
    assert report.statuses == {}
    assert report.non_resumable == ()
    assert report.memory_contributions == 0
    assert report.accepted_records == 0


async def test_reconcile_reports_active_after_restart_with_memory_contribution_and_accepted_record(
    tmp_path, monkeypatch
):
    base_dir = tmp_path / "base"
    session_id = new_session_id()
    session1 = Session(base_dir, session_id)
    session1.start()
    host, registry1, hooks1 = await _build_host(session1, monkeypatch, tmp_path)
    dispatcher1 = Dispatcher(
        session=session1, registry=registry1, hooks=hooks1, resolver=HeadlessResolver()
    )
    try:
        start = await _dispatch(
            dispatcher1, "mcp__stub__workflow__workflow_start",
            {"workflow_id": "wf1", "phase": "start"}, purpose="agent-task",
        )
        assert not start.is_error
        context = await _dispatch(dispatcher1, "mcp__memory__memory_list", {}, purpose="context")
        assert not context.is_error
        write = await _dispatch(
            dispatcher1, "mcp__memory__memory_write",
            {"entry_type": "project", "name": "wf1-note", "subject": "wf1",
             "description": "progress note", "body": "started"},
            purpose="agent-task",
        )
        assert not write.is_error and not write.read_text().startswith("error:")
    finally:
        session1.close()

    session2, _transcript = resume_session(base_dir, session_id)
    registry2 = ToolRegistry()
    _register_connected_tools(registry2, host)
    dispatcher2 = Dispatcher(
        session=session2, registry=registry2, hooks=HookBus(), resolver=HeadlessResolver()
    )
    kernel = _FakeKernel(session=session2, mcp=host, loop=_FakeLoop(dispatcher=dispatcher2))

    try:
        report = await reconcile(kernel)
    finally:
        session2.close()
        await host.stop()

    assert len(report.refs) == 1
    ref = report.refs[0]
    assert (ref.server, ref.workflow_id) == ("stub", "wf1")
    assert report.statuses[f"{ref.server}:{ref.workflow_id}"] == "active"
    assert report.non_resumable == ()
    assert report.memory_contributions == 1
    assert report.accepted_records == 1


@pytest.mark.parametrize("restart_plugin", [False, True], ids=["removed", "restarted-empty"])
async def test_reconcile_marks_lost_plugin_state_unavailable_and_non_resumable_after_restart(
    tmp_path, monkeypatch, restart_plugin,
):
    base_dir = tmp_path / "base"
    session_id = new_session_id()
    session1 = Session(base_dir, session_id)
    session1.start()
    tasks = TaskService(session1)
    tasks.create("ship the workflow reconciliation contract")
    tasks.add_requirement({"id": "req-1", "description": "reconcile after restart"})
    host, registry1, hooks1 = await _build_host(session1, monkeypatch, tmp_path)
    dispatcher1 = Dispatcher(
        session=session1, registry=registry1, hooks=hooks1, resolver=HeadlessResolver()
    )
    try:
        start = await _dispatch(
            dispatcher1, "mcp__stub__workflow__workflow_start",
            {"workflow_id": "wf1", "phase": "start"}, purpose="agent-task",
        )
        assert not start.is_error
    finally:
        session1.close()

    await host.stop()

    session2, _transcript = resume_session(base_dir, session_id)
    # A new module instance has fresh in-memory state. Its tools remain reachable
    # in the restarted case, so get_state returns a domain error, not unknown tool.
    host, registry2, hooks2 = await _build_host(
        session2, monkeypatch, tmp_path, include_stub=restart_plugin,
    )
    dispatcher2 = Dispatcher(
        session=session2, registry=registry2, hooks=hooks2, resolver=HeadlessResolver()
    )
    kernel = _FakeKernel(session=session2, mcp=host, loop=_FakeLoop(dispatcher=dispatcher2))

    try:
        report = await reconcile(kernel)
        tracked = TaskService(session2).selected()
        completions = [env.event for env in read_session(base_dir, session_id)
                       if isinstance(env.event, ToolCallCompleted)]
        if restart_plugin:
            assert not completions[-1].is_error
            assert "not found" in json.loads(completions[-1].result_text)["error"]
        else:
            assert completions[-1].is_error
    finally:
        session2.close()
        await host.stop()

    key = "stub:wf1"
    assert len(report.refs) == 1
    assert report.refs[0].last_phase == "start"
    assert report.statuses[key] == "unavailable"
    assert report.non_resumable == (key,)
    assert tracked is not None
    assert list(tracked.requirements) == ["req-1"]


async def test_reconcile_with_memory_absent_has_zero_contributions_and_no_error(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    session_id = new_session_id()
    session = Session(base_dir, session_id)
    session.start()
    host, registry, hooks = await _build_host(
        session, monkeypatch, tmp_path, include_memory=False, include_stub=True
    )
    dispatcher = Dispatcher(session=session, registry=registry, hooks=hooks, resolver=HeadlessResolver())
    try:
        start = await _dispatch(
            dispatcher, "mcp__stub__workflow__workflow_start",
            {"workflow_id": "wf1", "phase": "start"}, purpose="agent-task",
        )
        assert not start.is_error
        kernel = _FakeKernel(session=session, mcp=host, loop=_FakeLoop(dispatcher=dispatcher))
        report = await reconcile(kernel)
    finally:
        session.close()
        await host.stop()

    assert len(report.refs) == 1
    assert report.statuses["stub:wf1"] == "active"
    assert report.memory_contributions == 0
    assert report.accepted_records == 0


async def test_reconcile_with_agent_swarm_absent_has_no_refs_and_no_error(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    session_id = new_session_id()
    session = Session(base_dir, session_id)
    session.start()
    host, registry, hooks = await _build_host(
        session, monkeypatch, tmp_path, include_memory=True, include_stub=False
    )
    dispatcher = Dispatcher(session=session, registry=registry, hooks=hooks, resolver=HeadlessResolver())
    try:
        context = await _dispatch(dispatcher, "mcp__memory__memory_list", {}, purpose="context")
        assert not context.is_error
        kernel = _FakeKernel(session=session, mcp=host, loop=_FakeLoop(dispatcher=dispatcher))
        report = await reconcile(kernel)
    finally:
        session.close()
        await host.stop()

    assert report.refs == ()
    assert report.statuses == {}
    assert report.non_resumable == ()
    assert report.memory_contributions == 1


def test_plugins_reconcile_cli_prints_report_without_starting_any_server(tmp_path, capsys):
    base_dir = tmp_path / "base"
    session_id = new_session_id()
    session = Session(base_dir, session_id)
    session.start()
    _append_workflow_call(
        session, "c1", "mcp__stub__workflow__workflow_start",
        {"workflow_id": "wf1", "phase": "start"},
        {"workflow_id": "wf1", "phase": "start", "status": "active"},
    )
    session.close()

    plugins_main(["reconcile", str(session_id), "--base-dir", str(base_dir)])

    out = capsys.readouterr().out
    assert "Plugin workflow refs: 1" in out
    assert "stub/wf1" in out
    assert "status=unknown" in out


def test_harness_status_joins_plugin_reconciliation_summary_line(tmp_path, capsys):
    from harness.status_cli import main as status_main

    base_dir = tmp_path / "base"
    session_id = new_session_id()
    session = Session(base_dir, session_id)
    session.start()
    _append_workflow_call(
        session, "c1", "mcp__stub__workflow__workflow_start",
        {"workflow_id": "wf1", "phase": "start"},
        {"workflow_id": "wf1", "phase": "start", "status": "active"},
    )
    session.close()

    status_main([str(session_id), "--base-dir", str(base_dir)])

    out = capsys.readouterr().out
    assert "plugins: 1 workflow refs (0 unavailable), 0 memory contributions, 0 accepted records" in out
