"""Configured context is owned by core, enforced normally, and never guessed."""

import asyncio

import pytest

from harness.agent import AgentTask, TaskLimits
from harness.cli import build_kernel
from harness.context import ContextPolicy
from harness.events import CompactionApplied
from harness.fold import fold
from harness.hooks import Allow, Block, ProposedToolCall, Rewrite
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName


def policy(**source_changes):
    return ContextPolicy.model_validate({"history_turns": 1, "max_input_bytes": 8192,
        "sources": [{"id": "normal-memory", "tool": "memory_lookup", "args": {"subject": "project"},
                     **source_changes}]})


class Lookup:
    spec = ToolSpec(name=ToolName("memory_lookup"), description="Fixture lookup",
                    parameters={"type": "object", "properties": {"subject": {"type": "string"}},
                                "required": ["subject"]})

    def __init__(self, text="Stored preference: report the retry limit."):
        self.text, self.calls = text, []

    async def __call__(self, args):
        self.calls.append(args)
        return self.text


async def make_kernel(tmp_path, *, profile, provider=None, lookup=None, permissions=None, **kwargs):
    kernel = build_kernel(base_dir=tmp_path, provider=provider or FakeProvider([text_turn("answer")]),
                          model=ModelId("fake"), context_policy=profile, permissions=permissions, **kwargs)
    if lookup is not None:
        kernel.registry.register(lookup)
    await kernel.loop.start()
    return kernel


def observations(kernel):
    return [env.event for env in read_session(kernel.session.base, kernel.session.id)
            if env.event.type == "context_source_observed"]


async def test_source_runs_once_before_inference_and_remains_pinned_across_iterations(tmp_path):
    lookup = Lookup()
    provider = FakeProvider([tool_call_turn("step", ToolName("echo"), {}), text_turn("answer")])
    kernel = await make_kernel(tmp_path, profile=policy(), provider=provider, lookup=lookup)
    try:
        task = AgentTask(prompt="work")
        result = await kernel.loop.run_task(task)
        assert lookup.calls == [{"subject": "project"}]
        assert len(provider.calls) == 2
        assert all(lookup.text in "\n".join(m.text() for m in call) for call in provider.calls)
        ready = [event for event in observations(kernel) if event.status == "ready"]
        assert len(ready) == 1 and ready[0].task_id == task.id and ready[0].run_id == result.run_id
        assert kernel.session.blobs.get(ready[0].result).decode() == lookup.text
        events = read_session(tmp_path, kernel.session.id)
        assert next(e.seq for e in events if e.event.type == "context_source_observed" and e.event.status == "ready") < next(
            e.seq for e in events if e.event.type == "model_call_proposed")
        # Core source calls are real tool facts, not invented assistant tool proposals.
        assert not any(lookup.text in m.text() for m in fold(events).messages)
        assert kernel.loop.history == fold(events).messages
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["missing", "denied", "filtered", "error", "oversized"])
async def test_optional_source_failure_is_visible_and_does_not_supply_fake_memory(tmp_path, mode):
    class Broken(Lookup):
        async def __call__(self, args):
            raise RuntimeError("private failure detail")
    lookup = Broken() if mode == "error" else Lookup("private payload" * 20 if mode == "oversized" else "private payload")
    profile = policy(max_bytes=128)
    if mode == "filtered":
        profile = profile.model_copy(update={"tools": ()})
    permissions = PermissionEngine([RuleSet(rules=[PermissionRule("deny", "memory_lookup")], default="allow")]) if mode == "denied" else None
    kernel = await make_kernel(tmp_path, profile=profile, lookup=None if mode == "missing" else lookup,
                               permissions=permissions)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work"))
        assert result.status == "completed"
        last = observations(kernel)[-1]
        assert last.status in {"unavailable", "oversized"} and last.result is None
        context = "\n".join(m.text() for m in kernel.provider.calls[0])
        assert "normal-memory" in context and "unavailable" in context
        assert "private payload" not in context and "private failure detail" not in context
    finally:
        kernel.session.close()


async def test_required_source_failure_stops_before_model_call(tmp_path):
    kernel = await make_kernel(tmp_path, profile=policy(required=True))
    try:
        with pytest.raises(RuntimeError, match="required context source"):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert not kernel.provider.calls
        state = fold(read_session(tmp_path, kernel.session.id))
        assert not state.open_agent_runs and not state.open_intents
        assert next(iter(state.agent_runs.values())).status == "failed"
    finally:
        kernel.session.close()


async def test_source_arguments_and_rewrites_use_existing_enforcement(tmp_path):
    lookup = Lookup()
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=lookup)
    seen = []
    async def rewrite(action):
        if isinstance(action, ProposedToolCall):
            return Rewrite(ProposedToolCall(call_id=action.call_id, tool=action.tool, args={"subject": "rewritten"}))
        return Allow()
    async def deny(action):
        if isinstance(action, ProposedToolCall):
            seen.append(action.args)
            return Block("not granted")
        return Allow()
    kernel.hooks.register_dispatch("rewrite-context", rewrite, priority=10)
    kernel.hooks.register_dispatch("deny-context", deny, priority=20)
    try:
        await kernel.loop.run_task(AgentTask(prompt="work"))
        assert seen == [{"subject": "rewritten"}] and not lookup.calls
        assert observations(kernel)[-1].status == "unavailable"
    finally:
        kernel.session.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_source_timeout_or_cancellation_settles_before_the_attempt_finishes(tmp_path, cancel):
    entered, closed = asyncio.Event(), asyncio.Event()
    class Waiting(Lookup):
        async def __call__(self, args):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
    kernel = await make_kernel(tmp_path, profile=policy(timeout_seconds=30 if cancel else 0.05), lookup=Waiting())
    try:
        running = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work")))
        await entered.wait()
        if cancel:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert not kernel.provider.calls
        else:
            assert (await running).status == "completed"
        assert closed.is_set()
        assert observations(kernel)[-1].status == ("cancelled" if cancel else "timeout")
        state = fold(read_session(tmp_path, kernel.session.id))
        assert not state.open_intents and not state.open_agent_runs
    finally:
        kernel.session.close()


async def test_source_configuration_and_task_continuity_survive_short_history_and_resume(tmp_path):
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=Lookup())
    kernel.tasks.create("Implement the retry policy")
    kernel.tasks.add_requirement({"id": "review", "description": "Check the retry behavior"})
    result = await kernel.loop.run_task(kernel.tasks.prepare("first"))
    kernel.session.append(CompactionApplied(from_seq=1, to_seq=10000, summary="old work"))
    kernel.session.close()
    provider, lookup = FakeProvider([text_turn("continued")]), Lookup("Fresh memory after restart")
    resumed = build_kernel(base_dir=tmp_path, provider=provider, model=ModelId("other"), resume_session_id=kernel.session.id)
    resumed.registry.register(lookup)
    try:
        assert resumed.context_policy == policy()
        continued = await resumed.loop.run_task(resumed.tasks.prepare("continue"))
        context = "\n".join(m.text() for m in provider.calls[0])
        assert "Fresh memory after restart" in context
        assert result.run_id in context and "Previous task attempt" in context
        assert "fresh evidence" in context
        assert continued.task_id == result.task_id and not resumed.tasks.selected().accepted
    finally:
        resumed.session.close()


async def test_zero_iteration_budget_never_fetches_context(tmp_path):
    lookup = Lookup()
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=lookup)
    try:
        result = await kernel.loop.run_task(AgentTask(prompt="work", limits=TaskLimits(max_iterations=0)))
        assert result.status == "incomplete" and not lookup.calls and not kernel.provider.calls
    finally:
        kernel.session.close()


@pytest.mark.parametrize("change", [
    {"tool": "*"}, {"id": "two words"}, {"max_bytes": 16385}, {"max_bytes": True},
    {"timeout_seconds": 0}, {"timeout_seconds": float("inf")}, {"required": "yes"},
    {"args": {"value": "x" * 8192}}, {"extra": True},
])
def test_invalid_source_configuration_fails_closed(change):
    with pytest.raises(ValueError):
        policy(**change)


def test_duplicate_or_excessive_sources_are_rejected():
    source = policy().sources[0].model_dump()
    for sources in ([source, source], [{**source, "id": f"source{i}"} for i in range(5)]):
        with pytest.raises(ValueError):
            ContextPolicy.model_validate({"sources": sources})


@pytest.mark.parametrize("mode", ["budget", "arguments", "child"])
async def test_context_cannot_bypass_budget_schema_or_child_boundary(tmp_path, mode):
    from dataclasses import replace
    from harness.execution import ExecutionLimits
    lookup = Lookup()
    kernel = await make_kernel(tmp_path, profile=policy(args={"subject": 3}) if mode == "arguments" else policy(),
                               lookup=lookup, execution_limits=ExecutionLimits(max_tool_calls=0 if mode == "budget" else 4))
    if mode == "child":
        kernel.loop.dispatcher.scope = replace(kernel.loop.dispatcher.scope, depth=1)
    try:
        await kernel.loop.run_task(AgentTask(prompt="work"))
        assert not lookup.calls
        if mode == "child":
            assert not observations(kernel)
        else:
            assert observations(kernel)[-1].status == "unavailable"
    finally:
        kernel.session.close()


async def test_oversized_blob_is_not_materialized(tmp_path, monkeypatch):
    lookup = Lookup("x" * 20000)
    kernel = await make_kernel(tmp_path, profile=policy(max_bytes=100), lookup=lookup)
    original_get = kernel.session.blobs.get
    def bounded_get(ref):
        assert ref.size < 20000, "oversized source must not be materialized"
        return original_get(ref)
    monkeypatch.setattr(kernel.session.blobs, "get", bounded_get)
    try:
        await kernel.loop.run_task(AgentTask(prompt="work"))
        assert observations(kernel)[-1].status == "oversized"
    finally:
        kernel.session.close()


async def test_pinned_sources_respect_total_input_budget(tmp_path):
    from harness.errors import ContextOverflow
    kernel = await make_kernel(tmp_path, profile=policy(max_bytes=8192), lookup=Lookup("x" * 7000))
    try:
        with pytest.raises(ContextOverflow):
            await kernel.loop.run_task(AgentTask(prompt="work", limits=TaskLimits(max_input_bytes=1024)))
        assert not kernel.provider.calls
    finally:
        kernel.session.close()


@pytest.mark.parametrize("bridge", [False, True])
async def test_external_agents_receive_context_exactly_once(tmp_path, bridge):
    from harness.agent_runtime import bind_agent_runtime
    from harness.provider import TextDelta, StreamStop
    from tests.test_external_agent_runtime import ScriptedCodex
    backend, lookup = ScriptedCodex([TextDelta("done"), StreamStop("end_turn")]), Lookup()
    kernel = await make_kernel(tmp_path, profile=policy(), provider=backend, lookup=lookup)
    try:
        runtime = kernel.loop if bridge else bind_agent_runtime(backend, ModelId("codex/default"), kernel.loop.dispatcher)
        result = await runtime.run_task(AgentTask(prompt="work"))
        assert result.status == "completed" and len(lookup.calls) == 1
        assert sum(lookup.text in m.text() for m in backend.received["messages"]) == 1
        assert kernel.loop.history == fold(read_session(tmp_path, kernel.session.id)).messages if bridge else True
    finally:
        kernel.session.close()


async def test_clearing_saved_profile_disables_sources_on_resume(tmp_path):
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=Lookup())
    kernel.session.close()
    lookup = Lookup()
    resumed = build_kernel(base_dir=tmp_path, provider=FakeProvider([text_turn("done")]), model=ModelId("fake"),
                           resume_session_id=kernel.session.id, inherit_context_policy=False)
    resumed.registry.register(lookup)
    try:
        await resumed.loop.run_task(AgentTask(prompt="work"))
        assert not lookup.calls and resumed.context_policy is None
    finally:
        resumed.session.close()


async def test_standalone_external_agent_cannot_widen_context_profile(tmp_path):
    from harness.agent_runtime import bind_agent_runtime
    from harness.errors import ContextOverflow
    from harness.provider import TextDelta, StreamStop
    from tests.test_external_agent_runtime import ScriptedCodex
    backend = ScriptedCodex([TextDelta("done"), StreamStop("end_turn")])
    profile = policy().model_copy(update={"max_input_bytes": 256})
    kernel = await make_kernel(tmp_path, profile=profile, provider=backend, lookup=Lookup("x" * 1000))
    try:
        runtime = bind_agent_runtime(backend, ModelId("codex/default"), kernel.loop.dispatcher)
        with pytest.raises(ContextOverflow):
            await runtime.run_task(AgentTask(prompt="work"))
        assert backend.calls == 0
    finally:
        kernel.session.close()


async def test_source_journal_failure_never_proceeds_to_inference(tmp_path, monkeypatch):
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=Lookup())
    append = kernel.session.append
    def fail(event):
        if event.type == "tool_call_completed":
            raise OSError("journal unavailable")
        return append(event)
    monkeypatch.setattr(kernel.session, "append", fail)
    try:
        with pytest.raises(OSError, match="journal unavailable"):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert not kernel.provider.calls
    finally:
        kernel.session.close()


@pytest.mark.parametrize("mode", ["blob-integrity", "encoding"])
async def test_source_dispatch_storage_errors_are_fatal_even_when_optional(tmp_path, monkeypatch, mode):
    from harness.blobs import BlobIntegrityError
    kernel = await make_kernel(tmp_path, profile=policy(), lookup=Lookup("x" * 20000 if mode == "blob-integrity" else "\ud800"))
    if mode == "blob-integrity":
        original_put = kernel.session.blobs.put
        def fail_large(data):
            if len(data) > 16384:
                raise BlobIntegrityError("corrupt existing object")
            return original_put(data)
        monkeypatch.setattr(kernel.session.blobs, "put", fail_large)
    try:
        with pytest.raises(BlobIntegrityError if mode == "blob-integrity" else UnicodeError):
            await kernel.loop.run_task(AgentTask(prompt="work"))
        assert not kernel.provider.calls
    finally:
        kernel.session.close()
