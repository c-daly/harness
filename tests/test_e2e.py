# tests/test_e2e.py
from harness.cli import build_kernel, run_once
from harness.events import HookDecided, PermissionResolved
from harness.fold import fold
from harness.hooks import Ask, Allow, Block, HookBus, ProposedToolCall, Rewrite
from harness.interaction import ScriptedResolver
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName


class SafeShell:
    spec = ToolSpec(name=ToolName("safe_shell"), description="run safely", parameters={})

    async def __call__(self, args):
        return f"ran: {args['command']}"


def guard(action):
    if not isinstance(action, ProposedToolCall):
        return Allow()
    command = action.args.get("command", "")
    if "rm -rf" in command:
        return Block(reason="destructive")
    if action.tool == "bash":
        return Rewrite(action=ProposedToolCall(
            call_id=action.call_id, tool=ToolName("safe_shell"), args=dict(action.args)))
    if "deploy" in command:
        return Ask(reason="deploys need approval")
    return Allow()


async def test_full_stack_block_rewrite_ask_and_replay(tmp_path):
    hooks = HookBus()
    hooks.register_dispatch("guard", guard, priority=10)
    provider = FakeProvider([
        tool_call_turn("trying rm", ToolName("bash"), {"command": "rm -rf /"}),
        tool_call_turn("ok, listing instead", ToolName("bash"), {"command": "ls"}),
        tool_call_turn("now deploying", ToolName("safe_shell"), {"command": "deploy prod"}),
        text_turn("all done"),
    ])
    kernel = build_kernel(
        provider=provider, base_dir=tmp_path, model=ModelId("fake"),
        hooks=hooks, resolver=ScriptedResolver([True]),
    )
    kernel.registry.register(SafeShell())
    queue = kernel.session.bus.subscribe(maxsize=1024)
    session_id = kernel.session.id

    result = await run_once(kernel, "clean up and deploy")
    assert result == "all done"

    envs = read_session(tmp_path, session_id)
    events = [e.event for e in envs]

    # 1. the block was recorded, not erased
    blocks = [e for e in events if isinstance(e, HookDecided) and e.decision["kind"] == "block"]
    assert len(blocks) == 1

    # 2. the rewrite executed the effective call
    rewrites = [e for e in events if isinstance(e, HookDecided) and e.decision["kind"] == "rewrite"]
    assert rewrites and rewrites[0].decision["tool"] == "safe_shell"

    # 3. the Ask was approved by the scripted resolver
    resolved = [e for e in events if isinstance(e, PermissionResolved)]
    assert resolved and resolved[0].allowed is True

    # 4. replay: folding the log reproduces the live transcript exactly
    state = fold(envs)
    assert state.open_intents == {}
    live = kernel.loop.history
    folded = state.messages
    assert [m.role for m in folded] == [m.role for m in live]
    assert [m.text() for m in folded] == [m.text() for m in live]

    # 5. subscribers saw exactly what the log has
    seen = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(seen) == len(envs)
    assert [e.seq for e in seen] == [e.seq for e in envs]


async def test_replay_of_recorded_session_is_a_regression_fixture(tmp_path):
    """The cheap, durable pattern: any session log can be re-folded and asserted on."""
    provider = FakeProvider([text_turn("fixture")])
    kernel = build_kernel(provider=provider, base_dir=tmp_path, model=ModelId("fake"))
    session_id = kernel.session.id
    await run_once(kernel, "make a fixture")
    state = fold(read_session(tmp_path, session_id))
    assert state.messages[-1].text() == "fixture"
