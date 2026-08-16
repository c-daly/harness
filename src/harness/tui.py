"""The Textual frontend: a subscriber plus decision provider.

Kernel coupling is deliberate but narrow: the SubscriberBus (render), the
on_chunk tee (streaming), the Resolver (decisions), and the loop/session/mcp
lifecycle calls that mirror run_once's ordering contract."""

import asyncio
import json
import time
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option
from textual.worker import WorkerCancelled, WorkerFailed

from harness.cli import Kernel, build_kernel
from harness.frontmatter import CommandDef
from harness.events import (
    CompactionApplied,
    CustomEvent,
    RetryAttempted,
    ToolCallCompleted,
    ToolCallProposed,
)
from harness.fold import fold
from harness.hooks import ProposedToolCall
from harness.interaction import PermissionRequest
from harness.log import read_session
from harness.mcp_host import McpHost
from harness.messages import Message, Role
from harness.provider import TextDelta, ThinkingDelta, collect
from harness.sessions import SessionSummary, list_sessions
from harness.telemetry import TelemetrySubscriber, open_store_memory, run_rollup
from harness.tui_support import HistoryRing, SlashCommand, expand_file_mentions, parse_slash_command
from harness.types import ModelId, SessionId

_SNIPPET_CAP = 200

_COMPACT_INSTRUCTION = (
    "Summarize the conversation above in a concise handoff paragraph: key facts, "
    "decisions made, and any open threads or next steps. Respond with the summary "
    "text only, no preamble."
)

# A constrained model is one that either self-identifies as "local" (small,
# self-hosted) or advertises a small context window; the threshold and
# default mirror what a typical local GGUF quant ships with.
_SMALL_CONTEXT_THRESHOLD = 32768
_LOCAL_CONTEXT_DEFAULT = 16384
_UNCONSTRAINED_CONTEXT = 10**9  # sentinel: an unset max_input_tokens is never "small"
_CONTEXT_WARN_FRACTION = 0.10


def _schema_token_estimate(specs) -> int:
    """~4 characters/token approximation of the JSON schema payload the
    model receives for these tools every turn."""
    return (
        sum(
            len(
                json.dumps(
                    {"name": str(s.name), "description": s.description, "parameters": s.parameters}
                )
            )
            for s in specs
        )
        // 4
    )


def _format_age(seconds: float) -> str:
    """Coarse "Ns/Nm/Nh/Nd ago" age, rounded down to the largest whole unit."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def _session_row(summary: "SessionSummary") -> str:
    age = _format_age(time.time() - summary.mtime)
    if summary.error:
        return f"{age} ago -- {summary.session_id}  [unreadable: {summary.error}]"
    label = summary.first_prompt or "(no prompt)"
    return f"{age} ago -- {label}"


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


def _describe_call(action) -> str:
    args = action.args
    tool = str(action.tool)
    if tool == "bash":
        cmd = str(args.get("command", ""))
        return cmd if len(cmd) <= 500 else cmd[:500] + f" …[{len(cmd) - 500} more chars]"
    if tool in ("read_file", "write_file", "edit_file", "glob", "grep"):
        key = "file_path" if "file_path" in args else "path" if "path" in args else "pattern"
        val = str(args.get(key, ""))
        if tool == "write_file":
            content_val = str(args.get("content", ""))
            return f"{val}  ({len(content_val)} bytes)"
        if tool == "edit_file":
            old = str(args.get("old_string", ""))[:200]
            new = str(args.get("new_string", ""))[:200]
            return f"{val}\n  - {old}\n  + {new}"
        return val
    return ""


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
        from harness.tui_support import grant_pattern

        action = self.request.action
        with Vertical(id="permission-box"):
            if isinstance(action, ProposedToolCall):
                yield Static(_plain(f"Permission: tool {action.tool}"))
                detail = _describe_call(action)
                if detail:
                    yield Static(_plain(detail))
                tool, match = grant_pattern(self.request)
                if match:
                    grant = f"{tool}({list(match.values())[0]})"
                else:
                    # honest about breadth: an empty match is allow-all-tool, session-only
                    grant = f"{tool} (ALL calls -- session only)"
                yield Static(_plain(f"[a] will allow: {grant}"))
            else:
                yield Static(_plain(f"Permission: model {action.model}"))
            yield Static(_plain(self.request.reason))
            yield Static(_plain("[y] allow once   [a] always   [n] deny"))

    def action_answer(self, result: str) -> None:
        self.dismiss(result)


class ServerChecklistScreen(ModalScreen[set[str]]):
    """Session-start checklist: one row per configured MCP server, pre-checked
    from spec.default_enabled. Enter dismisses with the checked server names;
    McpHost.start(only=...) never constructs or launches the rest -- unchecked
    servers hold zero resources and register zero tools for this session."""

    # priority=True: Checkbox itself binds enter/space to toggle, and the first
    # checkbox holds initial focus -- without priority this binding would never
    # fire (mirrors HarnessApp's own priority Escape binding for the same class
    # of focused-widget-vs-modal conflict). Space still toggles a checkbox.
    BINDINGS = [Binding("enter", "accept", "start selected servers", priority=True)]

    def __init__(self, mcp: McpHost) -> None:
        super().__init__()
        self._specs = mcp.specs

    def compose(self) -> ComposeResult:
        with Vertical(id="checklist-box"):
            yield Static(_plain("MCP servers -- uncheck any you don't want this session"))
            for spec in self._specs:
                target = spec.command or spec.url or ""
                yield Checkbox(
                    f"{spec.name}  ({target})",
                    value=spec.default_enabled,
                    id=f"chk-{spec.name}",
                )
            yield Static(_plain("[enter] start selected servers"))

    def action_accept(self) -> None:
        selected = {
            spec.name
            for spec in self._specs
            if self.query_one(f"#chk-{spec.name}", Checkbox).value
        }
        self.dismiss(selected)


class SessionPickerScreen(ModalScreen["SessionId | None"]):
    """/resume: pick a prior session to reopen. Enter dismisses with the
    highlighted session id (newest first, so Enter alone resumes the most
    recent session); Escape cancels with None. Escape is handled by
    HarnessApp.action_interrupt (mirrors PermissionScreen: the app's own
    priority Esc binding preempts a modal's, so cancellation lives there,
    not in a binding on this screen)."""

    BINDINGS = [Binding("enter", "accept", "resume selected session", priority=True)]

    def __init__(self, sessions: "list[SessionSummary]") -> None:
        super().__init__()
        self._sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="resume-box"):
            yield Static(_plain("Resume which session? [enter] pick   [esc] cancel"))
            yield OptionList(
                *(
                    Option(_plain(_session_row(s)), id=str(s.session_id))
                    for s in self._sessions
                ),
                id="resume-list",
            )

    def action_accept(self) -> None:
        option_list = self.query_one("#resume-list", OptionList)
        highlighted = option_list.highlighted
        if highlighted is None:
            self.dismiss(None)
            return
        option = option_list.get_option_at_index(highlighted)
        self.dismiss(SessionId(option.id))


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
    _THOUGHT_MODES = ("collapse", "full", "off")

    def __init__(
        self,
        kernel: Kernel,
        *,
        catalog_path=None,
        ask: "AppBoundAsk | None" = None,
        native_tools: bool = False,
        workspace_root: "Path | None" = None,
        routing_rules=None,
    ) -> None:
        super().__init__()
        self.kernel = kernel
        self.catalog_path = catalog_path
        # Startup inputs a kernel rebuild (/clear, /resume) needs that are NOT
        # recoverable from the Kernel object itself. Everything else -- provider,
        # model, pricing, tags, plugins, resolver, and (via resolver.engine) the
        # permission engine -- is read back off self.kernel in _rebuild_kernel.
        self._native_tools = native_tools
        self._workspace_root = workspace_root
        self._routing_rules = routing_rules
        self._turn_worker = None
        self._interrupting = False
        self._ended = False
        self._bus_pump_worker = None
        # One pump worker per plugin subscriber, tied to the CURRENT session's
        # bus; a kernel rebuild cancels these and starts fresh ones (see
        # _start_plugin_subscribers) since a new session owns a new bus.
        self._plugin_pump_workers: list = []
        self._stream_buffer = ""
        # /thoughts mode: session-local, not persisted; not reset per turn.
        self._thought_mode = "collapse"  # "collapse" | "full" | "off"
        self._thought_buffer = ""
        self._thought_started: float | None = None
        self._thought_collapsed = False
        self._stats_conn = None
        self._stats_sub = None
        self._stats_queue = None
        self._mcp_errlog = None
        # The checklist selection (or headless default set) from the FIRST mount
        # -- a /clear rebuild restarts exactly these servers, never re-prompting.
        self._mcp_enabled: "set[str] | None" = None
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

    def say(self, prefix: str, text: str, *, style: str | None = None) -> None:
        line = Text(prefix)
        content = _plain(text)
        if style:
            content.stylize(style)
        line.append(content)
        self.query_one("#transcript", RichLog).write(line)

    def _clear_live(self) -> None:
        self._stream_buffer = ""
        self._thought_buffer = ""
        self._thought_started = None
        self._thought_collapsed = False
        self.query_one("#live", Static).update("")

    def _render_live(self) -> None:
        # collapse/full both stream the raw thought above the answer-so-far while
        # thinking is in progress; collapse stops doing so once it has replaced
        # the thought with a "(thought for ...)" summary line (see _on_chunk).
        if (
            self._thought_mode in ("collapse", "full")
            and self._thought_buffer
            and not self._thought_collapsed
        ):
            text = f"{self._thought_buffer}\n{self._stream_buffer}"
        else:
            text = self._stream_buffer
        self.query_one("#live", Static).update(_plain(text))

    def _on_chunk(self, chunk) -> None:
        match chunk:
            case TextDelta(text=text):
                if (
                    self._thought_mode == "collapse"
                    and self._thought_buffer
                    and not self._thought_collapsed
                ):
                    elapsed = (
                        int(time.monotonic() - self._thought_started)
                        if self._thought_started is not None
                        else 0
                    )
                    self._stream_buffer += (
                        f"(thought for {elapsed}s · {len(self._thought_buffer)} chars)\n"
                    )
                    self._thought_collapsed = True
                self._stream_buffer += text
                self._render_live()
            case ThinkingDelta(text=text):
                if self._thought_started is None:
                    self._thought_started = time.monotonic()
                self._thought_buffer += text
                if self._thought_mode == "off":
                    self.query_one("#live", Static).update(
                        _plain(self._stream_buffer + " (thinking\u2026)")
                    )
                else:
                    self._render_live()
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
            selected = await self.push_screen_wait(ServerChecklistScreen(kernel.mcp))
            self._mcp_enabled = selected  # a /clear rebuild restarts exactly this set
            for warning in await kernel.mcp.start(only=selected):
                self.say("! ", warning)
        # Registry is final now (MCP registration above already ran, if any) --
        # safe to estimate the tool-schema footprint against it.
        await self._warn_context_at_mount()
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
            for warning in kernel.plugin_warnings:
                self.say("! ", warning)
            self._start_plugin_subscribers(kernel)
        self._bus_pump_worker = self.run_worker(
            self._bus_pump(_bus_queue), group="driver", exit_on_error=False
        )
        self.set_interval(1.0, self.refresh_stats)

    def _start_plugin_subscribers(self, kernel: Kernel) -> None:
        """Emit each plugin's plugin_loaded event and start one pump worker
        per subscriber, wired onto kernel.session.bus. Called at mount AND
        again after every kernel rebuild (/clear, /resume) -- a rebuilt
        kernel's session owns a brand-new SubscriberBus that nothing is
        listening to yet; without this, subscribers go silently deaf the
        moment the kernel is swapped."""
        if kernel.plugins is None:
            return
        from harness.plugins import _pump

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
                worker = self.run_worker(
                    _pump(_sub_queue, fn, sub_def.name, kernel.session),
                    group="driver",
                    exit_on_error=False,
                )
                self._plugin_pump_workers.append(worker)

    def _render_resumed_history(self) -> None:
        for message in self.kernel.loop.history:
            text = message.text()
            if text:
                prefix = "> " if message.role == Role.USER else ""
                self.say(prefix, text)

    async def _rebuild_kernel(self, resume_session_id: "SessionId | None" = None) -> None:
        """Tear down the current kernel/session cleanly and rebuild in place:
        same provider instance, same permission engine, same resolver/ask
        wiring -- then rewire chunk streaming, event rendering, and stats onto
        the new session. Backs /clear (resume_session_id=None) here; /resume
        passes a session id to reopen instead of starting fresh (Task 4).

        Callers own the "no turn running" guard -- this assumes it's safe to
        tear down the current kernel.
        """
        old_kernel = self.kernel
        old_mcp = old_kernel.mcp
        # These pumps are tied to the OLD session's bus; cancel them now so
        # they don't leak as zombie workers forever draining an orphaned
        # queue. Fresh ones start below, after the new kernel exists.
        for worker in self._plugin_pump_workers:
            worker.cancel()
        self._plugin_pump_workers = []
        try:
            await old_kernel.loop.end()
        except RuntimeError:
            pass  # already ended elsewhere
        except Exception as exc:
            self.say("! ", f"session end failed: {exc}")
        if old_mcp is not None:
            # Mirrors run_tui's own teardown ordering: stop/flush the old host
            # BEFORE closing the session (flush_events() is a no-op on a closed
            # session, so events from stop() would otherwise be silently lost).
            await old_mcp.stop()
            old_mcp.flush_events()
            if self._mcp_errlog is not None:
                self._mcp_errlog.close()
                self._mcp_errlog = None
        old_kernel.session.close()

        # build_kernel's own `mcp=` path constructs a plain McpHost with the
        # DEFAULT transport (real stdio spawn) and no way to pass a test
        # transport_factory through -- so MCP is NOT threaded through
        # build_kernel here. Instead, a fresh McpHost is built by hand below,
        # reusing the specs and transport_factory off the OLD host, mirroring
        # how _build_checklist_kernel wires McpHost onto a kernel in tests.
        kernel = build_kernel(
            provider=old_kernel.provider,
            base_dir=old_kernel.session.base,
            model=old_kernel.loop.model,
            pricing=old_kernel.loop.pricing,
            pricing_for=old_kernel.loop.pricing_for,
            resume_session_id=resume_session_id,
            permissions=getattr(old_kernel.runner.resolver, "engine", None),
            tags=old_kernel.tags,
            resolver=old_kernel.runner.resolver,
            plugins=old_kernel.plugins,
            workspace_root=self._workspace_root,
            native_tools=self._native_tools,
            routing_rules=self._routing_rules,
            model_pinned=old_kernel.loop.model_pinned,
        )
        self.kernel = kernel
        kernel.loop.on_chunk = self._on_chunk

        # Re-subscribe stats BEFORE start()/resumed-render so SessionStarted (or
        # SessionResumed) lands in the fresh queue -- mirrors _session_driver's
        # mount-time ordering.
        self._stats_queue = kernel.session.bus.subscribe(maxsize=4096)

        _bus_queue = kernel.session.bus.subscribe()
        if self._bus_pump_worker is not None:
            self._bus_pump_worker.cancel()
        self._bus_pump_worker = self.run_worker(
            self._bus_pump(_bus_queue), group="driver", exit_on_error=False
        )

        if old_mcp is not None:
            # Restart exactly the servers enabled at the app's FIRST mount
            # (the checklist selection, or the headless default set) -- the
            # checklist itself is never re-shown on a rebuild.
            kernel.mcp = McpHost(
                old_mcp.specs,
                registry=kernel.registry,
                hooks=kernel.hooks,
                session=kernel.session,
                transport_factory=old_mcp._transport_factory,
            )
            # Re-pointed at the NEW session's directory -- restarted servers'
            # stderr must not keep landing under the old (torn-down) session.
            errlog_path = (
                kernel.session.base / "sessions" / str(kernel.session.id) / "mcp-stderr.log"
            )
            errlog_path.parent.mkdir(parents=True, exist_ok=True)
            self._mcp_errlog = errlog_path.open("a")
            kernel.mcp.errlog = self._mcp_errlog
            # McpHost buffers lifecycle events (server_started etc.) until
            # flush_events() -- it must not touch session._seq before
            # session.start() below, so flushing is deferred past it (mirrors
            # _session_driver's own mount-time ordering exactly).
            for warning in await kernel.mcp.start(only=self._mcp_enabled or set()):
                self.say("! ", warning)

        if not kernel.resumed:
            await kernel.loop.start()
        else:
            self._render_resumed_history()

        for tag in kernel.tags:
            kernel.session.append(CustomEvent(namespace="harness", name="tag", data={"tag": tag}))

        if kernel.mcp is not None:
            kernel.mcp.flush_events()

        self._start_plugin_subscribers(kernel)

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
        # full mode: the thought stays visible in the transcript, dimmed, above
        # the answer -- read _thought_buffer BEFORE _clear_live() wipes it.
        if self._thought_mode == "full" and self._thought_buffer:
            self.say("", self._thought_buffer, style="dim")
        self._clear_live()
        self.say("", reply)

    async def _run_compact(self) -> None:
        """One summarize completion through the CURRENT model/provider over
        the whole transcript; on success, replace loop.history with the
        summary as a system message -- exactly what CompactionApplied's fold
        replay produces (fold.py:89-97), so a later resume/read-back matches.
        """
        kernel = self.kernel
        loop = kernel.loop
        try:
            # repair=False: this session's own writer holds the lock right
            # now, so repair (meant for reopening a closed/crashed session)
            # would refuse anyway (log.py:91-95) -- a torn tail here is a
            # genuine read failure and belongs in the except below, not a
            # separate unguarded call that can crash the worker silently.
            state = fold(read_session(kernel.session.base, kernel.session.id, repair=False))
            if not state._msg_seqs:
                self.say("! ", "nothing to compact")
                return
            from_seq, to_seq = state._msg_seqs[0], state._msg_seqs[-1]
            messages = [
                Message.system_text(loop.system_prompt),
                *loop.history,
                Message.user_text(_COMPACT_INSTRUCTION),
            ]
            # Issued directly against the provider (bypassing the dispatcher)
            # so this admin call does not itself become a message-bearing log
            # event that CompactionApplied's fold would need to also collapse.
            summary_message, _usage, _stop = await collect(
                loop.provider.complete(model=loop.model, messages=messages, tools=())
            )
        except Exception as exc:
            self.say("! ", f"compact failed: {exc}")
            return
        summary = summary_message.text()
        kernel.session.append(
            CompactionApplied(from_seq=from_seq, to_seq=to_seq, summary=summary, model=loop.model)
        )
        loop.history = [Message.system_text(f"Summary of earlier conversation: {summary}")]
        self.say("", f"compacted {len(state.messages)} messages -> 1 summary")

    async def _run_resume(self) -> None:
        """/resume: pick a prior session and rebuild the kernel onto it.
        Runs as its own worker (see the /resume dispatch) since
        push_screen_wait needs one. Excludes the CURRENT session from the
        picker -- resuming into the session you're already in is a
        no-op-shaped trap, not a real choice."""
        current_id = self.kernel.session.id
        sessions = [
            s for s in list_sessions(self.kernel.session.base) if s.session_id != current_id
        ]
        if not sessions:
            self.say("! ", "no sessions to resume")
            return
        chosen = await self.push_screen_wait(SessionPickerScreen(sessions))
        if chosen is None:
            return
        self._clear_live()
        self.query_one("#transcript", RichLog).clear()
        await self._rebuild_kernel(resume_session_id=chosen)
        self.say("", f"resumed session {self.kernel.session.id}")

    async def _run_command(self, command: SlashCommand) -> None:
        if command.name == "help":
            self.say(
                "",
                "/help  /model [alias]  /thoughts [collapse|full|off]  /clear  /compact  "
                "/resume  /tools  /quit  — @/path attaches a file",
            )
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
        elif command.name == "thoughts":
            self._set_thought_mode(command.arg.strip())
        elif command.name == "clear":
            if self._turn_worker is not None and self._turn_worker.is_running:
                self.say("! ", "a turn is already running -- Esc to interrupt it first")
                return
            await self._rebuild_kernel()
            self._clear_live()
            self.query_one("#transcript", RichLog).clear()
            self.say("", f"cleared -- new session {self.kernel.session.id}")
        elif command.name == "compact":
            if self._turn_worker is not None and self._turn_worker.is_running:
                self.say("! ", "a turn is already running -- Esc to interrupt it first")
                return
            self._turn_worker = self.run_worker(
                self._run_compact(), group="agent", exit_on_error=False
            )
        elif command.name == "resume":
            if self._turn_worker is not None and self._turn_worker.is_running:
                self.say("! ", "a turn is already running -- Esc to interrupt it first")
                return
            # push_screen_wait must run from a worker (Textual requirement) --
            # unlike /clear and /compact, this command needs a modal answer
            # before it can act, so the picker + rebuild both live in one.
            self.run_worker(self._run_resume(), group="driver", exit_on_error=False)
        elif command.name in self._plugin_commands:
            body = self._plugin_commands[command.name].body
            prompt = body.replace("$ARGUMENTS", command.arg)
            if not prompt.strip():
                self.say("! ", f"command /{command.name} is empty after expansion")
                return
            # A plugin command IS a turn: route through the same guard as plain input.
            # (@file mentions deliberately do NOT expand inside command bodies v1 --
            # the body is the plugin author's text.)
            if self._turn_worker is not None and self._turn_worker.is_running:
                self.say("! ", "a turn is already running -- Esc to interrupt it first")
                return
            self.say("> ", prompt)
            self._turn_worker = self.run_worker(
                self._run_turn(prompt), group="agent", exit_on_error=False
            )
        else:
            self.say("! ", f"unknown command: /{command.name}")

    def _set_thought_mode(self, arg: str) -> None:
        if not arg:
            self.say("", f"thought mode: {self._thought_mode} (collapse|full|off)")
        elif arg in self._THOUGHT_MODES:
            self._thought_mode = arg
            self.say("", f"thought mode -> {arg}")
        else:
            self.say(
                "! ",
                f"unknown thought mode: {arg!r} (valid: {', '.join(self._THOUGHT_MODES)})",
            )

    def _maybe_warn_context(self, resolved) -> None:
        """Toast once when a constrained model's tool-schema footprint eats a
        meaningful slice of its context window -- local/small-context models
        are the ones that actually run out of room for schemas plus history."""
        constrained = (
            "local" in resolved.tags
            or (resolved.max_input_tokens or _UNCONSTRAINED_CONTEXT) < _SMALL_CONTEXT_THRESHOLD
        )
        if not constrained:
            return
        ctx = resolved.max_input_tokens or _LOCAL_CONTEXT_DEFAULT
        specs = self.kernel.registry.specs()
        est = _schema_token_estimate(specs)
        if est <= ctx * _CONTEXT_WARN_FRACTION:
            return
        pct = round(est / ctx * 100)
        self.notify(
            f"{resolved.alias}: {len(specs)} tools \u2248 {est:,} tokens of schemas "
            f"(~{pct}% of {ctx:,} context) \u2014 /tools to review",
            severity="warning",
            timeout=8,
        )

    async def _warn_context_at_mount(self) -> None:
        """Resolve the running model against the catalog once the tool
        registry is final and toast if it's constrained and schema-heavy.
        No-ops without --catalog, or when the model isn't a catalog alias (a
        bare --model run, or the echo/fake providers tests use) -- there are
        no tags/limits to judge it against."""
        if self.catalog_path is None or not Path(self.catalog_path).exists():
            return
        from harness.catalog import Catalog, UnknownAliasError

        catalog = Catalog.load(Path(self.catalog_path))
        try:
            resolved = catalog.resolve(str(self.kernel.loop.model))
        except UnknownAliasError:
            return
        self._maybe_warn_context(resolved)

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
        # An in-flight turn finishes its current dispatch with the old alias and
        # picks the new one up next iteration. The CatalogProvider resolves the
        # endpoint per call from the ALIAS against its catalog snapshot, so
        # switching retargets loop.model AND refreshes that snapshot from the
        # just-loaded catalog — otherwise a models.toml edit made mid-session
        # validates here but dispatches against the startup catalog. A
        # CatalogProvider is concurrency-safe.
        from harness.cli import _make_pricing_for
        from harness.provider_litellm import CatalogProvider

        loop = self.kernel.loop
        loop.model = ModelId(alias)
        loop.model_pinned = True  # an explicit /model is a pin (routing-exempt)
        loop.pricing = resolved.pricing_dict() or None
        provider = loop.provider
        if isinstance(provider, CatalogProvider):
            # Swap the snapshot on the SHARED instance: the loop, the subagent
            # runner, and mixture tools all hold this one provider, so the
            # refresh reaches delegated work too.
            provider.catalog = catalog
        else:
            from harness.provider_claude_code import ClaudeCodeProvider
            from harness.provider_codex import CodexProvider

            # Upgrading out of echo mode (no --model): match cli.py's
            # construction — backend entries must be dispatchable — and swap
            # via the kernel so every holder (loop, subagent runner, kernel)
            # gets the new provider, not just loop.provider.
            self.kernel.set_provider(
                CatalogProvider(catalog, claude_code=ClaudeCodeProvider(), codex=CodexProvider())
            )
        # Unpinned subagents inherit the session's CURRENT model — the build-time
        # default (in echo mode not even a catalog alias) would dispatch experts
        # to the wrong place. Pricing lookups follow the fresh snapshot too.
        self.kernel.runner.default_model = ModelId(alias)
        self.kernel.runner.pricing = loop.pricing
        self.kernel.runner.pricing_for = loop.pricing_for = _make_pricing_for(catalog)
        self.say("", f"model → {alias} ({resolved.route})")
        self._maybe_warn_context(resolved)

    def action_interrupt(self) -> None:
        # The priority Esc binding preempts modal bindings: with a permission
        # modal up, Esc means "deny this ask", not "kill the turn".
        if isinstance(self.screen, PermissionScreen):
            self.screen.action_answer("deny")
            return
        if isinstance(self.screen, SessionPickerScreen):
            self.screen.dismiss(None)
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


async def run_tui(
    kernel: Kernel,
    *,
    catalog_path=None,
    ask: "AppBoundAsk | None" = None,
    native_tools: bool = False,
    workspace_root=None,
    routing_rules=None,
) -> None:
    app = HarnessApp(
        kernel,
        catalog_path=catalog_path,
        ask=ask,
        native_tools=native_tools,
        workspace_root=workspace_root,
        routing_rules=routing_rules,
    )
    try:
        await app.run_async()
    finally:
        # app.kernel, not the `kernel` param -- /clear may have rebuilt it in
        # place, and the live kernel at exit is the one that needs teardown.
        live = app.kernel
        if live.mcp is not None:
            await live.mcp.stop()
            live.mcp.flush_events()
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        live.session.close()
