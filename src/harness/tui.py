"""The Textual frontend: a subscriber plus decision provider.

Kernel coupling is deliberate but narrow: the SubscriberBus (render), the
on_chunk tee (streaming), the Resolver (decisions), and the loop/session/mcp
lifecycle calls that mirror run_once's ordering contract."""

import asyncio

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Static

from harness.cli import Kernel
from harness.events import CustomEvent, RetryAttempted, ToolCallCompleted, ToolCallProposed
from harness.hooks import ProposedToolCall
from harness.interaction import PermissionRequest
from harness.messages import Role
from harness.provider import TextDelta, ThinkingDelta
from harness.tui_support import HistoryRing, expand_file_mentions, parse_slash_command

_SNIPPET_CAP = 200


def _plain(text: str) -> Text:
    """Untrusted strings render as plain Text -- no markup, no
    rendering-unsafe control chars (newline/tab kept)."""
    return Text("".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32))


class AppBoundAsk:
    """Late-binding ask: built before the app exists, bound at app start."""

    def __init__(self) -> None:
        self.app: "HarnessApp | None" = None

    async def __call__(self, request: PermissionRequest) -> str:
        if self.app is None:
            return "deny"  # fail closed before the app is up
        return await self.app.push_screen_wait(PermissionScreen(request))


class PermissionScreen(ModalScreen[str]):
    BINDINGS = [
        Binding("y", "answer('allow')", "allow once"),
        Binding("a", "answer('always')", "always"),
        Binding("n", "answer('deny')", "deny"),
        Binding("escape", "answer('deny')", show=False),
    ]

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        action = self.request.action
        what = (
            f"tool {action.tool}" if isinstance(action, ProposedToolCall)
            else f"model {action.model}"
        )
        with Vertical(id="permission-box"):
            yield Static(_plain(f"Permission: {what}"))
            yield Static(_plain(self.request.reason))
            yield Static("[y] allow once   [a] always   [n] deny")

    def action_answer(self, result: str) -> None:
        self.dismiss(result)


class HistoryInput(Input):
    BINDINGS = [
        Binding("up", "history_prev", show=False),
        Binding("down", "history_next", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.history = HistoryRing()

    def action_history_prev(self) -> None:
        self.value = self.history.prev(self.value)
        self.cursor_position = len(self.value)

    def action_history_next(self) -> None:
        self.value = self.history.next(self.value)
        self.cursor_position = len(self.value)


class HarnessApp(App[None]):
    CSS = """
    #live { height: auto; }
    #stats { dock: bottom; height: 1; }
    #prompt { dock: bottom; }
    """
    BINDINGS = [Binding("escape", "interrupt", "Interrupt", priority=True)]

    def __init__(
        self, kernel: Kernel, *, catalog_path=None, ask: "AppBoundAsk | None" = None
    ) -> None:
        super().__init__()
        self.kernel = kernel
        self.catalog_path = catalog_path
        self._turn_worker = None
        self._ended = False
        self._stream_buffer = ""
        if ask is not None:
            ask.app = self

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(id="transcript", wrap=True, markup=False, max_lines=10_000)
            yield Static(id="live")
        yield Static(id="stats")
        yield HistoryInput(id="prompt", placeholder="prompt (/help for commands)")

    def say(self, prefix: str, text: str) -> None:
        line = Text(prefix)
        line.append(_plain(text))
        self.query_one("#transcript", RichLog).write(line)

    def _clear_live(self) -> None:
        self._stream_buffer = ""
        self.query_one("#live", Static).update("")

    def _on_chunk(self, chunk) -> None:
        match chunk:
            case TextDelta(text=text):
                self._stream_buffer += text
                self.query_one("#live", Static).update(_plain(self._stream_buffer))
            case ThinkingDelta():
                self.query_one("#live", Static).update(
                    _plain(self._stream_buffer + " (thinking\u2026)")
                )
            case _:
                pass

    async def on_mount(self) -> None:
        self.query_one("#prompt", HistoryInput).focus()
        self.run_worker(self._session_driver(), group="driver", exit_on_error=False)

    async def _session_driver(self) -> None:
        kernel = self.kernel
        if kernel.mcp is not None:
            for warning in await kernel.mcp.start():
                self.say("! ", warning)
        if not kernel.resumed:
            await kernel.loop.start()
        else:
            self._render_resumed_history()
        self.kernel.loop.on_chunk = self._on_chunk
        for tag in kernel.tags:
            kernel.session.append(
                CustomEvent(namespace="harness", name="tag", data={"tag": tag})
            )
        if kernel.mcp is not None:
            kernel.mcp.flush_events()
        self.run_worker(self._bus_pump(), group="driver", exit_on_error=False)

    def _render_resumed_history(self) -> None:
        for message in self.kernel.loop.history:
            text = message.text()
            if text:
                prefix = "> " if message.role == Role.USER else ""
                self.say(prefix, text)

    async def _bus_pump(self) -> None:
        queue = self.kernel.session.bus.subscribe()
        while True:
            envelope = await queue.get()
            self._render_event(envelope.event)

    def _render_event(self, event) -> None:
        match event:
            case ToolCallProposed(tool=tool):
                self.say("\u2699 ", str(tool))
            case ToolCallCompleted(result_text=text, is_error=is_error):
                snippet = (text or "(blob)")[:_SNIPPET_CAP]
                self.say("\u2717 " if is_error else "\u2713 ", snippet)
            case CustomEvent(namespace="mcp", name=name, data=data):
                server = data.get("server", "")
                self.say("mcp ", f"{name}: {server}")
            case RetryAttempted():
                self._clear_live()
                self.say("! ", "retrying\u2026")
            case _:
                pass

    @on(Input.Submitted, "#prompt")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        self.query_one("#prompt", HistoryInput).history.remember(text)
        command = parse_slash_command(text)
        if command is not None:
            # Commands are deliberately NOT blocked mid-turn: /quit during a
            # stuck turn must remain possible (it cancels the agent group in
            # _finish); /help and /tools are read-only; /model mutates the
            # loop only between dispatches.
            await self._run_command(command)
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self.say("! ", "a turn is already running -- Esc to interrupt it first")
            return
        expanded, attached, errors = expand_file_mentions(text)
        if errors:
            for error in errors:
                self.say("! ", error)
            return
        for path in attached:
            self.say("+ ", f"attached {path}")
        self.say("> ", text)
        self._turn_worker = self.run_worker(
            self._run_turn(expanded), group="agent", exit_on_error=False
        )

    async def _run_turn(self, prompt: str) -> None:
        self._clear_live()
        try:
            reply = await self.kernel.loop.run_turn(prompt)
        except asyncio.CancelledError:
            self._clear_live()
            raise                                   # Esc repair lands in Task 7
        except Exception as exc:
            self._clear_live()
            self.kernel.loop.repair_turn()      # orphaned user msg is benign;
            self.say("! ", f"turn failed: {exc}")  # unpaired tool calls are not
            return
        self._clear_live()
        self.say("", reply)

    async def _run_command(self, command) -> None:  # Task 8 fills this in
        self.say("! ", f"unknown command: /{command.name}")

    def action_interrupt(self) -> None:             # Task 7 fills this in
        pass

    async def _finish(self) -> None:
        if self._ended:
            return
        self._ended = True
        self.workers.cancel_group(self, "agent")
        try:
            await self.kernel.loop.end()
        except RuntimeError:
            pass                                    # already ended elsewhere
        except Exception as exc:
            self.say("! ", f"end failed: {exc}")

    async def on_unmount(self) -> None:
        await self._finish()


async def run_tui(
    kernel: Kernel, *, catalog_path=None, ask: "AppBoundAsk | None" = None
) -> None:
    app = HarnessApp(kernel, catalog_path=catalog_path, ask=ask)
    try:
        await app.run_async()
    finally:
        if kernel.mcp is not None:
            await kernel.mcp.stop()
            kernel.mcp.flush_events()
        kernel.session.close()
