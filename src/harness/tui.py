"""The Textual frontend: a subscriber plus decision provider.

Swappable by design: everything kernel-side reaches the TUI through four
seams only -- the SubscriberBus, the on_chunk tee, the Resolver, and the
run_once ordering contract mirrored in _session_driver."""

import asyncio

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, RichLog, Static

from harness.cli import Kernel
from harness.events import CustomEvent, ToolCallCompleted, ToolCallProposed
from harness.messages import Role
from harness.tui_support import HistoryRing, expand_file_mentions, parse_slash_command

_SNIPPET_CAP = 200


def _plain(text: str) -> Text:
    """Untrusted strings render as plain Text -- never markup, no control chars."""
    return Text("".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32))


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

    def __init__(self, kernel: Kernel, *, catalog_path=None) -> None:
        super().__init__()
        self.kernel = kernel
        self.catalog_path = catalog_path
        self._turn_worker = None
        self._ended = False
        self._stream_buffer = ""

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
                self.say("⚙ ", str(tool))
            case ToolCallCompleted(result_text=text, is_error=is_error):
                snippet = (text or "(blob)")[:_SNIPPET_CAP]
                self.say("\u2717 " if is_error else "\u2713 ", snippet)
            case CustomEvent(namespace="mcp", name=name, data=data):
                server = data.get("server", "")
                self.say("mcp ", f"{name}: {server}")
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
            await self._run_command(command)
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
        try:
            reply = await self.kernel.loop.run_turn(prompt)
        except asyncio.CancelledError:
            raise                                   # Esc repair lands in Task 7
        except Exception as exc:
            self.say("! ", f"turn failed: {exc}")
            return
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


async def run_tui(kernel: Kernel, *, catalog_path=None) -> None:
    app = HarnessApp(kernel, catalog_path=catalog_path)
    try:
        await app.run_async()
    finally:
        if kernel.mcp is not None:
            await kernel.mcp.stop()
            kernel.mcp.flush_events()
        kernel.session.close()
