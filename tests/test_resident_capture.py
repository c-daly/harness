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


async def complete_turn(kernel, task):
    result = await kernel.loop.run_task(task)
    await kernel.loop.captures.wait()
    return result


def facts(kernel):
    return read_session(kernel.session.base, kernel.session.id)


def state(kernel):
    return capture_state(facts(kernel))


async def test_capture_is_audited_and_excluded_from_conversation(tmp_path):
    kernel, (prep, save) = await kernel_at(tmp_path)
    try:
        result = await complete_turn(kernel, AgentTask(prompt="Correction: use the test workspace."))
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
        result = await complete_turn(kernel, AgentTask(prompt="remember this"))
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
    result = await kernel.loop.run_task(AgentTask(prompt="correction"))
    assert result.status == "completed"
    await asyncio.wait_for(entered.wait(), 3)
    await kernel.loop.captures.pause()
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
        await complete_turn(kernel, AgentTask(prompt="remember this"))
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
        result = await complete_turn(kernel, AgentTask(prompt="work"))
        assert result.status == "completed"
        assert not next(a for a in adapters if a.spec.name == tool).calls
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("record,expected", [("<<SKIP>>", "skipped"), ("", "pending")])
async def test_skip_and_empty_record_are_distinct(tmp_path, record, expected):
    kernel, (_, save) = await kernel_at(tmp_path, provider=FakeProvider([text_turn("ok"), text_turn(record)]))
    try:
        await complete_turn(kernel, AgentTask(prompt="thanks"))
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
        result = await complete_turn(kernel, AgentTask(prompt="work"))
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
        result = await complete_turn(kernel, AgentTask(prompt="work"))
        assert result.status == "completed" and not save.calls
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("change", [{"project": "other"}, {"workspace": "/other"}, {"write_tool": "other"}])
async def test_policy_change_cannot_redirect_prepared_write(tmp_path, change):
    kernel, _ = await kernel_at(tmp_path, save_fn=lambda _: "error: offline")
    await complete_turn(kernel, AgentTask(prompt="work"))
    sid = kernel.session.id
    kernel.session.close()
    resumed, (prep, save) = await kernel_at(tmp_path, resume=sid,
        policy=profile(tmp_path, **change), provider=FakeProvider([]))
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
            await complete_turn(kernel, AgentTask(prompt="work"))
        assert not kernel.loop._task_active
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


async def test_reenabled_capture_does_not_capture_runs_made_with_profile_cleared(tmp_path):
    first, _ = await kernel_at(tmp_path)
    await complete_turn(first, AgentTask(prompt="recorded"))
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
        result = await complete_turn(kernel, AgentTask(prompt="work"))
        assert result.status == "completed" and not prep.calls and not save.calls
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


async def test_capture_model_permission_is_independent_of_resident(tmp_path):
    permissions = PermissionEngine([RuleSet(rules=[PermissionRule("deny", "model:recorder")], default="allow")])
    kernel, (_, save) = await kernel_at(tmp_path, permissions=permissions)
    try:
        result = await complete_turn(kernel, AgentTask(prompt="work"))
        assert result.status == "completed" and not save.calls
        assert len(kernel.provider.calls) == 1
        assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        kernel.session.close()


def test_capture_local_admission_has_background_priority():
    from harness.scheduling import request_priority
    assert request_priority("capture", 0) == "background"


@pytest.mark.parametrize("phase", ["prepare", "model", "write"])
async def test_completed_turn_returns_and_new_turn_preempts_capture(tmp_path, phase):
    entered, settled = asyncio.Event(), asyncio.Event()

    async def hang():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            settled.set()

    async def prepare_fn(args):
        if phase == "prepare":
            await hang()
        return prepare(args)

    async def save_fn(args):
        if phase == "write":
            await hang()
        return receipt(args)

    class Provider:
        foreground_calls = 0

        async def complete(self, **kwargs):
            if kwargs["model"] == "recorder":
                if phase == "model":
                    await hang()
                answer = "record"
            else:
                self.foreground_calls += 1
                if self.foreground_calls == 2:
                    assert settled.is_set(), "foreground overlapped an unsettled capture call"
                answer = "answer"
            for chunk in text_turn(answer):
                yield chunk

    kernel, _ = await kernel_at(tmp_path, provider=Provider(),
        prepare_fn=prepare_fn, save_fn=save_fn, policy=profile(tmp_path, timeout_seconds=120))
    try:
        first = await asyncio.wait_for(kernel.loop.run_task(AgentTask(prompt="first")), 3)
        assert first.status == "completed"
        await asyncio.wait_for(entered.wait(), 3)
        assert not kernel.loop._task_active
        assert not settled.is_set()
        second = await asyncio.wait_for(kernel.loop.run_task(AgentTask(prompt="second")), 3)
        assert second.status == "completed" and settled.is_set()
        requests, _, observed = state(kernel)
        original = next(r for r in requests.values() if r.run_id == first.run_id)
        assert observed[original.id].status == "pending"
        assert len(requests) == 2
    finally:
        await kernel.loop.end()
        assert kernel.loop.captures._worker is None
        assert not fold(facts(kernel)).open_intents
        kernel.session.close()


async def test_idle_pass_attempts_later_captures_after_old_failure_without_spinning(tmp_path, monkeypatch):
    def prepare_fn(args):
        if json.loads(args["transcript"])["user"] == "old":
            raise RuntimeError("old record cannot be prepared")
        return prepare(args)

    kernel, (prep, save) = await kernel_at(tmp_path, prepare_fn=prepare_fn,
        provider=FakeProvider([text_turn("one"), text_turn("two"), text_turn("second record"),
                               text_turn("three"), text_turn("third record")]))
    service = kernel.loop.captures
    try:
        with monkeypatch.context() as patch:
            patch.setattr(service, "schedule", lambda: None)
            old = await kernel.loop.run_task(AgentTask(prompt="old"))
            await kernel.loop.run_task(AgentTask(prompt="newer"))
        service.schedule()
        await service.wait()
        assert len(prep.calls) == 2 and len(save.calls) == 1
        assert sorted(o.status for o in state(kernel)[2].values()) == ["pending", "saved"]
        # The next fresh request goes first; the failed old request still gets
        # one attempt per pass and is never an automatic retry loop.
        await complete_turn(kernel, AgentTask(prompt="newest"))
        assert [json.loads(c["transcript"])["user"] for c in prep.calls] == ["old", "newer", "newest", "old"]
        requests, _, observed = state(kernel)
        assert observed[next(r.id for r in requests.values() if r.run_id == old.run_id)].status == "pending"
        assert len(save.calls) == 2
    finally:
        await service.close()
        kernel.session.close()


@pytest.mark.parametrize("change", [
    {"timeout_seconds": 60}, {"max_output_tokens": 2048}, {"max_record_bytes": 1},
    {"model": "different-recorder"}, {"prepare_tool": "different-prepare"},
    {"max_input_bytes": 1, "max_transcript_bytes": 1},
])
async def test_tuning_policy_replays_prepared_bytes_without_reinference(tmp_path, change):
    kernel, (_, save) = await kernel_at(tmp_path, save_fn=lambda _: "offline")
    await complete_turn(kernel, AgentTask(prompt="retain this"))
    sent, sid = save.calls[0], kernel.session.id
    kernel.session.close()
    resumed, (prep, save) = await kernel_at(tmp_path, resume=sid,
        provider=FakeProvider([]), policy=profile(tmp_path, **change))
    try:
        await resumed.loop.captures.retry()
        assert not prep.calls and not resumed.provider.calls
        assert save.calls == [sent]
        assert next(iter(state(resumed)[2].values())).status == "saved"
    finally:
        resumed.session.close()


@pytest.mark.parametrize("rewrite", ["tool", "args", "in_place", "identity"])
async def test_capture_receipt_requires_exact_dispatched_writer(tmp_path, rewrite):
    from harness.hooks import Allow, ProposedToolCall, Rewrite

    kernel, (_, save) = await kernel_at(tmp_path)
    spoof = Adapter("spoof", receipt)
    kernel.registry.register(spoof)

    def hook(call):
        if not isinstance(call, ProposedToolCall) or call.tool != "save":
            return Allow()
        if rewrite == "in_place":
            call.args["project"] = "redirected"
            return Allow()
        return Rewrite(ProposedToolCall(call.call_id,
            ToolName("spoof") if rewrite == "tool" else call.tool,
            {**call.args, "project": "redirected"} if rewrite == "args" else dict(call.args)))

    kernel.hooks.register_dispatch("rewrite-writer", hook)
    try:
        await complete_turn(kernel, AgentTask(prompt="remember"))
        assert not spoof.calls, "a matching fake receipt must never replace the real writer"
        if rewrite == "identity":
            assert len(save.calls) == 1
            assert next(iter(state(kernel)[2].values())).status == "saved"
        else:
            assert not save.calls
            assert next(iter(state(kernel)[2].values())).status == "pending"
    finally:
        await kernel.loop.captures.close()
        kernel.session.close()


async def test_close_settles_capture_before_session_end(tmp_path):
    entered, settled = asyncio.Event(), asyncio.Event()

    async def write(args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            settled.set()

    kernel, _ = await kernel_at(tmp_path, save_fn=write)
    try:
        await kernel.loop.run_task(AgentTask(prompt="remember"))
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(kernel.loop.end(), 3)
        assert settled.is_set() and kernel.loop.captures._worker is None
        assert next(iter(state(kernel)[2].values())).status == "pending"
        events = facts(kernel)
        assert events[-1].event.type == "session_ended"
        assert not fold(events).open_intents
        kernel.loop.captures.schedule()
        assert kernel.loop.captures._worker is None
    finally:
        await kernel.loop.captures.close()
        kernel.session.close()


async def test_foreground_cancellation_during_capture_cleanup_is_not_consumed(tmp_path):
    entered, cleaning = asyncio.Event(), asyncio.Event()

    async def write(args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await asyncio.Future()

    kernel, _ = await kernel_at(tmp_path, save_fn=write)
    try:
        await kernel.loop.run_task(AgentTask(prompt="first"))
        await asyncio.wait_for(entered.wait(), 3)
        foreground = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="cancel me")))
        await asyncio.wait_for(cleaning.wait(), 3)
        foreground.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreground
        assert kernel.loop.captures._worker is None and not kernel.loop._task_active
        assert len(state(kernel)[0]) == 1
        assert not fold(facts(kernel)).open_intents
    finally:
        await kernel.loop.captures.close()
        kernel.session.close()
