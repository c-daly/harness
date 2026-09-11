"""Reconciled continuations preserve evidence without replaying external assignments."""

import asyncio
import hashlib
import json

import pytest

from harness.agent import TaskLimits
from harness.agent_runtime import AgentRuntimeInfo
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.dispatcher import current_dispatch_tool
from harness.errors import NetworkFailed
from harness.execution import ExecutionLimits
from harness.fold import fold
from harness.handoff import ExactCall, HandoffSpec, Resolution, snapshot
from harness.hooks import ProposedToolCall
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import text_turn, tool_call_turn
from harness.types import CallId, ModelId, ToolName


class MixedProvider:
    def __init__(self, root):
        self.root, self.requests = root, []
        self.catalog = Catalog({"external": {"route": "codex/default", "backend": "codex"},
                                "local": {"route": "openai/local", "tags": ["tools"]}})
        self.native_calls = 0
        self.entered = asyncio.Event()
        self.hang = False
        self.steps = ["old", "new", "new", "done"]

    def execution_kind(self, model):
        return "agent" if model == "external" else "inference"

    def agent_runtime_info(self, model):
        return AgentRuntimeInfo(runtime="codex") if model == "external" else None

    async def complete(self, **kwargs):
        self.native_calls += 1
        outcome = await current_dispatch_tool.get()(ProposedToolCall(call_id=CallId("source-write"),
            tool=ToolName("write_file"), args={"file_path": str(self.root / "A.txt"), "content": "stage A\n"}))
        assert not outcome.is_error
        # This unobserved write stands in for a provider-native side effect.
        (self.root / "native.txt").write_text("external native effect")
        raise NetworkFailed("connection lost after side effects")
        yield

    async def infer(self, request):
        self.requests.append(request)
        if self.hang:
            self.entered.set()
            await asyncio.Event().wait()
        step = self.steps.pop(0) if self.steps else "done"
        if step == "fail":
            from harness.errors import AuthFailed
            raise AuthFailed("expired after continuation side effect")
        letter = {"old": "A", "new": "B", "third": "C"}.get(step)
        chunks = text_turn("done") if step == "done" else tool_call_turn("", ToolName("write_file"),
            {"file_path": str(self.root / f"{letter}.txt"), "content": f"stage {letter}\n"})
        for chunk in chunks:
            yield chunk


def permissions(*rules):
    return PermissionEngine([RuleSet(rules=[*rules, PermissionRule("allow", "*")], default="allow")])


async def source(tmp_path, *, provider=None, engine=None, limits=None):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    provider = provider or MixedProvider(root)
    kernel = build_kernel(base_dir=tmp_path / "sessions", provider=provider, model=ModelId("external"),
        native_tools=True, workspace_root=root, permissions=engine or permissions(), execution_limits=limits)
    await kernel.loop.start()
    kernel.tasks.create("Complete both stages")
    for letter in ("A", "B"):
        args = {"file_path": str(root / f"{letter}.txt"), "content": f"stage {letter}\n"}
        result = f"Created {root / f'{letter}.txt'} (1 lines)."
        kernel.tasks.add_requirement({"id": letter, "description": f"Stage {letter} recorded write",
            "check": {"kind": "tool_result", "tool": "write_file", "args": args,
                      "sha256": hashlib.sha256(result.encode()).hexdigest()}})
    kernel.tasks.add_requirement({"id": "review", "description": "User inspects the complete result"})
    task = kernel.tasks.prepare("ORIGINAL ASSIGNMENT SENTINEL: perform both stages and do not stop")
    task = task.model_copy(update={"limits": TaskLimits(max_iterations=8, timeout_seconds=10)})
    with pytest.raises(NetworkFailed):
        await kernel.loop.run_task(task)
    assert (root / "A.txt").read_text() == "stage A\n" and not (root / "B.txt").exists()
    assert kernel.tasks.selected().external_execution
    kernel.loop.model = ModelId("local")
    return kernel, provider


def specification(kernel, **updates):
    checkpoint = snapshot(kernel.session)
    root = kernel.provider.root
    spec = HandoffSpec(snapshot_sha256=checkpoint.digest, model=ModelId("local"),
        continuation="Only finish stage B and summarize the result. Stage A is already done.", process_stopped=True,
        resolutions=tuple(Resolution(effect_id=e.id, status="completed", note="Inspected native.txt and stopped process.")
            for e in checkpoint.effects if e.state == "uncertain"),
        allowed_calls=(ExactCall(tool="write_file", args={"file_path": str(root / "B.txt"), "content": "stage B\n"}),),
        required_tags=("tools",))
    return HandoffSpec.model_validate({**spec.model_dump(), **updates})


@pytest.mark.parametrize("resume", [False, True])
async def test_handoff_keeps_identity_and_evidence_blocks_duplicate_calls_and_excludes_old_assignment(tmp_path, resume):
    kernel, provider = await source(tmp_path)
    record = kernel.handoffs.record(specification(kernel))
    task_id, session_id = kernel.tasks.selected().definition.id, kernel.session.id
    if resume:
        kernel.session.close()
        kernel = build_kernel(base_dir=tmp_path / "sessions", provider=provider, model=ModelId("local"),
            resume_session_id=session_id, native_tools=True, workspace_root=provider.root, permissions=permissions())
    try:
        counts = kernel.loop.dispatcher.scope.budget.model_calls
        with pytest.raises(ValueError, match="reconcile external effects"):
            await kernel.loop.run_task(kernel.tasks.prepare("retry everything"))
        result = await kernel.handoffs.run(record.id)
        assert result.status == "completed" and result.task_id == task_id
        assert provider.native_calls == 1 and len(provider.requests) == 4
        assert kernel.loop.dispatcher.scope.budget.model_calls == counts + 4
        for request in provider.requests:
            assert "ORIGINAL ASSIGNMENT SENTINEL" not in str(request.messages)
        assert (provider.root / "A.txt").read_text() == "stage A\n"
        assert (provider.root / "B.txt").read_text() == "stage B\n"
        events = read_session(kernel.session.base, session_id)
        completed = [e.event for e in events if e.event.type == "tool_call_completed" and not e.event.is_error]
        assert len(completed) == 2  # One A write before the failure, one B write after reconciliation.
        assert len([e for e in events if e.event.type == "tool_call_completed" and e.event.is_error]) == 2
        checked = kernel.tasks.check()
        assert checked.evidence["A"].status == "passed" and checked.evidence["B"].status == "passed"
        assert checked.evidence["A"].source_seq < checked.run_started_seq < checked.evidence["B"].source_seq
        assert checked.unresolved == ("review",) and not checked.accepted
        kernel.tasks.confirm("review", "Inspected both files and native effects")
        kernel.tasks.accept("Accepted after checking both stages")
        assert kernel.tasks.selected().accepted
        with pytest.raises(ValueError, match="already attempted"):
            await kernel.handoffs.run(record.id)
        assert not fold(events).open_intents and not fold(events).open_agent_runs
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["unknown", "stale", "duplicate-completed", "changed-destination", "pin", "tags"])
async def test_handoff_refuses_unreconciled_changed_or_ineligible_work(tmp_path, mode):
    kernel, provider = await source(tmp_path)
    try:
        spec = specification(kernel)
        if mode == "unknown":
            spec = spec.model_copy(update={"resolutions": tuple(r.model_copy(update={"status": "uncertain"})
                                                                 for r in spec.resolutions)})
        if mode == "duplicate-completed":
            spec = spec.model_copy(update={"allowed_calls": (ExactCall(tool="write_file", args={
                "file_path": str(provider.root / "A.txt"), "content": "stage A\n"}),)})
            with pytest.raises(ValueError, match="completed action"):
                kernel.handoffs.record(spec)
            return
        record = kernel.handoffs.record(spec)
        if mode == "stale":
            kernel.tasks.add_requirement({"id": "new", "description": "New requirement"})
        elif mode == "changed-destination":
            provider.catalog.entries["local"]["api_base"] = "https://changed.invalid"
        elif mode == "pin":
            kernel.loop.model = ModelId("external")
        elif mode == "tags":
            # Fix a plan against a declaration with missing requirements; no inference can begin.
            provider.catalog.entries["local"]["tags"] = []
            record = kernel.handoffs.record(spec)
        with pytest.raises(ValueError):
            await kernel.handoffs.run(record.id)
        assert not provider.requests and not (provider.root / "B.txt").exists()
    finally:
        kernel.session.close()


@pytest.mark.parametrize("reason", ["source-deny", "current-deny", "budget", "workspace", "custom-hook"])
async def test_continuation_cannot_expand_source_authority_or_reset_budget(tmp_path, reason):
    engine = permissions(PermissionRule("deny", "write_file", {"file_path": "*/B.txt"})) \
        if reason == "source-deny" else permissions()
    kernel, provider = await source(tmp_path, engine=engine,
        limits=ExecutionLimits(max_model_calls=1) if reason == "budget" else None)
    record = kernel.handoffs.record(specification(kernel))
    session_id = kernel.session.id
    kernel.session.close()
    current = permissions(PermissionRule("deny", "write_file", {"file_path": "*/B.txt"})) \
        if reason == "current-deny" else permissions()
    kernel = build_kernel(base_dir=tmp_path / "sessions", provider=provider, model=ModelId("local"),
        resume_session_id=session_id, native_tools=True,
        workspace_root=tmp_path if reason == "workspace" else provider.root, permissions=current)
    try:
        if reason == "custom-hook":
            # A newly installed policy still runs, even though it was not in the source snapshot.
            from harness.hooks import Block
            kernel.hooks.register_dispatch("new-policy", lambda action: Block(reason="new policy denies continuation"))
        if reason in {"source-deny", "current-deny"}:
            await kernel.handoffs.run(record.id)
            assert not (provider.root / "B.txt").exists()
            assert kernel.tasks.check().evidence["B"].status != "passed"
        else:
            with pytest.raises(Exception):
                await kernel.handoffs.run(record.id)
            assert not provider.requests
        assert kernel.tasks.selected().definition.id == record.task_id
    finally:
        kernel.session.close()


async def test_cancelled_handoff_is_consumed_and_requires_fresh_reconciliation_after_resume(tmp_path):
    kernel, provider = await source(tmp_path)
    record = kernel.handoffs.record(specification(kernel))
    provider.hang = True
    work = asyncio.create_task(kernel.handoffs.run(record.id))
    await provider.entered.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    session_id = kernel.session.id
    kernel.session.close()
    resumed = build_kernel(base_dir=tmp_path / "sessions", provider=provider, model=ModelId("local"),
        resume_session_id=session_id, native_tools=True, workspace_root=provider.root, permissions=permissions())
    try:
        with pytest.raises(ValueError, match="already attempted"):
            await resumed.handoffs.run(record.id)
        with pytest.raises(ValueError, match="reconcile external effects"):
            await resumed.loop.run_task(resumed.tasks.prepare("try again"))
        assert snapshot(resumed.session).run_id != record.source_run_id
        state = fold(read_session(resumed.session.base, session_id))
        assert not state.open_agent_runs and not state.open_model_intents and len(provider.requests) == 1
    finally:
        resumed.session.close()


def test_spec_cannot_enable_shell_delegation_or_hide_inspection():
    base = dict(snapshot_sha256="a" * 64, model="local", continuation="remaining work", process_stopped=True,
                resolutions=())
    for tool in ("bash", "dispatch_agent", "mcp__plugin__tool"):
        with pytest.raises(ValueError):
            HandoffSpec(**base, allowed_calls=(ExactCall(tool=tool, args={}),))
    with pytest.raises(ValueError):
        HandoffSpec(**{**base, "process_stopped": False})


async def test_headless_inspection_does_not_resume_or_call_provider(tmp_path, monkeypatch, capsys):
    kernel, provider = await source(tmp_path)
    session_id, base = kernel.session.id, kernel.session.base
    kernel.session.close()
    before = read_session(base, session_id)
    monkeypatch.setattr("sys.argv", ["harness", "handoff", "inspect", str(session_id), "--base-dir", str(base), "--json"])
    from harness.cli import main
    main()
    report = json.loads(capsys.readouterr().out)
    assert len(report["snapshot_sha256"]) == 64
    assert any(e["kind"] == "provider_native" for e in report["checkpoint"]["effects"])
    assert read_session(base, session_id) == before and not provider.requests


async def test_chained_handoff_retains_completed_effects_and_original_permission_floor(tmp_path):
    from harness.errors import AuthFailed
    kernel, provider = await source(tmp_path, engine=permissions(
        PermissionRule("deny", "write_file", {"file_path": "*/C.txt"})))
    record = kernel.handoffs.record(specification(kernel))
    session_id = kernel.session.id
    kernel.session.close()
    kernel = build_kernel(base_dir=tmp_path / "sessions", provider=provider, model=ModelId("local"),
        resume_session_id=session_id, native_tools=True, workspace_root=provider.root, permissions=permissions())
    try:
        provider.steps = ["new", "fail"]
        with pytest.raises(AuthFailed):
            await kernel.handoffs.run(record.id)
        assert (provider.root / "B.txt").exists()
        checkpoint = snapshot(kernel.session)
        assert {e.call.args["file_path"] for e in checkpoint.effects if e.call and e.state == "completed"} == {
            str(provider.root / "A.txt"), str(provider.root / "B.txt")}
        assert any(e.operator_note for e in checkpoint.effects if e.kind == "provider_native")
        with pytest.raises(ValueError, match="completed action"):
            kernel.handoffs.record(specification(kernel))  # B has now completed too.
        spec = specification(kernel, allowed_calls=(ExactCall(tool="write_file", args={
            "file_path": str(provider.root / "C.txt"), "content": "stage C\n"}),))
        next_record = kernel.handoffs.record(spec)
        provider.steps = ["third", "done"]
        await kernel.handoffs.run(next_record.id)
        assert not (provider.root / "C.txt").exists()  # Original deny survives the weaker resumed engine.
        checked = kernel.tasks.check()
        assert checked.evidence["A"].status == "passed" and checked.evidence["B"].status == "passed"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("change", ["rewrite", "binding", "source-hook", "source-code", "child"])
async def test_handoff_fails_closed_for_unportable_scope_and_final_rewrites(tmp_path, change, monkeypatch):
    kernel, provider = await source(tmp_path)
    try:
        if change == "source-hook":
            # Record a second source attempt with an opaque policy, without bypassing the retry guard.
            from harness.hooks import Allow
            kernel.tasks.create("Separate source task")
            kernel.hooks.register_dispatch("opaque-policy", lambda action: Allow())
            kernel.loop.model = ModelId("external")
            with pytest.raises(NetworkFailed):
                await kernel.loop.run_task(kernel.tasks.prepare("new source"))
            kernel.loop.model = ModelId("local")
        record = kernel.handoffs.record(specification(kernel))
        if change == "rewrite":
            from harness.hooks import Allow, ProposedModelCall, Rewrite
            kernel.hooks.register_dispatch("rewrite-after-review", lambda action:
                Rewrite(ProposedModelCall(action.call_id, ModelId("external")))
                if isinstance(action, ProposedModelCall) else Allow(), priority=2000)
        elif change == "binding":
            kernel.registry.get("write_file")._root = tmp_path
        elif change == "source-code":
            monkeypatch.setattr("harness.handoff.policy_version", lambda: "b" * 64)
        elif change == "child":
            from harness.events import SubagentSpawned
            from harness.types import SessionId
            kernel.session.append(SubagentSpawned(child_session_id=SessionId("unreconciled-child")))
        with pytest.raises(Exception):
            await kernel.handoffs.run(record.id)
        assert not provider.requests and not (provider.root / "B.txt").exists()
    finally:
        kernel.session.close()


async def test_cancel_waits_for_started_native_file_thread_even_after_repeated_cancel(tmp_path, monkeypatch):
    import threading
    from harness.native_tools import WriteFileTool
    kernel, provider = await source(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = WriteFileTool._write

    def paused_write(tool, path, content, expected):
        entered.set()
        assert release.wait(5)
        return original(tool, path, content, expected)

    monkeypatch.setattr(WriteFileTool, "_write", paused_write)
    provider.steps = ["new", "done"]
    record = kernel.handoffs.record(specification(kernel))
    work = asyncio.create_task(kernel.handoffs.run(record.id))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        for _ in range(2):
            work.cancel()
            await asyncio.sleep(0.02)
            assert not work.done()
            assert fold(read_session(kernel.session.base, kernel.session.id)).open_agent_runs
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert (provider.root / "B.txt").read_text() == "stage B\n"
        state = fold(read_session(kernel.session.base, kernel.session.id))
        assert not state.open_agent_runs and not state.open_intents
        checkpoint = snapshot(kernel.session)
        assert any(e.call and e.call.args.get("file_path") == str(provider.root / "B.txt")
                   and e.state == "uncertain" for e in checkpoint.effects)
    finally:
        release.set()
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        kernel.session.close()


@pytest.mark.parametrize("mode", ["changed-content", "relative-source", "ignored-arg", "relative-destination"])
async def test_exact_allowlist_rejects_completed_targets_and_argument_aliases(tmp_path, mode):
    from harness.handoff import validate_resolutions
    kernel, provider = await source(tmp_path)
    try:
        checkpoint = snapshot(kernel.session)
        if mode in {"changed-content", "relative-source"}:
            if mode == "relative-source":
                checkpoint = checkpoint.model_copy(update={"effects": tuple(
                    e.model_copy(update={"call": ExactCall(tool="write_file", args={"file_path": "A.txt",
                        "content": "stage A\n"})}) if e.call else e for e in checkpoint.effects)})
            spec = specification(kernel, snapshot_sha256=checkpoint.digest, allowed_calls=(ExactCall(
                tool="write_file", args={"file_path": str(provider.root / "A.txt"), "content": "different"}),))
            with pytest.raises(ValueError, match="completed file target"):
                validate_resolutions(checkpoint, spec)
        else:
            args = {"file_path": str(provider.root / "B.txt"), "content": "stage B\n"}
            if mode == "ignored-arg":
                args["ignored"] = True
            else:
                args["file_path"] = "B.txt"
            record = kernel.handoffs.record(specification(kernel, allowed_calls=(ExactCall(tool="write_file", args=args),)))
            with pytest.raises(ValueError, match="ignored|canonical"):
                await kernel.handoffs.run(record.id)
        assert not provider.requests
    finally:
        kernel.session.close()


async def test_real_external_process_mcp_failure_restart_and_native_continuation(tmp_path):
    from harness.provider_litellm import CatalogProvider
    from scripts.qualify_handoff import journey

    class ScriptedDestination(CatalogProvider):
        called = False

        async def infer(self, request):
            if self.called:
                chunks = text_turn("Stage B completed")
            else:
                self.called = True
                chunks = tool_call_turn("", ToolName("write_file"), {
                    "file_path": str(tmp_path / "project/B.txt"), "content": "stage B\n"})
            for chunk in chunks:
                yield chunk

    provider = ScriptedDestination(Catalog({"external": {"backend": "codex", "route": "codex/default"},
                                            "local-small": {"route": "openai/local-small"}}))
    report = await journey(tmp_path, provider)
    assert report["passed"], report


async def test_context_binding_tracks_mcp_declarations_without_recording_secrets(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from mcp.types import Tool
    from harness.context import ContextPolicy, ContextSource
    from harness.handoff import context_bindings
    from harness.mcp_config import McpServerSpec
    from harness.mcp_host import McpTool

    kernel, _ = await source(tmp_path)
    try:
        dispatcher = kernel.loop.dispatcher
        policy = ContextPolicy(sources=(ContextSource(id="normal-memory", tool="mcp__memory__lookup"),))
        dispatcher.scope = replace(dispatcher.scope, context_policy=policy)
        assert context_bindings(dispatcher) == {"mcp__memory__lookup": None}
        spec = McpServerSpec(name="memory", transport="http", url="http://127.0.0.1:9200/mcp",
                             headers={"Authorization": "HANDOFF_TEST_SECRET"})
        monkeypatch.setenv("HANDOFF_TEST_SECRET", "do-not-record-this")
        conn = SimpleNamespace(spec=spec)
        tool = McpTool(conn, Tool(name="lookup", inputSchema={"type": "object"}))
        kernel.registry.register(tool)
        before = context_bindings(dispatcher)
        assert before["mcp__memory__lookup"] and "do-not-record-this" not in str(before)
        conn.spec = replace(spec, url="http://127.0.0.1:9201/mcp")
        assert context_bindings(dispatcher) != before
    finally:
        kernel.session.close()
