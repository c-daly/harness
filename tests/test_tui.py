"""Textual app: headless pilot tests. Each test builds a kernel on tmp_path."""

import asyncio
from contextlib import asynccontextmanager

import anyio
from mcp.shared.memory import create_client_server_memory_streams
from rich.markdown import Markdown
from rich.text import Text
from textual.widgets import Input, OptionList, RichLog, Static

from harness.cli import build_kernel
from harness.fold import fold
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.mcp_host import McpHost
from harness.messages import Role
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import (
    EchoProvider,
    FakeProvider,
    StreamStop,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageReport,
    tool_call_turn,
    text_turn,
)
from harness.tools import ToolSpec
from harness.tui import (
    AppBoundAsk,
    HarnessApp,
    PermissionScreen,
    ServerChecklistScreen,
    SessionPickerScreen,
    _schema_token_estimate,
)
from harness.tui_support import TuiResolver
from harness.types import CallId, ModelId, ToolName
from tests.conftest import fixture_stdio_spec, load_fixture_server

from harness.plugins import load_plugins


class EchoTool:
    spec = ToolSpec(
        name=ToolName("echo_tool"),
        description="Echo the input back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    async def __call__(self, args: dict) -> str:
        return args["text"]


class PathEchoTool:
    # A write_file-named stub: its primary arg is file_path, so grant_pattern yields a
    # non-empty workspace-scoped match that persists (unlike an empty-match allow-all).
    spec = ToolSpec(
        name=ToolName("write_file"),
        description="Write file stub",
        parameters={
            "type": "object",
            "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        },
    )

    async def __call__(self, args: dict) -> str:
        return args["file_path"]


class FatSchemaTool:
    # Parameters schema padded to ~40KB so its estimated token cost clears the
    # 10%-of-context threshold for a 16384-token constrained model.
    spec = ToolSpec(
        name=ToolName("fat_tool"),
        description="Tool with an oversized parameter schema",
        parameters={"type": "object", "description": "x" * 40_000},
    )

    async def __call__(self, args: dict) -> str:
        return "ok"


def make_app(tmp_path, catalog_path=None, plugins=None, **kernel_kwargs) -> HarnessApp:
    """Build a HarnessApp for tests.

    Special kwargs (consumed here, not forwarded to build_kernel):
      engine: PermissionEngine -- when given, wires up AppBoundAsk + TuiResolver.
      catalog_path: Path -- when given, forwarded to HarnessApp for /model.
      plugins: LoadedPlugins -- when given, forwarded to build_kernel.
      native_tools/workspace_root/routing_rules -- forwarded to BOTH build_kernel
      (initial kernel) and HarnessApp (so a /clear rebuild reuses them too,
      mirroring cli.py's run_tui(..., native_tools=True, workspace_root=..., ...)).
    """
    engine = kernel_kwargs.pop("engine", None)
    ask: AppBoundAsk | None = None
    resolver = None
    if engine is not None:
        ask = AppBoundAsk()
        resolver = TuiResolver(ask=ask, engine=engine)
    native_tools = kernel_kwargs.pop("native_tools", False)
    workspace_root = kernel_kwargs.pop("workspace_root", None)
    routing_rules = kernel_kwargs.pop("routing_rules", None)
    build_kwargs: dict = dict(
        provider=kernel_kwargs.pop("provider", EchoProvider()),
        base_dir=tmp_path,
        model=kernel_kwargs.pop("model", ModelId("echo")),
    )
    if resolver is not None:
        build_kwargs["resolver"] = resolver
    if engine is not None and "permissions" not in kernel_kwargs:
        build_kwargs["permissions"] = engine
    if plugins is not None:
        build_kwargs["plugins"] = plugins
    if native_tools:
        build_kwargs["native_tools"] = True
    if workspace_root is not None:
        build_kwargs["workspace_root"] = workspace_root
    if routing_rules is not None:
        build_kwargs["routing_rules"] = routing_rules
    build_kwargs.update(kernel_kwargs)
    kernel = build_kernel(**build_kwargs)
    return HarnessApp(
        kernel,
        catalog_path=catalog_path,
        ask=ask,
        native_tools=native_tools,
        workspace_root=workspace_root,
        routing_rules=routing_rules,
    )


async def test_submit_renders_user_line_and_reply(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hi there", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "hi there" in lines
        assert "echo: hi there" in lines
        assert app.query_one(Input).value == ""


async def test_session_started_exactly_once_and_teardown(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.2)
    # run_test exits the app; on_unmount fires _finish() -> loop.end() -> session_ended
    # session.close() is NOT called by the app (that is run_tui job), call it here
    kernel = app.kernel
    kernel.session.close()
    envelopes = read_session(tmp_path, kernel.session.id)
    types = [e.event.type for e in envelopes]
    assert types[0] == "session_started"
    assert types.count("session_started") == 1
    assert "session_ended" in types


class GatedProvider:
    """Deterministic in-flight turn: parks until the test releases the gate."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(self, *, model, messages, tools):
        await self.release.wait()
        yield TextDelta(text="gated done")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_second_submit_while_turn_running_is_rejected(tmp_path):
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.1)  # turn parked at the gate
        await pilot.press(*"two", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
        provider.release.set()  # let the first turn finish
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "gated done" in lines
    # exactly ONE user_message in the log
    app.kernel.session.close()
    envelopes = read_session(tmp_path, app.kernel.session.id)
    assert [e.event.type for e in envelopes].count("user_message") == 1


async def test_turn_failure_renders_and_loop_survives(tmp_path):
    # engine denying model:* (tests/test_permissions.py construction); every
    # turn raises ModelDispatchBlocked, so the loop must survive repeat failures.
    engine = PermissionEngine(
        [RuleSet(rules=[PermissionRule(action="deny", tool="model:*")], default="allow")]
    )
    app = make_app(tmp_path, permissions=engine)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "turn failed" in lines
        await pilot.press(*"again", "enter")  # loop still accepts turns
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert lines.count("turn failed") == 2


async def test_streaming_tokens_appear_in_live_tail_then_finalize(tmp_path):
    provider = FakeProvider(
        [
            [
                TextDelta(text="str"),
                TextDelta(text="eam"),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="end_turn"),
            ]
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "stream" in lines  # finalized
        assert app.query_one("#live", Static).content == ""  # tail cleared


class _FlakyStreamProvider:
    """Attempt 1 yields partial then raises Overloaded; attempt 2 yields the full reply."""

    def __init__(self):
        self.attempts = 0

    async def complete(self, *, model, messages, tools=()):
        from harness.errors import Overloaded

        self.attempts += 1
        if self.attempts < 2:
            yield TextDelta(text="par")
            raise Overloaded("busy")
        for chunk in [
            TextDelta(text="full"),
            TextDelta(text=" reply"),
            UsageReport(usage=Usage()),
            StreamStop(stop_reason="end_turn"),
        ]:
            yield chunk


async def test_retry_resets_live_tail_no_duplication(tmp_path):
    provider = _FlakyStreamProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    app.kernel.loop.dispatcher.retry_delays = (0.0,)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.4)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "full reply" in lines
        assert "parfull" not in lines  # the partial didnt bleed into the final
        assert "retrying" in lines  # the reset was announced


async def test_thinking_only_stream_clears_live_tail(tmp_path):
    provider = FakeProvider(
        [
            [
                ThinkingDelta(text="pondering"),
                TextDelta(text="done thinking"),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="end_turn"),
            ],
            [
                ThinkingDelta(text="only thoughts"),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="end_turn"),
            ],
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.3)
        assert str(app.query_one("#live", Static).content) == ""
        await pilot.press(*"two", "enter")  # thinking-only turn
        await pilot.pause(0.3)
        assert str(app.query_one("#live", Static).content) == ""  # not stuck


class ThinkingGatedProvider:
    """Yields a thought, then parks; yields the answer, then parks again --
    gives tests a deterministic window to inspect the live view mid-stream."""

    def __init__(self) -> None:
        self.release_after_thought = asyncio.Event()
        self.release_after_text = asyncio.Event()

    async def complete(self, *, model, messages, tools=()):
        yield ThinkingDelta(text="pondering")
        await self.release_after_thought.wait()
        yield TextDelta(text="answer")
        await self.release_after_text.wait()
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_thoughts_collapse_mode_is_default(tmp_path):
    provider = ThinkingGatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:think"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.1)
        # while thinking streams in, the live view shows the raw thought text
        assert "pondering" in str(app.query_one("#live", Static).content)
        provider.release_after_thought.set()
        await pilot.pause(0.1)
        # once the answer starts, collapse mode replaces the thought with a summary
        live_text = str(app.query_one("#live", Static).content)
        assert "(thought for" in live_text
        assert "pondering" not in live_text
        assert "answer" in live_text
        provider.release_after_text.set()
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "answer" in lines
        assert "pondering" not in lines  # raw thought never lands in the transcript
        assert "pondering" not in app.kernel.loop.history[-1].text()  # (d): history stays clean


async def test_thoughts_full_mode_retains_thought_above_answer(tmp_path):
    provider = ThinkingGatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:think"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/thoughts full", "enter")
        await pilot.pause(0.1)
        await pilot.press(*"go", "enter")
        await pilot.pause(0.1)
        assert "pondering" in str(app.query_one("#live", Static).content)
        provider.release_after_thought.set()
        await pilot.pause(0.1)
        # full mode keeps streaming the raw thought alongside the answer -- no collapse
        live_text = str(app.query_one("#live", Static).content)
        assert "pondering" in live_text
        assert "answer" in live_text
        provider.release_after_text.set()
        await pilot.pause(0.2)
        lines_list = [str(line) for line in app.query_one(RichLog).lines]
        thought_idx = next(i for i, l in enumerate(lines_list) if "pondering" in l)
        answer_idx = next(
            i for i, l in enumerate(lines_list) if "answer" in l and "pondering" not in l
        )
        assert thought_idx < answer_idx  # thought retained above the answer
        assert "pondering" not in app.kernel.loop.history[-1].text()  # (d): history stays clean


async def test_thoughts_off_mode_keeps_pre_existing_suffix_only(tmp_path):
    provider = ThinkingGatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:think"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/thoughts off", "enter")
        await pilot.pause(0.1)
        await pilot.press(*"go", "enter")
        await pilot.pause(0.1)
        live_text = str(app.query_one("#live", Static).content)
        assert "pondering" not in live_text
        assert "(thinking" in live_text  # pre-existing suffix behavior preserved
        provider.release_after_thought.set()
        await pilot.pause(0.1)
        provider.release_after_text.set()
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "pondering" not in lines
        assert "(thought for" not in lines
        assert "pondering" not in app.kernel.loop.history[-1].text()  # (d): history stays clean


async def test_thoughts_command_rejects_unknown_mode(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/thoughts bogus", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "collapse" in lines and "full" in lines and "off" in lines


# --- /markdown (Task 7) ---


async def test_render_reply_markdown_by_default_and_plain_after_toggle_off(tmp_path):
    """(a): _render_reply returns Markdown by default, Text after /markdown off."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert isinstance(app._render_reply("# hi"), Markdown)
        await pilot.click("#prompt")
        await pilot.press(*"/markdown off", "enter")
        await pilot.pause(0.1)
        assert isinstance(app._render_reply("# hi"), Text)


async def test_completed_reply_with_code_block_and_table_uses_markdown_seam(tmp_path):
    """(b) e2e: a fenced code block + table reply completes without error and
    the transcript write for the reply goes through the _render_reply seam."""
    body = "```python\nprint(1)\n```\n\n| a | b |\n| - | - |\n| 1 | 2 |"
    provider = FakeProvider([text_turn(body)])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:md"))
    seam_calls = []
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        real_render_reply = app._render_reply

        def spy(text):
            seam_calls.append(text)
            return real_render_reply(text)

        app._render_reply = spy
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "turn failed" not in lines
    assert seam_calls == [body]
    assert isinstance(real_render_reply(body), Markdown)


async def test_markdown_command_rejects_unknown_arg(tmp_path):
    """(c): /markdown bogus reports the valid values."""
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/markdown bogus", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "unknown markdown mode" in lines
        assert "on" in lines and "off" in lines


async def test_markdown_command_no_arg_reports_current_mode(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/markdown", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "markdown mode: on" in lines


async def test_reply_render_seam_not_used_for_thought_lines(tmp_path):
    """(d): full-mode thought lines stay on the _plain seam -- _render_reply
    is invoked ONLY for the completed assistant reply text."""
    provider = ThinkingGatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:think"))
    seam_calls = []
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        real_render_reply = app._render_reply

        def spy(text):
            seam_calls.append(text)
            return real_render_reply(text)

        app._render_reply = spy
        await pilot.click("#prompt")
        await pilot.press(*"/thoughts full", "enter")
        await pilot.pause(0.1)
        await pilot.press(*"go", "enter")
        await pilot.pause(0.1)
        provider.release_after_thought.set()
        await pilot.pause(0.1)
        provider.release_after_text.set()
        await pilot.pause(0.2)
    assert seam_calls == ["answer"]  # only the completed reply went through the seam


async def test_reply_render_seam_not_used_on_turn_failure(tmp_path):
    """(d): error lines stay on the _plain seam via say(), never _render_reply."""
    engine = PermissionEngine(
        [RuleSet(rules=[PermissionRule(action="deny", tool="model:*")], default="allow")]
    )
    app = make_app(tmp_path, permissions=engine)
    seam_calls = []
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        real_render_reply = app._render_reply

        def spy(text):
            seam_calls.append(text)
            return real_render_reply(text)

        app._render_reply = spy
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "turn failed" in lines
    assert seam_calls == []


async def test_permission_modal_allow_completes_turn(tmp_path):
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = FakeProvider(
        [
            tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up; dispatch parked
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("y")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "done" in lines
    app.kernel.session.close()
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert "permission_requested" in events
    assert "permission_resolved" in events


async def test_permission_modal_deny_blocks_tool(tmp_path):
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = FakeProvider(
        [
            tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("n")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "denied by user" in lines


async def test_permission_modal_always_persists_grant(tmp_path):
    # Uses write_file so grant_pattern yields a non-empty workspace-scoped path match:
    # only constrained grants persist (C1 -- an empty-match allow-all rule is never
    # written to grants.toml from a single keystroke).
    grants_path = tmp_path / "grants.toml"
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="write_file")],
                default="allow",
            )
        ],
        grants_path=grants_path,
    )
    provider = FakeProvider(
        [
            tool_call_turn(
                "calling", ToolName("write_file"), {"file_path": "/w/proj/a.txt", "content": "hi"}
            ),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(PathEchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("a")
        await pilot.pause(0.5)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "done" in lines
    # grant was persisted (constrained to the workspace path glob)
    assert grants_path.exists()
    assert "write_file" in grants_path.read_text()


async def test_escape_interrupts_turn_and_loop_survives(tmp_path):
    provider = GatedProvider()  # parks until released -- never released here
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"slow", "enter")
        await pilot.pause(0.2)  # turn parked in the provider
        await pilot.press("escape")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "interrupted" in lines.lower()
        # loop survives: swap provider and run another turn
        app.kernel.loop.provider = EchoProvider()
        await pilot.click("#prompt")
        await pilot.press(*"again", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "echo: again" in lines
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("user_interrupt") == 1


async def test_escape_with_no_turn_running_is_a_noop(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)
        events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
        assert "user_interrupt" not in events


async def test_escape_on_permission_modal_denies_it(tmp_path):
    # The app-level priority Esc binding preempts the modal own escape binding;
    # action_interrupt must DELEGATE to the modal -- this test pins that behaviour.
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = FakeProvider(
        [
            tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, PermissionScreen)  # modal gone
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "denied by user" in lines
        events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
        assert "user_interrupt" not in events  # the TURN was not interrupted


class ParkingProvider:
    """First complete() call yields the given chunks; subsequent calls park on an
    Event (like GatedProvider) -- used to drive a turn into a second, blocked model
    dispatch so Esc can interrupt a genuinely-running turn."""

    def __init__(self, first_chunks):
        self.first_chunks = first_chunks
        self.calls = 0
        self.release = asyncio.Event()

    async def complete(self, *, model, messages, tools=()):
        self.calls += 1
        if self.calls == 1:
            for chunk in self.first_chunks:
                yield chunk
            return
        await self.release.wait()  # park: never released in these tests
        yield TextDelta(text="unreached")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_escape_after_modal_close_interrupts_running_turn(tmp_path):
    # Esc #1 denies the modal (turn keeps running); the turn proceeds to model call
    # #2, which parks; Esc #2 interrupts the genuinely-running turn. Exactly one
    # user_interrupt is recorded -- the modal-deny Esc must NOT count as an interrupt.
    engine = PermissionEngine(
        [
            RuleSet(
                rules=[PermissionRule(action="ask", tool="echo_tool")],
                default="allow",
            )
        ]
    )
    provider = ParkingProvider(tool_call_turn("calling", ToolName("echo_tool"), {"text": "hi"}))
    app = make_app(tmp_path, provider=provider, model=ModelId("parking"), engine=engine)
    app.kernel.registry.register(EchoTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"run it", "enter")
        await pilot.pause(0.3)  # modal up after tool call in turn #1
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("escape")  # Esc #1: deny the modal
        await pilot.pause(0.3)
        assert not isinstance(app.screen, PermissionScreen)  # modal gone
        await pilot.pause(0.3)  # turn proceeds to model call #2 -> parks
        await pilot.press("escape")  # Esc #2: interrupt the running turn
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "denied by user" in lines
        assert "interrupted" in lines
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("user_interrupt") == 1


class PartialThenParkProvider:
    """Yields some streamed text, then parks on an Event -- so an interrupt has a
    non-empty stream buffer to preserve."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(self, *, model, messages, tools=()):
        yield TextDelta(text="partial output")
        await self.release.wait()  # park: never released here
        yield TextDelta(text=" tail")
        yield UsageReport(usage=Usage())
        yield StreamStop(stop_reason="end_turn")


async def test_escape_preserves_partial_streamed_output(tmp_path):
    provider = PartialThenParkProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("partial"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)  # partial streamed, turn parked
        await pilot.press("escape")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "~ partial output" in lines  # the partial was preserved with the ~ prefix
        assert "interrupted" in lines
        assert app.query_one("#live", Static).content == ""  # live tail cleared
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("user_interrupt") == 1


MODELS_TOML_TWO_ALIASES = (
    "[models.alias-a]\n"
    "route = 'local/model-a'\n"
    "input_cost_per_token = 0.0\n"
    "output_cost_per_token = 0.0\n"
    "\n"
    "[models.alias-b]\n"
    "route = 'local/model-b'\n"
    "input_cost_per_token = 0.0\n"
    "output_cost_per_token = 0.0\n"
)


async def test_slash_help_and_tools_and_unknown(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/help", "enter")
        await pilot.press(*"/tools", "enter")
        await pilot.press(*"/nope", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "/model" in lines  # help lists commands
        assert "dispatch_agent" in lines  # tools lists the builtin
        assert "unknown command" in lines


async def test_slash_model_switches_via_catalog(tmp_path):
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_TWO_ALIASES)
    app = make_app(tmp_path, catalog_path=catalog_file)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model", "enter")  # no arg: list aliases
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "alias-a" in lines and "alias-b" in lines
        await pilot.press(*"/model alias-b", "enter")
        await pilot.pause(0.3)  # switch runs in a worker now (lazy litellm import)
        # the ALIAS now flows through dispatch; the CatalogProvider resolves the
        # route per call. The route still shows in the confirmation line.
        from harness.provider_litellm import CatalogProvider

        assert str(app.kernel.loop.model) == "alias-b"
        assert app.kernel.loop.model_pinned is True
        assert isinstance(app.kernel.loop.provider, CatalogProvider)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "local/model-b" in lines  # route surfaced in 'model → alias-b (route)'
        await pilot.press(*"/model nope", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "unknown alias" in lines


async def test_slash_model_without_catalog_says_so(tmp_path):
    app = make_app(tmp_path)  # no catalog_path
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "no catalog" in lines


async def test_slash_quit_exits_cleanly(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/quit", "enter")
        await pilot.pause(0.2)
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert "session_ended" in events
    assert [e for e in events].count("session_ended") == 1


async def test_slash_quit_during_running_turn_is_clean(tmp_path):
    provider = GatedProvider()  # never released
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"stuck", "enter")
        await pilot.pause(0.2)  # turn parked
        await pilot.press(*"/quit", "enter")
        await pilot.pause(0.3)
    events = [e.event.type for e in read_session(tmp_path, app.kernel.session.id)]
    assert events.count("session_ended") == 1  # no crash, clean single end


async def test_stats_line_updates_after_a_turn(tmp_path):
    provider = FakeProvider(
        [
            [
                TextDelta(text="hi"),
                UsageReport(usage=Usage(input_tokens=7, output_tokens=3)),
                StreamStop(stop_reason="end_turn"),
            ]
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        app.refresh_stats()  # poke instead of waiting 1s
        await pilot.pause(0.1)
        stats = str(app.query_one("#stats", Static).content)
        assert "in 7" in stats and "out 3" in stats
        assert "tools 0" in stats


async def test_tui_pipes_mcp_child_stderr_to_file(tmp_path):
    kernel = build_kernel(
        provider=EchoProvider(),
        base_dir=tmp_path,
        model=ModelId("echo"),
        mcp=[fixture_stdio_spec()],
    )
    app = HarnessApp(kernel)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)  # checklist mounts
        await pilot.press("enter")  # accept defaults -- default_enabled=True
        await pilot.pause(0.5)  # mcp start + session driver
        await pilot.click("#prompt")
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.3)
    try:
        errlog_path = tmp_path / "sessions" / str(kernel.session.id) / "mcp-stderr.log"
        assert errlog_path.exists()
        assert "ListToolsRequest" in errlog_path.read_text()
    finally:
        # close what run_tui would close in its finally (run_test does not run run_tui)
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await kernel.mcp.stop()
        kernel.session.close()


async def test_hostile_tool_name_renders_neutralized(tmp_path):
    """Transcript rendering strips control bytes and never interprets markup
    from tool-controlled strings (the \u2699 line renders str(tool) via _plain)."""
    hostile = "ev\x1b[31mil\x07[bold red]X[/]\rZZ"

    class HostileTool:
        spec = ToolSpec(name=ToolName(hostile), description="", parameters={})

        async def __call__(self, args):
            return "ok"

    provider = FakeProvider(
        [
            tool_call_turn("", ToolName(hostile), {}),
            text_turn("done"),
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    app.kernel.registry.register(HostileTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        rendered = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "\x1b" not in rendered and "\x07" not in rendered and "\r" not in rendered
        assert "done" in rendered


async def test_malformed_stream_fails_turn_and_loop_survives(tmp_path):
    """A provider yielding garbage tool-call JSON raises MalformedStreamError
    (non-retryable): the TUI renders the failure, repairs, and the next turn works."""
    provider = FakeProvider(
        [
            [
                ToolCallDelta(
                    index=0, call_id=CallId("x1"), tool=ToolName("echo_tool"), args_json="{not json"
                ),
                UsageReport(usage=Usage()),
                StreamStop(stop_reason="tool_use"),
            ]
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "turn failed" in lines
        app.kernel.loop.provider = EchoProvider()
        await pilot.click("#prompt")
        await pilot.press(*"again", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "echo: again" in lines


# ---------------------------------------------------------------------------
# Task 7: TUI plugin commands
# ---------------------------------------------------------------------------

MINIMAL_PLUGIN_MANIFEST = """
[plugin]
name = "echo-plugin"
version = "0.1.0"
description = "Plugin for TUI command tests"
"""

_ECHO_COMMAND_CONTENT = (
    "---\nname: echo-args\ndescription: Echo the arguments\n---\nPlease echo: $ARGUMENTS"
)


def _make_echo_plugin(tmp_path):
    """Create a plugin with commands/echo-args.md in tmp_path; return LoadedPlugins."""
    plugin_dir = tmp_path / "echo-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(MINIMAL_PLUGIN_MANIFEST)
    commands_dir = plugin_dir / "commands"
    commands_dir.mkdir()
    (commands_dir / "echo-args.md").write_text(_ECHO_COMMAND_CONTENT)
    return load_plugins([tmp_path])


async def test_plugin_command_submits_its_body_as_a_turn(tmp_path):
    """Invoking /echo-args expands $ARGUMENTS and submits the body as a normal turn."""
    loaded = _make_echo_plugin(tmp_path / "plugins")
    app = make_app(tmp_path, plugins=loaded)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/echo-args hello world", "enter")
        await pilot.pause(0.3)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "Please echo: hello world" in lines  # the expanded body was submitted
        assert "echo: Please echo: hello world" in lines  # EchoProvider replied


async def test_plugin_command_listed_in_help_and_unknown_still_unknown(tmp_path):
    """Plugin commands appear in /help output; unknown commands remain unknown."""
    loaded = _make_echo_plugin(tmp_path / "plugins")
    app = make_app(tmp_path, plugins=loaded)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/help", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "/echo-args" in lines
        await pilot.press(*"/nope", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "unknown command" in lines


async def test_plugin_command_respects_turn_guard(tmp_path):
    """Invoking a plugin command mid-turn renders the \"already running\" message."""
    loaded = _make_echo_plugin(tmp_path / "plugins")
    provider = GatedProvider()  # parks until released -- never released here
    app = make_app(tmp_path, plugins=loaded, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")  # start a turn; parks at gate
        await pilot.pause(0.1)
        await pilot.press(*"/echo-args hi", "enter")  # plugin command mid-turn
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines


async def test_slash_model_upgrade_wires_claude_code_backend(tmp_path):
    """Upgrading out of echo mode via /model must wire the claude-code backend:
    a catalog may hold backend = "claude-code" entries, and a bare
    CatalogProvider fails them with "backend ... not wired" (the TUI bug hit
    live on 2026-08-15)."""
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_TWO_ALIASES)
    app = make_app(tmp_path, catalog_path=catalog_file)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model alias-b", "enter")
        await pilot.pause(0.3)
        from harness.provider_litellm import CatalogProvider

        provider = app.kernel.loop.provider
        assert isinstance(provider, CatalogProvider)
        assert provider.claude_code is not None  # claude-code entries dispatchable


async def test_slash_model_upgrade_retargets_subagent_runner(tmp_path):
    """PR #2 review: /model upgrading out of echo mode must retarget the
    SubagentRunner too — it captured the build-time provider, so a swap that
    touches only loop.provider leaves dispatch_agent and mixture experts on
    the echo provider, with an unresolvable echo default_model."""
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_TWO_ALIASES)
    app = make_app(tmp_path, catalog_path=catalog_file)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model alias-b", "enter")
        await pilot.pause(0.3)
        runner = app.kernel.runner
        assert runner.provider is app.kernel.loop.provider
        assert app.kernel.provider is app.kernel.loop.provider
        assert runner.default_model == ModelId("alias-b")
        assert runner.pricing_for is app.kernel.loop.pricing_for


async def test_slash_model_refreshes_catalog_snapshot_in_place(tmp_path):
    """PR #2 review: with a CatalogProvider already live, /model must swap the
    provider's catalog snapshot — otherwise a models.toml edit made mid-session
    validates against the fresh catalog but dispatches against the startup one,
    so a new alias falls through as a literal LiteLLM route. The provider
    INSTANCE must survive (the subagent runner shares it)."""
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_TWO_ALIASES)
    app = make_app(tmp_path, catalog_path=catalog_file)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model alias-b", "enter")
        await pilot.pause(0.3)
        provider = app.kernel.loop.provider  # CatalogProvider after upgrade
        catalog_file.write_text(
            MODELS_TOML_TWO_ALIASES
            + "\n[models.alias-new]\n"
            + "route = 'local/model-new'\n"
            + "input_cost_per_token = 0.0\n"
            + "output_cost_per_token = 0.0\n"
        )
        await pilot.press(*"/model alias-new", "enter")
        await pilot.pause(0.3)
        assert app.kernel.loop.provider is provider  # same shared instance
        assert provider.catalog.resolve("alias-new").route == "local/model-new"
        assert app.kernel.loop.model == ModelId("alias-new")
        assert app.kernel.runner.default_model == ModelId("alias-new")


# ---------------------------------------------------------------------------
# Task 2: session-start MCP server checklist
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _checklist_memory_transport(fastmcp):
    """Same in-memory stream transport as test_mcp_host.py's memory_transport,
    duplicated here per the existing per-file convention (test_tools_allow.py
    duplicates it too as well) rather than importing across test modules."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        lowlevel = fastmcp._mcp_server
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: lowlevel.run(
                    server_read,
                    server_write,
                    lowlevel.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                yield (client_read, client_write)
            finally:
                tg.cancel_scope.cancel()


class _CountingTransportFactory:
    """Counts invocations so a test can prove a server's transport was (or was
    never) constructed -- the lossless-OFF half of the checklist invariant."""

    def __init__(self, fastmcp):
        self._fastmcp = fastmcp
        self.calls = 0

    def __call__(self, spec):
        self.calls += 1
        return _checklist_memory_transport(self._fastmcp)


def _build_checklist_kernel(tmp_path):
    """Kernel with two fake MCP servers wired over in-memory streams:
    'on-server' (default_enabled True, tools_allow restricts it to 'add')
    and 'off-server' (default_enabled False). Returns
    (kernel, on_counter, off_counter)."""
    on_spec = McpServerSpec(
        name="on-server",
        transport="stdio",
        command="unused",
        tools_allow=("add",),
        default_enabled=True,
    )
    off_spec = McpServerSpec(
        name="off-server",
        transport="stdio",
        command="unused",
        default_enabled=False,
    )
    on_counter = _CountingTransportFactory(load_fixture_server())
    off_counter = _CountingTransportFactory(load_fixture_server())
    counters = {"on-server": on_counter, "off-server": off_counter}

    def transport_factory(spec):
        return counters[spec.name](spec)

    kernel = build_kernel(provider=EchoProvider(), base_dir=tmp_path, model=ModelId("echo"))
    kernel.mcp = McpHost(
        [on_spec, off_spec],
        registry=kernel.registry,
        hooks=kernel.hooks,
        session=kernel.session,
        transport_factory=transport_factory,
    )
    return kernel, on_counter, off_counter


async def test_checklist_lists_servers_with_defaults(tmp_path):
    kernel, on_counter, off_counter = _build_checklist_kernel(tmp_path)
    app = HarnessApp(kernel)
    try:
        async with app.run_test() as pilot:
            await pilot.pause(0.3)
            screen = app.screen
            assert isinstance(screen, ServerChecklistScreen)
            assert screen.query_one("#chk-on-server").value is True
            assert screen.query_one("#chk-off-server").value is False
            await pilot.press("enter")
            await pilot.pause(0.3)
    finally:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await kernel.mcp.stop()
        kernel.session.close()


async def test_unchecked_server_never_starts(tmp_path):
    kernel, on_counter, off_counter = _build_checklist_kernel(tmp_path)
    app = HarnessApp(kernel)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("enter")  # accept defaults: off-server stays unchecked
        await pilot.pause(0.3)
    try:
        assert off_counter.calls == 0  # transport factory never invoked
        names = {str(s.name) for s in kernel.registry.specs()}
        assert not any(n.startswith("mcp__off-server__") for n in names)
        assert "off-server" not in kernel.mcp.connections
    finally:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await kernel.mcp.stop()
        kernel.session.close()


async def test_checked_server_full_capability(tmp_path):
    kernel, on_counter, off_counter = _build_checklist_kernel(tmp_path)
    app = HarnessApp(kernel)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("enter")  # accept defaults: on-server stays checked
        await pilot.pause(0.3)
    try:
        assert on_counter.calls == 1
        names = {str(s.name) for s in kernel.registry.specs()}
        assert "mcp__on-server__add" in names
        assert "mcp__on-server__fail" not in names  # tools_allow restricts to 'add'
        result = await kernel.registry.get(ToolName("mcp__on-server__add"))({"a": 2, "b": 2})
        assert result == "4"
    finally:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await kernel.mcp.stop()
        kernel.session.close()


async def test_clear_restarts_previously_enabled_mcp_servers(tmp_path):
    """/clear must leave MCP tools functional: the server(s) enabled at the
    FIRST mount's checklist restart (as fresh connections -- v1 accepts a
    process restart) on the rebuilt kernel, without re-showing the checklist,
    and a server that was never enabled stays never-started."""
    kernel, on_counter, off_counter = _build_checklist_kernel(tmp_path)
    app = HarnessApp(kernel)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, ServerChecklistScreen)
        await pilot.press("enter")  # accept defaults: on-server stays checked
        await pilot.pause(0.3)
        assert on_counter.calls == 1
        result = await app.kernel.registry.get(ToolName("mcp__on-server__add"))({"a": 2, "b": 2})
        assert result == "4"

        await pilot.click("#prompt")
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.3)

        assert not isinstance(app.screen, ServerChecklistScreen)  # never re-shown
        assert on_counter.calls == 2  # restarted with the same enabled set
        assert off_counter.calls == 0  # never-enabled server still never starts
        names = {str(s.name) for s in app.kernel.registry.specs()}
        assert "mcp__on-server__add" in names
        result = await app.kernel.registry.get(ToolName("mcp__on-server__add"))({"a": 3, "b": 4})
        assert result == "7"
        # the errlog is re-pointed at the NEW session's directory, not left
        # pointing at the (now torn-down) old one
        assert str(app.kernel.session.id) in app._mcp_errlog.name
    try:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        await app.kernel.mcp.stop()
    finally:
        app.kernel.session.close()


def test_schema_token_estimate_sums_json_length_over_four():
    specs = (
        ToolSpec(
            name=ToolName("t1"),
            description="one",
            parameters={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name=ToolName("t2"),
            description="two tool",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        ),
    )
    # json.dumps({"name": "t1", "description": "one",
    #             "parameters": {"type": "object", "properties": {}}}) is 88 chars;
    # the t2 spec's JSON is 116 chars -- (88 + 116) // 4 == 51.
    assert _schema_token_estimate(specs) == 51


MODELS_TOML_CONTEXT_TOAST = (
    "[models.tiny-local]\n"
    "route = 'local/tiny'\n"
    "tags = ['local']\n"
    "max_input_tokens = 16384\n"
    "input_cost_per_token = 0.0\n"
    "output_cost_per_token = 0.0\n"
    "\n"
    "[models.big-cloud]\n"
    "route = 'openai/big-cloud'\n"
    "input_cost_per_token = 0.0\n"
    "output_cost_per_token = 0.0\n"
)


async def test_context_toast_fires_for_constrained_model_not_for_unconstrained(tmp_path):
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_CONTEXT_TOAST)
    app = make_app(tmp_path, catalog_path=catalog_file)
    app.kernel.registry.register(FatSchemaTool())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model tiny-local", "enter")
        await pilot.pause(0.4)  # switch runs in a worker (lazy litellm import)
        warnings = [n for n in app._notifications if n.severity == "warning"]
        assert any(
            w.message.startswith("tiny-local: ")
            and "tokens of schemas" in w.message
            and "context)" in w.message
            and w.message.endswith("/tools to review")
            for w in warnings
        )

        app.clear_notifications()
        await pilot.press(*"/model big-cloud", "enter")
        await pilot.pause(0.4)
        assert not app._notifications  # unconstrained model: no toast at all


# --- status bar ---


async def test_statusbar_shows_model_and_tool_count_after_turn(tmp_path):
    provider = FakeProvider(
        [
            [
                TextDelta(text="hi"),
                UsageReport(usage=Usage(input_tokens=7, output_tokens=3)),
                StreamStop(stop_reason="end_turn"),
            ]
        ]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        app.refresh_stats()  # poke instead of waiting 1s
        await pilot.pause(0.1)
        bar = str(app.query_one("#statusbar", Static).content)
        assert "fake:echo" in bar
        assert "tools 0" in bar


async def test_statusbar_shows_ctx_and_cost_for_catalog_model(tmp_path):
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_CONTEXT_TOAST)
    app = make_app(tmp_path, catalog_path=catalog_file)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model tiny-local", "enter")
        await pilot.pause(0.4)  # switch runs in a worker (lazy litellm import)
        bar = str(app.query_one("#statusbar", Static).content)
        assert "ctx" in bar and "%" in bar
        assert "$" in bar


async def test_statusbar_omits_ctx_and_cost_in_echo_mode(tmp_path):
    app = make_app(tmp_path)  # no catalog_path -- not a catalog alias
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"go", "enter")
        await pilot.pause(0.3)
        app.refresh_stats()
        await pilot.pause(0.1)
        bar = str(app.query_one("#statusbar", Static).content)
        assert "ctx" not in bar
        assert "$" not in bar


async def test_statusbar_shows_new_alias_after_model_switch_without_a_turn(tmp_path):
    catalog_file = tmp_path / "models.toml"
    catalog_file.write_text(MODELS_TOML_TWO_ALIASES)
    app = make_app(tmp_path, catalog_path=catalog_file)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/model alias-b", "enter")
        await pilot.pause(0.3)  # switch runs in a worker (lazy litellm import)
        bar = str(app.query_one("#statusbar", Static).content)
        assert "alias-b" in bar


# --- /clear ---


async def test_clear_rebuilds_kernel_fresh_session_same_wiring(tmp_path):
    engine = PermissionEngine(
        [RuleSet(rules=[PermissionRule(action="deny", tool="nonexistent_tool")], default="allow")]
    )
    provider = FakeProvider([text_turn("hi there")])
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"), engine=engine)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hello", "enter")
        await pilot.pause(0.2)
        old_session_id = app.kernel.session.id
        old_provider = app.kernel.provider
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.2)
        assert app.kernel.session.id != old_session_id
        assert app.kernel.loop.history == []
        assert app.kernel.provider is old_provider
        assert app.kernel.loop.provider is old_provider
        assert app.kernel.runner.resolver.engine is engine
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "cleared" in lines


async def test_clear_refused_while_turn_running(tmp_path):
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"stuck", "enter")
        await pilot.pause(0.1)  # turn parked at the gate
        old_session_id = app.kernel.session.id
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
        assert app.kernel.session.id == old_session_id
        provider.release.set()  # let the first turn finish before teardown
        await pilot.pause(0.3)


async def test_turn_after_clear_completes_normally(tmp_path):
    app = make_app(tmp_path)  # default EchoProvider
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"before", "enter")
        await pilot.pause(0.2)
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.2)
        new_session_id = app.kernel.session.id
        await pilot.press(*"after", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "echo: after" in lines
        # the post-/clear turn is the ONLY thing in history -- the pre-/clear
        # "before" turn did not leak across the rebuild
        assert len(app.kernel.loop.history) == 2  # user + assistant
        assert app.kernel.session.id == new_session_id

    # and it's not just in-memory: the reply is durably logged under the NEW
    # session id, not the old one
    envelopes = read_session(tmp_path, new_session_id)
    folded = fold(envelopes)
    texts = [m.text() for m in folded.messages]
    assert any("echo: after" in t for t in texts)
    assert not any("before" in t for t in texts)


async def test_clear_reemits_tags_into_new_session_log(tmp_path):
    """_session_driver emits one CustomEvent(namespace="harness", name="tag")
    per --tag at mount; a /clear rebuild must reproduce that for the new
    session's log too, not just carry `tags` forward silently in memory."""
    app = make_app(tmp_path, tags=["exp-a", "exp-b"])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.2)
        new_session_id = app.kernel.session.id

    envelopes = read_session(tmp_path, new_session_id)
    tags_seen = [
        e.event.data.get("tag")
        for e in envelopes
        if e.event.type == "custom" and e.event.namespace == "harness" and e.event.name == "tag"
    ]
    assert tags_seen == ["exp-a", "exp-b"]


_SPY_SUBSCRIBER_PLUGIN_MANIFEST = (
    MINIMAL_PLUGIN_MANIFEST + '\n[[subscribers]]\nname = "spy"\nmodule = "hooks.py"\nfunction = "spy"\n'
)
_SPY_SUBSCRIBER_SRC = (
    "received = []\n\n\nasync def spy(envelope):\n    received.append(envelope.event.type)\n"
)


def _make_spy_plugin(tmp_path):
    """A plugin whose subscriber records every event.type it's pumped.
    Returns (LoadedPlugins, received_list) -- received is the SAME list
    object the running subscriber appends to (module-global), so the test
    can poll it live."""
    plugin_dir = tmp_path / "spy-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(_SPY_SUBSCRIBER_PLUGIN_MANIFEST)
    (plugin_dir / "hooks.py").write_text(_SPY_SUBSCRIBER_SRC)
    loaded = load_plugins([tmp_path])
    spy = loaded.plugins[0].subscriber_callables["spy"]
    return loaded, spy.__globals__["received"]


async def test_plugin_subscriber_receives_events_after_clear(tmp_path):
    """_session_driver starts one pump worker per plugin subscriber on the
    ORIGINAL session's bus; a /clear rebuild swaps in a session with a brand
    new bus. Unless the rebuild also restarts the subscriber pumps, the
    subscriber goes deaf the moment /clear runs -- it keeps draining an
    orphaned queue that will never receive another envelope."""
    loaded, received = _make_spy_plugin(tmp_path / "plugins")
    app = make_app(tmp_path, plugins=loaded)  # default EchoProvider
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"hello", "enter")
        await pilot.pause(0.2)
        assert len(received) > 0  # baseline: the pump is alive pre-/clear

        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.2)
        after_clear_baseline = len(received)

        await pilot.press(*"again", "enter")
        await pilot.pause(0.2)
        assert len(received) > after_clear_baseline  # still alive post-/clear


# --- /compact ---


async def test_compact_summarizes_history_and_round_trips(tmp_path):
    provider = FakeProvider(
        [text_turn("first reply"), text_turn("second reply"), text_turn("SUMMARY-TEXT")]
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.2)
        await pilot.press(*"two", "enter")
        await pilot.pause(0.2)
        await pilot.press(*"/compact", "enter")
        await pilot.pause(0.3)
        loop = app.kernel.loop
        assert len(loop.history) == 1
        assert loop.history[0].role == Role.SYSTEM
        assert "SUMMARY-TEXT" in loop.history[0].text()

    envelopes = read_session(tmp_path, app.kernel.session.id)
    compaction_envs = [e for e in envelopes if e.event.type == "compaction_applied"]
    assert len(compaction_envs) == 1  # among the log's LAST events, ahead of the app's own SessionEnded
    compaction = compaction_envs[0].event
    assert compaction.summary == "SUMMARY-TEXT"
    msg_bearing = [
        e
        for e in envelopes
        if e.seq < compaction_envs[0].seq
        and e.event.type in ("user_message", "model_call_completed")
    ]
    assert len(msg_bearing) == 4  # 2 user turns + 2 assistant replies
    assert compaction.from_seq == msg_bearing[0].seq
    assert compaction.to_seq == msg_bearing[-1].seq

    folded = fold(envelopes)
    assert len(folded.messages) == 1
    assert folded.messages[0].role == Role.SYSTEM
    assert folded.messages[0].text() == "Summary of earlier conversation: SUMMARY-TEXT"


class _FailOnNthCallProvider:
    """Serves ok_script turns normally; the Nth .complete() call raises."""

    def __init__(self, ok_script, fail_at):
        self._ok = list(ok_script)
        self._fail_at = fail_at
        self._n = 0

    async def complete(self, *, model, messages, tools=()):
        self._n += 1
        if self._n == self._fail_at:
            raise RuntimeError("summarize boom")
        for chunk in self._ok.pop(0):
            yield chunk


async def test_compact_failure_leaves_history_untouched_and_no_event(tmp_path):
    provider = _FailOnNthCallProvider(
        [text_turn("first reply"), text_turn("second reply")], fail_at=3
    )
    app = make_app(tmp_path, provider=provider, model=ModelId("fake:echo"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"one", "enter")
        await pilot.pause(0.2)
        await pilot.press(*"two", "enter")
        await pilot.pause(0.2)
        history_before = list(app.kernel.loop.history)
        await pilot.press(*"/compact", "enter")
        await pilot.pause(0.3)
        assert app.kernel.loop.history == history_before
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "compact failed" in lines

    envelopes = read_session(tmp_path, app.kernel.session.id)
    assert "compaction_applied" not in [e.event.type for e in envelopes]


async def test_compact_refused_while_turn_running(tmp_path):
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"stuck", "enter")
        await pilot.pause(0.1)  # turn parked at the gate
        history_before = list(app.kernel.loop.history)
        await pilot.press(*"/compact", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
        assert app.kernel.loop.history == history_before
        provider.release.set()  # let the first turn finish before teardown
        await pilot.pause(0.3)


# --- /resume ---


async def _write_seed_session(tmp_path, prompt_text):
    """Run one turn to completion in its own app, close the session, and
    hand back its id -- a prior session for a LATER app's /resume to find."""
    seed = make_app(tmp_path)
    async with seed.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*prompt_text, "enter")
        await pilot.pause(0.2)
    sid = seed.kernel.session.id
    seed.kernel.session.close()
    return sid


async def test_resume_shows_picker_with_age_and_first_prompt(tmp_path):
    await _write_seed_session(tmp_path, "hello from the past")

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/resume", "enter")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, SessionPickerScreen)
        option_list = screen.query_one(OptionList)
        # the app's OWN (currently open) session is excluded -- only the prior one shows
        assert option_list.option_count == 1
        rendered = str(option_list.get_option_at_index(0).prompt)
        assert "hello from the past" in rendered
        await pilot.press("escape")
        await pilot.pause(0.1)


async def test_resume_choosing_session_rebuilds_onto_it(tmp_path):
    seed_sid = await _write_seed_session(tmp_path, "hello from the past")

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        original_sid = app.kernel.session.id
        await pilot.press(*"/resume", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SessionPickerScreen)
        await pilot.press("enter")  # sole option is highlighted by default
        await pilot.pause(0.2)
        assert app.kernel.session.id == seed_sid
        assert app.kernel.session.id != original_sid
        texts = [m.text() for m in app.kernel.loop.history]
        assert any("hello from the past" in t for t in texts)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "hello from the past" in lines
        assert f"resumed session {seed_sid}" in lines


async def test_resume_escape_cancels_session_unchanged(tmp_path):
    await _write_seed_session(tmp_path, "hello from the past")

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        original_sid = app.kernel.session.id
        await pilot.press(*"/resume", "enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SessionPickerScreen)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, SessionPickerScreen)
        assert app.kernel.session.id == original_sid


async def test_resume_refused_while_turn_running(tmp_path):
    provider = GatedProvider()
    app = make_app(tmp_path, provider=provider, model=ModelId("gated"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"stuck", "enter")
        await pilot.pause(0.1)  # turn parked at the gate
        original_sid = app.kernel.session.id
        await pilot.press(*"/resume", "enter")
        await pilot.pause(0.1)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "already running" in lines
        assert not isinstance(app.screen, SessionPickerScreen)
        assert app.kernel.session.id == original_sid
        provider.release.set()  # let the first turn finish before teardown
        await pilot.pause(0.3)


async def test_resume_with_no_prior_sessions_says_so(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"/resume", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "no sessions" in lines.lower()
        assert not isinstance(app.screen, SessionPickerScreen)


async def test_rebuild_in_progress_refuses_turn_and_clear_and_ignores_esc(tmp_path):
    """A /resume rebuild genuinely parked mid-teardown (loop.end gated on a
    real asyncio.Event -- not just pilot.pause()-past-everything) must
    refuse a plain-text submit and a /clear, and Esc must not cancel it.
    Once released, exactly the resumed session is live with no turn that
    snuck in while the kernel was mid-swap."""
    seed_sid = await _write_seed_session(tmp_path, "hello from the past")

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")

        gate = asyncio.Event()
        real_end = app.kernel.loop.end

        async def gated_end():
            await gate.wait()
            return await real_end()

        app.kernel.loop.end = gated_end

        try:
            await pilot.press(*"/resume", "enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, SessionPickerScreen)
            await pilot.press("enter")  # sole option -> _rebuild_kernel starts...
            await pilot.pause(0.2)  # ...and is now parked in gated_end() on `gate`

            assert app._rebuild_in_progress is True

            # plain text is refused while the rebuild is in flight
            await pilot.press(*"sneaky", "enter")
            await pilot.pause(0.1)
            lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert "in progress" in lines

            # /clear is refused too -- it would try to tear down a kernel
            # that's already mid-teardown
            await pilot.press(*"/clear", "enter")
            await pilot.pause(0.1)
            lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert lines.count("in progress") == 2

            # Esc during the rebuild is a no-op, not a cancel
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app._rebuild_in_progress is True
        finally:
            # Always release the gate -- a mid-test assertion failure must
            # not leave the parked rebuild (and app teardown, which awaits
            # the same loop.end) hanging forever.
            gate.set()
        await pilot.pause(0.3)

        assert app._rebuild_in_progress is False
        assert app.kernel.session.id == seed_sid
        texts = [m.text() for m in app.kernel.loop.history]
        assert any("hello from the past" in t for t in texts)
        assert not any("sneaky" in t for t in texts)  # no interleaved turn


# --- Task 6: @-file mentions (Tab completion + dispatcher-gated injection) ---


async def test_tab_completes_at_mention_and_cycles_through_matches(tmp_path):
    (tmp_path / "alpha.py").write_text("a")
    (tmp_path / "alpha_beta.py").write_text("b")
    app = make_app(tmp_path, native_tools=True, workspace_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"see @alp")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert app.query_one("#prompt", Input).value == "see @alpha.py"
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert app.query_one("#prompt", Input).value == "see @alpha_beta.py"
        # cycling wraps back around to the first match
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert app.query_one("#prompt", Input).value == "see @alpha.py"


async def test_tab_without_at_token_is_not_a_regression_on_history(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"plain text")
        await pilot.press("tab")
        await pilot.pause(0.05)
        # Tab with no @ token never mutates the input
        assert app.query_one("#prompt", Input).value == "plain text"
        await pilot.click("#prompt")
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press(*"second", "enter")
        await pilot.pause(0.2)
        # history (up/down) is unaffected by the intervening Tab press
        await pilot.press("up")
        await pilot.pause(0.05)
        assert app.query_one("#prompt", Input).value == "second"
        await pilot.press("up")
        await pilot.pause(0.05)
        assert app.query_one("#prompt", Input).value == "plain text"


async def test_at_mention_injects_file_content_keeps_literal_user_message_and_clears_after_turn(
    tmp_path,
):
    (tmp_path / "alpha.py").write_text("print('hello world')\n")
    provider = FakeProvider(script=[text_turn("ok"), text_turn("ok2")])
    app = make_app(
        tmp_path,
        provider=provider,
        model=ModelId("fake"),
        native_tools=True,
        workspace_root=tmp_path,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"explain @alpha.py", "enter")
        await pilot.pause(0.2)
        # a second, mention-free turn must NOT see the first turn's injected
        # file content -- turn_context is per-turn, cleared after run_turn
        await pilot.press(*"anything else", "enter")
        await pilot.pause(0.2)

    assert len(provider.calls) == 2
    first_call_text = "\n".join(m.text() for m in provider.calls[0])
    assert "print('hello world')" in first_call_text
    assert "explain @alpha.py" in first_call_text
    user_msgs = [m for m in provider.calls[0] if m.role == Role.USER]
    assert any(m.text() == "explain @alpha.py" for m in user_msgs)

    second_call_text = "\n".join(m.text() for m in provider.calls[1])
    assert "print('hello world')" not in second_call_text

    app.kernel.session.close()
    envelopes = read_session(tmp_path, app.kernel.session.id)
    user_events = [e.event for e in envelopes if e.event.type == "user_message"]
    assert len(user_events) == 2
    assert user_events[0].text == "explain @alpha.py"  # persisted message stays literal


async def test_at_mention_denied_by_permission_rule_is_visible_and_turn_still_runs(tmp_path):
    (tmp_path / "secret.txt").write_text("top secret")
    engine = PermissionEngine(
        [RuleSet(rules=[PermissionRule(action="deny", tool="read_file", match={"file_path": "*secret*"})])]
    )
    app = make_app(tmp_path, permissions=engine, native_tools=True, workspace_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"peek @secret.txt", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "peek @secret.txt" in lines  # literal text still shown
        assert "denied" in lines.lower() or "blocked" in lines.lower()  # refusal is visible
        assert "echo: peek @secret.txt" in lines  # turn still ran, on the literal text

    app.kernel.session.close()
    envelopes = read_session(tmp_path, app.kernel.session.id)
    types = [e.event.type for e in envelopes]
    assert "tool_call_proposed" in types  # the read was dispatched (evented), not skipped
    assert "tool_call_completed" in types


async def test_at_mention_oversized_file_is_truncated_with_a_note(tmp_path):
    content = ("x" * 40 + "\n") * 500  # formatted body clears the 16 KiB injection cap
    (tmp_path / "big.txt").write_text(content)
    provider = FakeProvider(script=[text_turn("ok")])
    app = make_app(
        tmp_path,
        provider=provider,
        model=ModelId("fake"),
        native_tools=True,
        workspace_root=tmp_path,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"look @big.txt", "enter")
        await pilot.pause(0.3)

    assert provider.calls
    joined = "\n".join(m.text() for m in provider.calls[0])
    assert "truncat" in joined.lower()
    assert len(joined.encode()) < len(content.encode())


async def test_at_mention_missing_file_passes_through_as_literal_text_no_error(tmp_path):
    provider = FakeProvider(script=[text_turn("ok")])
    app = make_app(
        tmp_path,
        provider=provider,
        model=ModelId("fake"),
        native_tools=True,
        workspace_root=tmp_path,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.click("#prompt")
        await pilot.press(*"see @nope.txt", "enter")
        await pilot.pause(0.2)
        lines = "\n".join(str(line) for line in app.query_one(RichLog).lines)
        assert "see @nope.txt" in lines
        assert "denied" not in lines.lower()
        assert "blocked" not in lines.lower()

    assert provider.calls
    joined = "\n".join(m.text() for m in provider.calls[0])
    assert "see @nope.txt" in joined
