"""Local queues preserve authority, deadlines, process ownership and UI control."""

import asyncio
import os
from dataclasses import replace

import httpx
import pytest

from harness.agent import AgentTask
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.errors import LocalBusy
from harness.events import LocalRequestObserved, parse_envelope_line
from harness.fold import fold
from harness.inference import InferenceRequest
from harness.log import read_session
from harness.messages import Message
from harness.provider import text_turn
from harness.provider_litellm import CatalogProvider
from harness.resources import LocalResources
from harness.scheduling import LocalScheduler
from harness.types import ModelId
from tests.test_local_resources import owned_catalog


def models():
    return Catalog({alias: {"route": "openai/test-model", "api_base": f"http://127.0.0.1:{8000+i}/v1",
        "tags": ["tools"], "local": {}} for i, alias in enumerate(("a", "b", "c", "d"))})


def ready_resources(**kwargs):
    return LocalResources(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json={"data": [{"id": "test-model"}]})), **kwargs)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_interactive_priority_fifo_and_cross_alias_exclusion():
    cat, resources = models(), ready_resources()
    entered, release = asyncio.Event(), asyncio.Event()
    order, events = [], []
    active = 0

    async def run(alias, priority, gated=False):
        nonlocal active
        async with resources.use(cat.resolve(alias), emit=events.append, priority=priority):
            active += 1
            assert active == 1
            order.append(alias)
            if gated:
                entered.set()
                await release.wait()
            active -= 1

    first = asyncio.create_task(run("a", "background", True))
    await entered.wait()
    work = asyncio.create_task(run("b", "work"))
    await until(lambda: resources.scheduler.activity("local")[1] == 1)
    interactive = asyncio.create_task(run("c", "interactive"))
    next_interactive = asyncio.create_task(run("d", "interactive"))
    await until(lambda: resources.scheduler.activity("local")[1] == 3)
    other = resources.snapshot(cat.resolve("c"))
    assert other.status == "busy" and not other.stale and other.active_alias == "a"
    assert other.queued_requests == 3
    release.set()
    await asyncio.gather(first, work, interactive, next_interactive)
    assert order == ["a", "c", "d", "b"]
    assert resources.scheduler.activity("local") == (None, 0)
    observations = [e.observation for e in events if isinstance(e, LocalRequestObserved)]
    for alias in order:
        states = [o.status for o in observations if o.alias == alias]
        assert states == (["acquired", "released"] if alias == "a" else ["queued", "acquired", "released"])
        acquired, released = [o for o in observations if o.alias == alias and o.status != "queued"]
        assert acquired.wait_ms == released.wait_ms


@pytest.mark.parametrize("mode", ["background", "full", "deadline", "cancel", "close"])
async def test_waiters_finish_without_starting_and_reservations_are_reusable(mode):
    cat, resources = models(), ready_resources(max_waiting=1)
    entered, release = asyncio.Event(), asyncio.Event()
    events, ran = [], []

    async def run(alias, *, priority="interactive", seconds=2):
        async with resources.use(cat.resolve(alias), emit=events.append, priority=priority, timeout_seconds=seconds):
            ran.append(alias)
            if alias == "a":
                entered.set()
                await release.wait()

    owner = asyncio.create_task(run("a"))
    await entered.wait()
    waiter = asyncio.create_task(run("b", seconds=0.03 if mode == "deadline" else 2,
                                     priority="background" if mode == "background" else "interactive"))
    if mode != "background":
        await until(lambda: resources.scheduler.activity("local")[1] == 1)
    if mode == "full":
        with pytest.raises(LocalBusy, match="queue_full"):
            await run("c")
        waiter.cancel()
    elif mode == "cancel":
        waiter.cancel()
    elif mode == "close":
        resources.scheduler.close()
    error = {"background": LocalBusy, "deadline": TimeoutError, "close": LocalBusy}.get(mode, asyncio.CancelledError)
    with pytest.raises(error):
        await waiter
    assert ran == ["a"] and resources.scheduler.activity("local") == ("a", 0)
    release.set()
    await owner
    if mode == "close":
        with pytest.raises(LocalBusy, match="closing"):
            await run("c")
    else:
        await run("c")
        assert ran == ["a", "c"]
    assert resources.scheduler.activity("local") == (None, 0)


async def test_cancel_after_grant_before_start_hands_capacity_to_next_waiter():
    cat, resources = models(), ready_resources()
    entered, release = asyncio.Event(), asyncio.Event()
    events, ran = [], []
    waiter = None

    def emit(event):
        events.append(event)
        if (isinstance(event, LocalRequestObserved) and event.observation.alias == "a"
                and event.observation.status == "released"):
            waiter.cancel()  # b has been granted, but has not resumed its await.

    async def run(alias):
        async with resources.use(cat.resolve(alias), emit=emit):
            ran.append(alias)
            if alias == "a":
                entered.set()
                await release.wait()

    owner = asyncio.create_task(run("a"))
    await entered.wait()
    waiter = asyncio.create_task(run("b"))
    successor = asyncio.create_task(run("c"))
    await until(lambda: resources.scheduler.activity("local")[1] == 2)
    release.set()
    await owner
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await successor
    assert ran == ["a", "c"] and resources.scheduler.activity("local") == (None, 0)


async def test_reentrant_child_fails_promptly_but_inherited_context_expires_with_owner():
    cat, resources = models(), ready_resources()
    release = asyncio.Event()

    async def delayed_child():
        await release.wait()
        async with resources.use(cat.resolve("b"), emit=lambda e: None):
            return "ran"

    async with resources.use(cat.resolve("a"), emit=lambda e: None):
        with pytest.raises(LocalBusy, match="reentrant"):
            async with resources.use(cat.resolve("b"), emit=lambda e: None):
                pytest.fail("recursive reservation must not wait on itself")
        child = asyncio.create_task(delayed_child())
    release.set()
    assert await child == "ran"


async def test_distinct_groups_can_run_together_but_endpoint_cannot_claim_two_groups():
    cat, resources = models(), ready_resources()
    cat.entries["b"]["local"]["resource_group"] = "other-device"
    async with resources.use(cat.resolve("a"), emit=lambda e: None):
        async with resources.use(cat.resolve("b"), emit=lambda e: None):
            assert resources.scheduler.activity("local")[0] == "a"
            assert resources.scheduler.activity("other-device")[0] == "b"
        cat.entries["b"]["api_base"] = cat.entries["a"]["api_base"]
        with pytest.raises(LocalBusy, match="endpoint_group_changed"):
            async with resources.use(cat.resolve("b"), emit=lambda e: None):
                pytest.fail("endpoint alias bypassed group exclusion")


@pytest.mark.parametrize("status", ["queued", "acquired", "released"])
async def test_failed_journal_does_not_strand_capacity(status):
    cat, resources = models(), ready_resources()
    entered, release = asyncio.Event(), asyncio.Event()

    def broken(event):
        if isinstance(event, LocalRequestObserved) and event.observation.status == status:
            raise OSError("disk failure")

    async def owner():
        async with resources.use(cat.resolve("a"), emit=lambda e: None):
            entered.set()
            await release.wait()

    async def failing():
        async with resources.use(cat.resolve("b"), emit=broken):
            pass

    running = asyncio.create_task(owner()) if status == "queued" else None
    if running:
        await entered.wait()
    with pytest.raises(OSError):
        await failing()
    release.set()
    if running:
        await running
    async with resources.use(cat.resolve("c"), emit=lambda e: None):
        pass
    assert resources.scheduler.activity("local") == (None, 0)


@pytest.mark.parametrize("stage", ["startup", "stream"])
async def test_dispatch_queue_deadline_preserves_budget_and_settles_events(tmp_path, monkeypatch, stage):
    cat = models()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def probe(request):
        if stage == "startup" and request.url.port == 8000:
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    async def complete(**kwargs):
        calls.append(kwargs["api_base"])
        if stage == "stream":
            entered.set()
            await release.wait()
        for chunk in text_turn("done"):
            yield chunk

    monkeypatch.setattr("harness.provider_litellm._acomplete", complete)
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(cat), model=ModelId("a"),
                          resources=LocalResources(transport=httpx.MockTransport(probe)))
    await kernel.loop.start()
    try:
        owner = asyncio.create_task(kernel.loop.run_task(AgentTask(prompt="work")))
        await entered.wait()
        with pytest.raises(TimeoutError):
            await kernel.loop.dispatcher.dispatch_inference(provider=kernel.loop.provider, request=InferenceRequest(
                model=ModelId("b"), messages=(Message.user_text("wait"),), purpose="conversation", timeout_seconds=0.03))
        release.set()
        await owner
        assert calls == [cat.entries["a"]["api_base"]]
        assert kernel.loop.dispatcher.scope.budget.model_calls == 2
        log = read_session(tmp_path, kernel.session.id)
        state = fold(log)
        assert not state.open_model_intents and not state.open_agent_runs
        assert all(parse_envelope_line(e.model_dump_json()) == e for e in log)
        observations = list(state.local_requests.values())
        assert {o.status for o in observations} == {"released", "cancelled"}
        assert all(o.call_id for o in observations)
    finally:
        release.set()
        await kernel.resources.close(emit=kernel.session.append)
        kernel.session.close()


async def test_background_race_abstains_without_starting_model(tmp_path, monkeypatch):
    from harness.hooks import Allow, HookBus, ProposedModelCall
    cat, resources = models(), ready_resources()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        for chunk in text_turn('{"kind":"question"}'):
            yield chunk

    async def occupy():
        async with resources.use(cat.resolve("a"), emit=lambda e: None):
            entered.set()
            await release.wait()

    worker = None
    hooks = HookBus()

    async def race(call):
        nonlocal worker
        if isinstance(call, ProposedModelCall):
            worker = asyncio.create_task(occupy())
            await entered.wait()
        return Allow()

    hooks.register_dispatch("occupied-after-precheck", race)
    monkeypatch.setattr("harness.provider_litellm._acomplete", complete)
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(cat), model=ModelId("b"),
                          resources=resources, hooks=hooks)
    await kernel.loop.start()
    try:
        result = await kernel.semantics.interpret("A question?", model=ModelId("b"))
        assert result.reason == "busy" and not calls
        log = read_session(tmp_path, kernel.session.id)
        assert any(isinstance(e.event, LocalRequestObserved) and e.event.observation.reason == "background_busy"
                   for e in log)
    finally:
        release.set()
        if worker:
            await worker
        kernel.session.close()


async def test_background_never_cold_starts(tmp_path):
    cat = models()
    cat.entries["a"]["local"].update(auto_start=True, command=["must-not-launch"])
    resources = LocalResources(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(
        httpx.ConnectError("offline"))))
    with pytest.raises(LocalBusy, match="already ready"):
        async with resources.use(cat.resolve("a"), emit=lambda e: None, priority="background"):
            pytest.fail("cold background call ran")
    assert not resources._owned and resources._reserved == 0


async def test_idle_owned_runtime_is_replaced_only_after_its_active_call_finishes(tmp_path, unused_tcp_port_factory):
    resources, events = LocalResources(), []
    first = owned_catalog(tmp_path, unused_tcp_port_factory()).resolve("local")
    other = tmp_path / "other"
    other.mkdir()
    second = replace(owned_catalog(other, unused_tcp_port_factory()).resolve("local"), alias="second")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with resources.use(first, emit=events.append):
            entered.set()
            await release.wait()

    async def switch():
        async with resources.use(second, emit=events.append):
            return int((other / "runtime.pid").read_text())

    owner = asyncio.create_task(hold())
    try:
        await entered.wait()
        pid = int((tmp_path / "runtime.pid").read_text())
        waiter = asyncio.create_task(switch())
        await until(lambda: resources.scheduler.activity("local")[1] == 1)
        assert not (other / "runtime.pid").exists()
        os.kill(pid, 0)
        release.set()
        await owner
        second_pid = await waiter
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        os.kill(second_pid, 0)
        assert resources._reserved == 1 and list(resources._owned) == ["second"]
        stops = [i for i, e in enumerate(events) if e.type == "local_runtime_requested" and e.action == "stop"]
        starts = [i for i, e in enumerate(events) if e.type == "local_runtime_requested" and e.action == "start"]
        assert starts[0] < stops[0] < starts[1]
    finally:
        release.set()
        await owner
        await resources.close(emit=events.append)


async def test_background_cannot_displace_warm_owned_model(tmp_path, unused_tcp_port_factory):
    resources, events = LocalResources(), []
    first = owned_catalog(tmp_path, unused_tcp_port_factory()).resolve("local")
    other = tmp_path / "other"
    other.mkdir()
    second = replace(owned_catalog(other, unused_tcp_port_factory()).resolve("local"), alias="second")
    try:
        async with resources.use(first, emit=events.append):
            pass
        pid = int((tmp_path / "runtime.pid").read_text())
        with pytest.raises(LocalBusy, match="cannot replace"):
            async with resources.use(second, emit=events.append, priority="background"):
                pytest.fail("background changed residency")
        assert not (other / "runtime.pid").exists()
        os.kill(pid, 0)
    finally:
        await resources.close(emit=events.append)


async def test_endpoint_alias_reuses_owned_process_and_stop_invalidates_all_aliases(tmp_path, unused_tcp_port):
    resources = LocalResources()
    first = owned_catalog(tmp_path, unused_tcp_port).resolve("local")
    alias = replace(first, alias="same-server")
    events = []
    try:
        async with resources.use(first, emit=events.append):
            pass
        async with resources.use(alias, emit=events.append):
            pass
        assert len(resources._owned) == 1
        assert len([e for e in events if e.type == "local_runtime_requested" and e.action == "start"]) == 1
        await resources.stop("local", emit=events.append)
        assert resources.snapshot(alias).stale
        async with resources.use(alias, emit=events.append):
            pass
        assert list(resources._owned) == ["same-server"]
    finally:
        await resources.close(emit=events.append)


async def test_owned_endpoint_can_switch_models_after_inventory_mismatch(tmp_path, unused_tcp_port):
    resources, events = LocalResources(), []
    cat = owned_catalog(tmp_path, unused_tcp_port)
    first = cat.resolve("local")
    cat.entries["local"]["route"] = "openai/other-model"
    cat.entries["local"]["local"]["command"] += ("--model-id", "other-model")
    second = replace(cat.resolve("local"), alias="second")
    try:
        async with resources.use(first, emit=events.append):
            pass
        pid = int((tmp_path / "runtime.pid").read_text())
        async with resources.use(second, emit=events.append) as observation:
            assert observation.model_id == "other-model"
            assert observation.ownership == "harness"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert list(resources._owned) == ["second"]
    finally:
        await resources.close(emit=events.append)


@pytest.mark.parametrize("mode", ["deadline", "repeated-cancel"])
async def test_interrupted_idle_unload_reaps_stubborn_process_before_next_admission(
    tmp_path, unused_tcp_port_factory, mode,
):
    resources, events = LocalResources(), []
    cat = owned_catalog(tmp_path, unused_tcp_port_factory())
    cat.entries["local"]["local"]["command"] += ("--ignore-term",)
    first = cat.resolve("local")
    other = tmp_path / "other"
    other.mkdir()
    second = replace(owned_catalog(other, unused_tcp_port_factory()).resolve("local"), alias="second")
    try:
        async with resources.use(first, emit=events.append):
            pass
        pid = int((tmp_path / "runtime.pid").read_text())
        async def switch():
            async with resources.use(second, emit=events.append, timeout_seconds=0.1 if mode == "deadline" else 5):
                pytest.fail("interrupted while reaping the previous process")

        switching = asyncio.create_task(switch())
        if mode == "repeated-cancel":
            await until(lambda: any(e.type == "local_runtime_requested" and e.action == "stop" for e in events))
            switching.cancel()
            await asyncio.sleep(0)
            switching.cancel()
        with pytest.raises(TimeoutError if mode == "deadline" else asyncio.CancelledError):
            await switching
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not resources._owned and resources._reserved == 0
        assert resources.scheduler.activity("local") == (None, 0)
        assert not (other / "runtime.pid").exists()
        async with resources.use(second, emit=events.append):
            pass
    finally:
        await resources.close(emit=events.append)


@pytest.mark.parametrize("gate", ["permission", "budget"])
async def test_authority_and_budget_reject_before_local_admission(tmp_path, gate):
    from harness.dispatcher import ModelDispatchBlocked
    from harness.execution import BudgetExceeded, ExecutionLimits
    from harness.permissions import PermissionEngine, RuleSet
    resources = ready_resources()
    kernel = build_kernel(base_dir=tmp_path, provider=CatalogProvider(models()), model=ModelId("a"),
        resources=resources, permissions=PermissionEngine([RuleSet(default="deny" if gate == "permission" else "allow")]))
    await kernel.loop.start()
    if gate == "budget":
        kernel.loop.dispatcher.scope.budget.limits = ExecutionLimits(max_model_calls=0)
    try:
        with pytest.raises(ModelDispatchBlocked if gate == "permission" else BudgetExceeded):
            await kernel.loop.dispatcher.dispatch_inference(provider=kernel.loop.provider, request=InferenceRequest(
                model=ModelId("a"), messages=(Message.user_text("work"),), purpose="conversation"))
        assert not resources.scheduler._lanes and not resources._owned
        log = read_session(tmp_path, kernel.session.id)
        assert not any(isinstance(e.event, LocalRequestObserved) for e in log)
        assert not fold(log).open_model_intents
    finally:
        kernel.session.close()


def test_saved_queue_status_never_claims_live_admission(tmp_path):
    from harness.resident import render_status
    from harness.scheduling import LocalRequestObservation
    from harness.session import Session
    from harness.types import SessionId
    with Session(tmp_path, SessionId("queued")) as session:
        session.start()
        session.append(LocalRequestObserved(observation=LocalRequestObservation(
            request_id="waiting", alias="a", group="local", priority="interactive",
            status="queued", reason="waiting", wait_ms=0)))
    log = read_session(tmp_path, SessionId("queued"))
    assert "recorded state, live admission unknown" in render_status(log)
    assert fold(log).local_requests["waiting"].status == "queued"


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_queue_limit_rejects_invalid_configuration(value):
    with pytest.raises(ValueError):
        LocalScheduler(max_waiting=value)


async def test_tui_queue_is_visible_and_cancellable_without_losing_draft(tmp_path, monkeypatch):
    from textual.widgets import Input
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    cat, resources = models(), ready_resources()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        for chunk in text_turn("unused"):
            yield chunk

    monkeypatch.setattr("harness.provider_litellm._acomplete", complete)
    app = make_app(tmp_path, provider=CatalogProvider(cat), model=ModelId("b"), resources=resources)

    async def occupy():
        async with resources.use(cat.resolve("a"), emit=lambda e: None, priority="work"):
            entered.set()
            await release.wait()

    owner = asyncio.create_task(occupy())
    try:
        await entered.wait()
        async with app.run_test(size=(140, 45)) as pilot:
            composer = app.query_one("#prompt", Input)
            composer.value = "work"
            await pilot.press("enter")
            await until(lambda: resources.scheduler.activity("local")[1] == 1)
            composer.value = "unsent draft"
            await pilot.pause(0.1)
            assert "waiting for local group local" in screen_text(app)
            assert "Local request b: queued" in screen_text(app)
            app.action_interrupt()
            await until(lambda: app.controller.active is None and not app._interrupting)
            assert composer.value == "unsent draft" and not calls
            assert resources.scheduler.activity("local") == ("a", 0)
            release.set()
            await owner
            assert "interrupted" in screen_text(app)
    finally:
        release.set()
        await owner
