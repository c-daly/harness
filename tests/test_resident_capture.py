"""Capture failure must not become memory, task acceptance, or conversation text."""

import asyncio
import hashlib
import json

import pytest

from harness.agent import AgentTask
from harness.capture import capture_state, render_captures
from harness.cli import build_kernel
from harness.context import ContextPolicy
from harness.errors import NetworkFailed
from harness.fold import fold
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, text_turn
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName


class Adapter:
    def __init__(self, name, callback):
        self.spec = ToolSpec(name=ToolName(name), description="capture fixture",
                            parameters={"type": "object"})
        self.callback = callback
        self.calls = []

    async def __call__(self, args):
        self.calls.append(args)
        value = self.callback(args)
        if asyncio.iscoroutine(value):
            value = await value
        return value if isinstance(value, str) else json.dumps(value)


def prepare(args):
    return dict(version=1, idempotent=True, prompt=args["transcript"], skip_sentinel="<<SKIP>>",
                destination="a" * 64)


def receipt(args):
    return dict(version=1, status="saved", capture_id=args["capture_id"],
                project=args["project"], name="record",
                destination=args["destination"],
                record_sha256=hashlib.sha256(args["record"].encode()).hexdigest())


def profile(root, **changes):
    return ContextPolicy.model_validate({"capture": {"project": "outing", "workspace": str(root),
        "prepare_tool": "prepare", "write_tool": "save", "model": "recorder", **changes}})


async def kernel_at(root, *, provider=None, policy=None, prepare_fn=prepare, save_fn=receipt,
                    permissions=None, resume=None, **kwargs):
    root.mkdir(exist_ok=True)
    kernel = build_kernel(base_dir=root / "state", workspace_root=root, native_tools=True,
        provider=provider or FakeProvider([text_turn("answer"), text_turn("remember the correction")]),
        model=ModelId("resident"), context_policy=policy or profile(root),
        permissions=permissions or PermissionEngine([RuleSet(default="allow")]),
        resume_session_id=resume, **kwargs)
    adapters = [Adapter("prepare", prepare_fn), Adapter("save", save_fn)]
    for adapter in adapters:
        kernel.registry.register(adapter)
    if not kernel.resumed:
        await kernel.loop.start()
    return kernel, adapters


def facts(kernel):
    return read_session(kernel.session.base, kernel.session.id)


def state(kernel):
    return capture_state(facts(kernel))


async def test_capture_is_audited_and_excluded_from_conversation(tmp_path):
    kernel, (prep, save) = await kernel_at(tmp_path)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="Correction: use the test workspace."))
        requests, prepared, observed = state(kernel)
        request = next(iter(requests.values()))
        assert request.run_id == result.run_id
        assert result.acceptance == "unverified"
        assert observed[request.id].status == "saved"
        assert prepared[request.id].record.sha256 == json.loads(kernel.session.blobs.get(
            observed[request.id].receipt))["record_sha256"]
        assert json.loads(prep.calls[0]["transcript"])["user"].startswith("Correction:")
        assert save.calls[0]["record"] == "remember the correction"
        assert len(kernel.provider.calls) == 2
        assert kernel.loop.history == fold(facts(kernel)).messages
        assert "remember the correction" not in str(fold(facts(kernel)).messages)
        assert "1 saved" in render_captures(facts(kernel))
    finally:
        kernel.session.close()


@pytest.mark.parametrize("failure", ["error_text", "wrong_id", "wrong_hash", "wrong_project", "exception"])
async def test_unverified_write_stays_pending_and_retry_reuses_record(tmp_path, failure):
    def bad(args):
        if failure == "exception":
            raise RuntimeError("write unavailable")
        if failure == "error_text":
            return "error: cannot write"
        value = receipt(args)
        value[{"wrong_id": "capture_id", "wrong_hash": "record_sha256",
               "wrong_project": "project"}[failure]] = "incorrect"
        return value
    kernel, (prep, save) = await kernel_at(tmp_path, save_fn=bad)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="remember this"))
        assert result.status == "completed"
        assert next(iter(state(kernel)[2].values())).status == "pending"
        save.callback = receipt
        await kernel.loop.captures.retry()
        assert len(prep.calls) == 1 and len(kernel.provider.calls) == 2
        assert save.calls[0] == save.calls[1]
        assert next(iter(state(kernel)[2].values())).status == "saved"
    finally:
        kernel.session.close()


async def test_cancelled_write_reopens_and_replays_identical_prepared_record(tmp_path):
    entered = asyncio.Event()

    async def hang(args):
        entered.set()
        await asyncio.Future()

    kernel, (_, save) = await kernel_at(tmp_path, save_fn=hang)
    running = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="correction")))
    await asyncio.wait_for(entered.wait(), 3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    sid, sent = kernel.session.id, save.calls[0]
    assert next(iter(state(kernel)[2].values())).status == "pending"
    assert not kernel.loop._task_active
    kernel.session.close()

    resumed, (prep, save) = await kernel_at(tmp_path, resume=sid, provider=FakeProvider([]))
    try:
        resumed.loop.captures.reconcile()
        await resumed.loop.captures.retry()
        assert len(state(resumed)[0]) == 1
        assert not prep.calls and save.calls == [sent]
        assert next(iter(state(resumed)[2].values())).status == "saved"
        assert not fold(facts(resumed)).open_intents
    finally:
        resumed.session.close()


async def test_reconcile_recovers_crash_between_terminal_run_and_capture_intent(tmp_path, monkeypatch):
    kernel, _ = await kernel_at(tmp_path)
    real = kernel.loop.captures.reconcile
    monkeypatch.setattr(kernel.loop.captures, "reconcile", lambda: None)
    try:
        await kernel.loop.run_task(AgentTask(prompt="remember this"))
        assert not state(kernel)[0]
        real()
        real()
        assert len(state(kernel)[0]) == 1
        await kernel.loop.captures.retry()
        assert next(iter(state(kernel)[2].values())).status == "saved"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("tool", ["prepare", "save"])
async def test_capture_cannot_bypass_tool_permissions(tmp_path, tool):
    permissions = PermissionEngine([RuleSet(rules=[PermissionRule("deny", tool)], default="allow")])
    kernel, adapters = await kernel_at(tmp_path, permissions=permissions)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed"
        assert not next(a for a in adapters if a.spec.name == tool).calls
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("record,expected", [("<<SKIP>>", "skipped"), ("", "pending")])
async def test_skip_and_empty_record_are_distinct(tmp_path, record, expected):
    kernel, (_, save) = await kernel_at(tmp_path, provider=FakeProvider([text_turn("ok"), text_turn(record)]))
    try:
        await kernel.loop.run_task(AgentTask(prompt="thanks"))
        assert not save.calls
        assert next(iter(state(kernel)[2].values())).status == expected
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["workspace", "source_limit", "non_idempotent", "timeout"])
async def test_capture_failures_preserve_task_and_pending_intent(tmp_path, mode):
    config = profile(tmp_path, **({"workspace": str(tmp_path / "other")} if mode == "workspace" else
                                 {"max_transcript_bytes": 1} if mode == "source_limit" else
                                 {"timeout_seconds": .01} if mode == "timeout" else {}))

    async def prep(args):
        if mode == "timeout":
            await asyncio.Future()
        value = prepare(args)
        if mode == "non_idempotent":
            value["idempotent"] = False
        return value

    kernel, (_, save) = await kernel_at(tmp_path, policy=config, prepare_fn=prep)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed"
        assert not save.calls and len(kernel.provider.calls) == 1
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


async def test_provider_failure_is_not_a_successful_capture(tmp_path):
    class Offline(FakeProvider):
        async def complete(self, **kwargs):
            if kwargs["model"] == "recorder":
                raise NetworkFailed("offline")
            async for chunk in super().complete(**kwargs):
                yield chunk
    kernel, (_, save) = await kernel_at(tmp_path, provider=Offline([text_turn("answer")]))
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed" and not save.calls
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


async def test_policy_change_cannot_redirect_prepared_write(tmp_path):
    kernel, _ = await kernel_at(tmp_path, save_fn=lambda _: "error: offline")
    await kernel.loop.run_task(AgentTask(prompt="work"))
    sid = kernel.session.id
    kernel.session.close()
    resumed, (prep, save) = await kernel_at(tmp_path, resume=sid,
        policy=profile(tmp_path, project="other"), provider=FakeProvider([]))
    try:
        await resumed.loop.captures.retry()
        assert not prep.calls and not save.calls
        assert "policy changed" in next(iter(state(resumed)[2].values())).reason
    finally:
        resumed.session.close()


async def test_journal_failure_propagates_and_clears_loop_busy_state(tmp_path, monkeypatch):
    kernel, _ = await kernel_at(tmp_path)
    append = kernel.session.append

    def fail(event):
        if event.type == "capture_prepared":
            raise OSError("journal full")
        return append(event)

    monkeypatch.setattr(kernel.session, "append", fail)
    try:
        with pytest.raises(OSError, match="journal full"):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert not kernel.loop._task_active
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


async def test_reenabled_capture_does_not_capture_runs_made_with_profile_cleared(tmp_path):
    first, _ = await kernel_at(tmp_path)
    await first.loop.run_task(AgentTask(prompt="recorded"))
    sid = first.session.id
    first.session.close()
    cleared = build_kernel(base_dir=tmp_path / "state", workspace_root=tmp_path, native_tools=True,
        provider=FakeProvider([text_turn("not for memory")]), model=ModelId("resident"),
        resume_session_id=sid, inherit_context_policy=False)
    try:
        excluded = await cleared.loop.run_task(AgentTask(prompt="capture is off"))
    finally:
        cleared.session.close()
    enabled, _ = await kernel_at(tmp_path, resume=sid)
    try:
        enabled.loop.captures.reconcile()
        assert all(r.run_id != excluded.run_id for r in state(enabled)[0].values())
        assert len(state(enabled)[0]) == 1
    finally:
        enabled.session.close()


async def test_context_filter_blocks_capture_tools_without_granting_permission(tmp_path):
    config = profile(tmp_path).model_copy(update={"tools": ()})
    kernel, (prep, save) = await kernel_at(tmp_path, policy=config)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed" and not prep.calls and not save.calls
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


async def test_capture_model_permission_is_independent_of_resident(tmp_path):
    permissions = PermissionEngine([RuleSet(rules=[PermissionRule("deny", "model:recorder")], default="allow")])
    kernel, (_, save) = await kernel_at(tmp_path, permissions=permissions)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed" and not save.calls
        assert len(kernel.provider.calls) == 1
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


def test_capture_local_admission_has_background_priority():
    from harness.scheduling import request_priority
    assert request_priority("capture", 0) == "background"
