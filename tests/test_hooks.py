import asyncio

from harness.hooks import (
    Allow,
    Ask,
    Block,
    HookBus,
    Inject,
    LifecyclePoint,
    ProposedToolCall,
    Rewrite,
    decision_to_payload,
)
from harness.types import CallId, ToolName

CALL = ProposedToolCall(call_id=CallId("c1"), tool=ToolName("bash"), args={"command": "rm -rf /"})


async def test_allow_chain_passes_through():
    bus = HookBus()
    bus.register_dispatch("a", lambda a: Allow(), priority=10)
    outcome = await bus.run_dispatch(CALL)
    assert outcome.effective == CALL
    assert outcome.blocked is None
    assert [name for name, _ in outcome.decisions] == ["a"]


async def test_block_short_circuits():
    bus = HookBus()
    bus.register_dispatch("guard", lambda a: Block(reason="dangerous"), priority=10)
    bus.register_dispatch("later", lambda a: Allow(), priority=20)
    outcome = await bus.run_dispatch(CALL)
    assert outcome.blocked is not None and outcome.blocked.reason == "dangerous"
    assert outcome.effective is None
    assert len(outcome.decisions) == 1  # later never ran


async def test_rewrite_chains_forward():
    safe = ProposedToolCall(call_id=CallId("c1"), tool=ToolName("safe_bash"), args={"command": "ls"})
    seen: list[str] = []

    def rewriter(action):
        return Rewrite(action=safe)

    def witness(action):
        seen.append(action.tool)
        return Allow()

    bus = HookBus()
    bus.register_dispatch("rewriter", rewriter, priority=10)
    bus.register_dispatch("witness", witness, priority=20)
    outcome = await bus.run_dispatch(CALL)
    assert outcome.effective == safe
    assert seen == ["safe_bash"]  # witness saw the rewritten call, not the original


async def test_priority_then_registration_order():
    order: list[str] = []

    def make(name):
        def fn(action):
            order.append(name)
            return Allow()
        return fn

    bus = HookBus()
    bus.register_dispatch("second", make("second"), priority=20)
    bus.register_dispatch("first", make("first"), priority=10)
    bus.register_dispatch("third", make("third"), priority=20)
    await bus.run_dispatch(CALL)
    assert order == ["first", "second", "third"]


async def test_ask_suspends_chain():
    bus = HookBus()
    bus.register_dispatch("asker", lambda a: Ask(reason="needs approval"), priority=10)
    outcome = await bus.run_dispatch(CALL)
    assert outcome.ask is not None and outcome.ask.reason == "needs approval"
    assert outcome.effective == CALL  # effective call known; approval pending


async def test_dispatch_timeout_fails_closed():
    async def slow(action):
        await asyncio.sleep(10)
        return Allow()

    bus = HookBus(dispatch_timeout=0.05)
    bus.register_dispatch_async("slow", slow, priority=10)
    outcome = await bus.run_dispatch(CALL)
    assert outcome.blocked is not None
    assert "timed out" in outcome.blocked.reason


async def test_lifecycle_contributions_collected_and_timeouts_fail_open():
    async def slow(ctx):
        await asyncio.sleep(10)

    bus = HookBus(lifecycle_timeout=0.05)
    bus.register_lifecycle("brief", LifecyclePoint.SESSION_START,
                           lambda ctx: (Inject(text="memory brief here"),))
    bus.register_lifecycle_async("slow", LifecyclePoint.SESSION_START, slow)
    contributions, warnings = await bus.run_lifecycle(LifecyclePoint.SESSION_START, ctx={})
    assert contributions == (Inject(text="memory brief here"),)
    assert warnings and "slow" in warnings[0]


def test_decision_payload_serializes_rewrite_fully():
    payload = decision_to_payload(Rewrite(action=CALL))
    assert payload["kind"] == "rewrite"
    assert payload["tool"] == "bash"
    assert payload["args"] == {"command": "rm -rf /"}
