"""The Textual frontend: a subscriber plus decision provider.

Kernel coupling is deliberate but narrow: the SubscriberBus (render), the
on_chunk tee (streaming), the Resolver (decisions), and the loop/session/mcp
lifecycle calls that mirror run_once's ordering contract."""

import asyncio
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RichLog, Static
from textual.worker import WorkerCancelled, WorkerFailed

from harness.cli import Kernel
from harness.frontmatter import CommandDef
from harness.events import CustomEvent, RetryAttempted, ToolCallCompleted, ToolCallProposed
from harness.hooks import ProposedToolCall
from harness.interaction import PermissionRequest
from harness.messages import Role
from harness.provider import TextDelta, ThinkingDelta
from harness.telemetry import TelemetrySubscriber, open_store_memory, run_rollup
from harness.tui_support import HistoryRing, SlashCommand, expand_file_mentions, parse_slash_command

_SNIPPET_CAP = 200


def _plain(text: str) -> Text:
    """Untrusted strings render as plain Text -- no markup, no
    rendering-unsafe control chars (newline/tab kept)."""
    return Text("".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32))


class AppBoundAsk:
    """Late-binding ask: built before the app exists, bound at app start.

    Concurrent tool asks are safe without a lock: each push_screen_wait call owns
    its own future and Textual stacks modals in order; answers route to the right
    future because dismiss() resolves exactly the screen that called it.
    No lock is needed in v1 -- stacking is the right behaviour.
    """

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
            f"tool {action.tool}"
            if isinstance(action, ProposedToolCall)
            else f"model {action.model}"
        )
        with Vertical(id="permission-box"):
            yield Static(_plain(f"Permission: {what}"))
            yield Static(_plain(self.request.reason))
            yield Static(_plain("[y] allow once   [a] always   [n] deny"))

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
        self._interrupting = False
        self._ended = False
        self._stream_buffer = ""
        self._stats_conn = None
        self._stats_sub = None
        self._stats_queue = None
        self._mcp_errlog = None
        if ask is not None:
            ask.app = self
        # Build plugin command lookup: name -> CommandDef (from all loaded plugins).
        self._plugin_commands: dict[str, CommandDef] = {}
        if kernel.plugins is not None:
            for cmd in kernel.plugins.commands:
                self._plugin_commands[cmd.name] = cmd

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
        # Subscribe the stats queue BEFORE loop.start() so SessionStarted is captured;
        # run_rollup KeyErrors on unknown session ids. On resumed sessions SessionStarted
        # is past -- refresh_stats guards with try/except KeyError (v1: stats blank);
        # 4096 deep so a tool burst between ticks cannot drop SessionStarted.
        self._stats_conn = open_store_memory()
        self._stats_sub = TelemetrySubscriber(self._stats_conn)
        self._stats_queue = self.kernel.session.bus.subscribe(maxsize=4096)
        if kernel.mcp is not None:
            errlog_path = (
                kernel.session.base / "sessions" / str(kernel.session.id) / "mcp-stderr.log"
            )
            errlog_path.parent.mkdir(parents=True, exist_ok=True)
            self._mcp_errlog = errlog_path.open("a")
            kernel.mcp.errlog = self._mcp_errlog
            for warning in await kernel.mcp.start():
                self.say("! ", warning)
        if not kernel.resumed:
            await kernel.loop.start()
        else:
            self._render_resumed_history()
        self.kernel.loop.on_chunk = self._on_chunk
        for tag in kernel.tags:
            kernel.session.append(CustomEvent(namespace="harness", name="tag", data={"tag": tag}))
        # Subscribe before flush_events() so MCP lifecycle events (server_started etc)
        # are not missed: flush_events() publishes to the bus synchronously.
        _bus_queue = self.kernel.session.bus.subscribe()
        if kernel.mcp is not None:
            kernel.mcp.flush_events()
        # Mirror run_once: plugin warnings + plugin_loaded events land here, after
        # loop.start()/tags/flush, then per-subscriber pumps run as driver workers
        # (Textual cancels the driver group at app exit -- no explicit teardown).
        if kernel.plugins is not None:
            from harness.plugins import _pump

            for warning in kernel.plugin_warnings:
                self.say("! ", warning)
            for plugin in kernel.plugins.plugins:
                kernel.session.append(
                    CustomEvent(
                        namespace="plugin",
                        name="plugin_loaded",
                        data={"plugin": plugin.name, "version": plugin.version},
                    )
                )
                for sub_def in plugin.subscribers:
                    fn = plugin.subscriber_callables.get(sub_def.name)
                    if fn is None:
                        continue
                    _sub_queue = kernel.session.bus.subscribe(maxsize=1024)
                    self.run_worker(
                        _pump(_sub_queue, fn, sub_def.name, kernel.session),
                        group="driver",
                        exit_on_error=False,
                    )
        self.run_worker(self._bus_pump(_bus_queue), group="driver", exit_on_error=False)
        self.set_interval(1.0, self.refresh_stats)

    def _render_resumed_history(self) -> None:
        for message in self.kernel.loop.history:
            text = message.text()
            if text:
                prefix = "> " if message.role == Role.USER else ""
                self.say(prefix, text)

    async def _bus_pump(self, queue) -> None:
        while True:
            envelope = await queue.get()
            self._render_event(envelope.event)

    def refresh_stats(self) -> None:
        if self._stats_sub is None:
            return
        self._stats_sub.drain(self._stats_queue)
        try:
            rollup = run_rollup(self._stats_conn, str(self.kernel.session.id))
        except KeyError:
            return  # nothing indexed yet (or resumed session: v1 stats stay blank)
        cost = rollup["cost"]
        cost_text = f"${cost:.4f}" if cost is not None else "n/a"
        inp = rollup["input_tokens"]
        out = rollup["output_tokens"]
        tc = rollup["tool_calls"]
        model = self.kernel.loop.model
        self.query_one("#stats", Static).update(
            _plain(f"{model} | in {inp} out {out} | cost {cost_text} | tools {tc}")
        )

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
            raise  # _after_interrupt owns cleanup; keep _stream_buffer for it to preserve
        except Exception as exc:
            self._clear_live()
            self.kernel.loop.repair_turn()  # orphaned user msg is benign;
            self.say("! ", f"turn failed: {exc}")  # unpaired tool calls are not
            return
        self._clear_live()
        self.say("", reply)

    async def _run_command(self, command: SlashCommand) -> None:
        if command.name == "help":
            self.say("", "/help  /model [alias]  /tools  /quit  — @/path attaches a file")
            if self._plugin_commands:
                self.say(
                    "",
                    "plugin commands: " + "  ".join(f"/{n}" for n in sorted(self._plugin_commands)),
                )
        elif command.name == "tools":
            for spec in self.kernel.registry.specs():
                self.say("  ", str(spec.name))
        elif command.name == "quit":
            await self._finish()
            self.exit()
        elif command.name == "model":
            # catalog.resolve lazily imports litellm (seconds) -- never block the
            # message handler; the switch applies on the next dispatch anyway
            self.run_worker(self._switch_model(command.arg), group="driver", exit_on_error=False)
        elif command.name in self._plugin_commands:
            body = self._plugin_commands[command.name].body
            prompt = body.replace("$ARGUMENTS", command.arg)
            # A plugin command IS a turn: route through the same guard as plain input.
            # (@file mentions deliberately do NOT expand inside command bodies v1 --
            # the body is the plugin author's text.)
            if self._turn_worker is not None and self._turn_worker.is_running:
                self.say("! ", "a turn is already running — Esc to interrupt it first")
                return
            self.say("> ", prompt)
            self._turn_worker = self.run_worker(
                self._run_turn(prompt), group="agent", exit_on_error=False
            )
        else:
            self.say("! ", f"unknown command: /{command.name}")

    async def _switch_model(self, alias: str) -> None:
        if self.catalog_path is None or not Path(self.catalog_path).exists():
            self.say("! ", "no catalog configured (--catalog)")
            return
        # Catalog.load lazily imports litellm on pricing fallback -- slow first touch
        # is acceptable here; /model is off the hot path.
        from harness.catalog import Catalog, UnknownAliasError

        catalog = Catalog.load(Path(self.catalog_path))
        if not alias:
            for name in catalog.aliases():
                self.say("  ", name)
            return
        try:
            resolved = catalog.resolve(alias)
        except UnknownAliasError:
            self.say("! ", f"unknown alias: {alias}")
            return
        # An in-flight turn finishes its current dispatch on the old provider and
        # picks the new one up next iteration.
        loop = self.kernel.loop
        loop.model = resolved.route
        loop.pricing = resolved.pricing_dict() or None
        from harness.provider_litellm import LiteLLMProvider

        # Subagents keep the provider captured at build time -- /model retargets
        # the ROOT loop only (kernel fact; revisit with the plugin loader).
        loop.provider = LiteLLMProvider(api_base=resolved.api_base)
        self.say("", f"model → {alias} ({resolved.route})")

    def action_interrupt(self) -> None:
        # The priority Esc binding preempts modal bindings: with a permission
        # modal up, Esc means "deny this ask", not "kill the turn".
        if isinstance(self.screen, PermissionScreen):
            self.screen.action_answer("deny")
            return
        worker = self._turn_worker
        if worker is None or worker.is_finished or self._interrupting:
            return
        # once per logical interrupt -- an invariant, not a timing bet: a second
        # Esc in the same tick still sees is_finished=False, so the flag guards it.
        self._interrupting = True
        worker.cancel()
        self.run_worker(self._after_interrupt(worker), group="driver", exit_on_error=False)

    async def _after_interrupt(self, worker) -> None:
        try:
            try:
                await worker.wait()
            except (WorkerCancelled, WorkerFailed):
                pass
            self.kernel.loop.interrupt_turn()
            if self._stream_buffer:
                self.say("~ ", self._stream_buffer)  # keep the partial visible
            self._clear_live()
            self.say("! ", "interrupted")
        finally:
            self._interrupting = False

    async def _finish(self) -> None:
        if self._ended:
            return
        self._ended = True
        self.workers.cancel_group(self, "agent")
        await asyncio.sleep(0)  # let the cancelled turn unwind before SessionEnded lands
        try:
            await self.kernel.loop.end()
        except RuntimeError:
            pass  # already ended elsewhere
        except Exception as exc:
            self.say("! ", f"end failed: {exc}")

    async def on_unmount(self) -> None:
        await self._finish()


async def run_tui(kernel: Kernel, *, catalog_path=None, ask: "AppBoundAsk | None" = None) -> None:
    app = HarnessApp(kernel, catalog_path=catalog_path, ask=ask)
    try:
        await app.run_async()
    finally:
        if kernel.mcp is not None:
            await kernel.mcp.stop()
            kernel.mcp.flush_events()
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        kernel.session.close()
