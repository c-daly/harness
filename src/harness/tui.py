"""The Textual frontend: a subscriber plus decision provider.

Kernel coupling is deliberate but narrow: the SubscriberBus (render), the
on_chunk tee (streaming), the Resolver (decisions), and the loop/session/mcp
lifecycle calls that mirror run_once's ordering contract."""

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import RenderableType
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option
from textual.worker import WorkerCancelled, WorkerFailed
from textual_image.widget.sixel import Image as SixelImage, SixelOptions

from harness.blobs import INLINE_THRESHOLD
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
from harness.math_markdown import MathMarkdown, SIXEL_META_KEY, SixelPlacement
from harness.mcp_host import McpHost
from harness.messages import Message, Role
from harness.provider import TextDelta, ThinkingDelta, collect
from harness.sessions import SessionSummary, list_sessions
from harness.telemetry import TelemetrySubscriber, open_store_memory, run_rollup
from harness.tui_panel import ActivityPanel
from harness.tui_support import HistoryRing, SlashCommand, parse_slash_command
from harness.types import ModelId, SessionId, ToolName, new_call_id

_SNIPPET_CAP = 200

# Task 8: the activity panel's agent-swarm section exists only when a server
# by exactly this name is among the enabled MCP servers (the checklist
# selection) -- absent otherwise, a named test contract rather than an
# empty placeholder. The state tool name mirrors McpHost's own
# `mcp__{server}__{tool}` registration convention (mcp_host.py) for a
# server exposing a workflow-shaped tool literally named
# "workflow__workflow_get_state".
_AGENT_SWARM_SERVER_NAME = "agent-swarm"
_AGENT_SWARM_STATE_TOOL = "mcp__agent-swarm__workflow__workflow_get_state"

# @-mentions (Task 6): a mention is "@" at the start of a token (start-of-string
# or preceded by whitespace) -- this is what lets a bare relative path like
# "@alpha.py" mention without a path-prefix requirement, while an embedded "@"
# in "bob@example.com" is never even considered a candidate. A token that does
# not resolve to a real file (a handle, a typo) simply fails the dispatcher
# read and passes through as plain text (contract d) -- no separate
# email/handle heuristic is needed.
_MENTION_TOKEN_RE = re.compile(r"(?:^|(?<=\s))@(\S+)")
_MENTION_TRAILING_PUNCT = ".,!?;:'\")}]"
# Reuses the blob-spill threshold (harness.blobs.INLINE_THRESHOLD, 16 KiB) as the
# injected-context cap: dispatch_tool already spills a read_file result above this
# size to a blob, so "outcome.blob is not None" IS the >16 KiB signal (see
# _inject_mentions) -- no separate constant to keep in sync with the tool.
_MENTION_CONTEXT_CAP = INLINE_THRESHOLD

# Tab-completion candidate listing (Task 6): directories that are never useful
# @-mention targets and are worth skipping outright in the non-git fallback walk.
_MENTION_WALK_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox",
}
_MENTION_WALK_CAP = 5000  # bounded os.walk: cap file count, not just depth


@dataclass
class _TranscriptSixel:
    placement_id: str
    widget: SixelImage
    x: int
    absolute_y: int
    width: int
    rows: int


class MathTranscriptStack(Container):
    """Transcript stack that preserves scrolling over equation widgets."""

    def on_mouse_scroll_down(self, event) -> None:
        self.app.query_one("#transcript", MathTranscript).scroll_down(animate=False)
        event.stop()

    def on_mouse_scroll_up(self, event) -> None:
        self.app.query_one("#transcript", MathTranscript).scroll_up(animate=False)
        event.stop()


class MathTranscript(RichLog):
    """RichLog whose display-math placeholders are backed by Sixel widgets.

    textual-image's Rich renderable is not compatible with Textual: its cursor
    controls become stored log segments.  The library's widget renderer instead
    needs the final screen region and crop, so equation widgets live in the
    transparent sibling overlay while this log retains blank placeholder cells.
    """

    def __init__(self, *args, **kwargs) -> None:
        self._sixel_widgets: list[_TranscriptSixel] = []
        self._mounted_placement_ids: set[str] = set()
        super().__init__(*args, **kwargs)

    def write(
        self,
        content,
        width: int | None = None,
        expand: bool = False,
        shrink: bool = True,
        scroll_end: bool | None = None,
        animate: bool = False,
    ):
        result = super().write(content, width, expand, shrink, scroll_end, animate)
        if isinstance(content, MathMarkdown) and content.sixel_placements:
            self._mount_sixel_placements(content.sixel_placements)
        return result

    def _mount_sixel_placements(
        self, placements: dict[str, SixelPlacement]
    ) -> None:
        wanted = {
            placement_id: placement
            for placement_id, placement in placements.items()
            if placement_id not in self._mounted_placement_ids
        }
        if not wanted or not self.is_mounted:
            return

        located: dict[str, tuple[int, int]] = {}
        for line_index, line in enumerate(self.lines):
            cell_offset = 0
            for segment in line:
                style = segment.style
                placement_id = (
                    style.meta.get(SIXEL_META_KEY) if style is not None else None
                )
                if placement_id in wanted and placement_id not in located:
                    located[placement_id] = (
                        cell_offset,
                        self._start_line + line_index,
                    )
                cell_offset += segment.cell_length

        stack = self.app.query_one("#transcript-stack", Container)
        widgets = []
        for placement_id, (x, absolute_y) in located.items():
            placement = wanted[placement_id]
            widget = SixelImage(
                placement.image,
                classes="math-sixel",
                sixel_options=SixelOptions(colors=16),
            )
            widget.styles.position = "absolute"
            widget.styles.width = placement.width
            widget.styles.height = placement.rows
            tracked = _TranscriptSixel(
                placement_id,
                widget,
                x,
                absolute_y,
                placement.width,
                placement.rows,
            )
            self._sixel_widgets.append(tracked)
            self._mounted_placement_ids.add(placement_id)
            widgets.append(widget)
        if widgets:
            # Mount only the equation-sized widgets above the transcript.  A
            # full-screen transparent sibling still contributes blank cells to
            # Textual's compositor and therefore erases all prose beneath it.
            stack.mount(*widgets)
            self._position_sixel_widgets()
            self.call_after_refresh(self._position_sixel_widgets)

    def _position_sixel_widgets(self) -> None:
        if not self.is_mounted:
            return
        scroll_x = round(self.scroll_x)
        scroll_y = round(self.scroll_y)
        viewport = self.scrollable_content_region
        retained: list[_TranscriptSixel] = []
        for tracked in self._sixel_widgets:
            if tracked.absolute_y + tracked.rows <= self._start_line:
                tracked.widget.remove()
                self._mounted_placement_ids.discard(tracked.placement_id)
                continue
            x = tracked.x - scroll_x
            y = tracked.absolute_y - self._start_line - scroll_y
            tracked.widget.styles.offset = (x, y)
            tracked.widget.display = (
                x < viewport.width
                and x + tracked.width > 0
                and y < viewport.height
                and y + tracked.rows > 0
            )
            retained.append(tracked)
        self._sixel_widgets = retained

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        if round(old_value) != round(new_value):
            self.call_after_refresh(self._position_sixel_widgets)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if round(old_value) != round(new_value):
            self.call_after_refresh(self._position_sixel_widgets)

    def on_resize(self, event) -> None:
        super().on_resize(event)
        self.call_after_refresh(self._position_sixel_widgets)

    def clear(self):
        for tracked in self._sixel_widgets:
            tracked.widget.remove()
        self._sixel_widgets.clear()
        self._mounted_placement_ids.clear()
        return super().clear()


def _list_workspace_files(root: Path) -> list[str]:
    """@-mention Tab-completion candidates, relative to root, tracked files
    first. `git ls-files` (+ untracked-non-ignored) when root is a git worktree;
    a bounded, sorted os.walk otherwise (no git binary, or not a repo)."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=2
        )
        if tracked.returncode == 0:
            files = [line for line in tracked.stdout.splitlines() if line]
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=root, capture_output=True, text=True, timeout=2,
            )
            if untracked.returncode == 0:
                files += [line for line in untracked.stdout.splitlines() if line]
            return files
    except (OSError, subprocess.SubprocessError):
        pass
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in _MENTION_WALK_SKIP_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
            if len(found) >= _MENTION_WALK_CAP:
                return sorted(found)
    return sorted(found)


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
        Binding("tab", "complete_mention", show=False),
    ]

    def __init__(self, *, workspace_root: "Path | None" = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.history = HistoryRing()
        self.workspace_root = workspace_root
        # Candidate file list: shelled out to git (or walked) at most once per
        # prompt -- reset_mention_cache() (called on submit) starts the next
        # prompt-session fresh so a file created/removed mid-session is picked up.
        self._mention_files: "list[str] | None" = None
        # In-progress Tab cycle: {"start": word-start index, "matches": [...],
        # "index": which match is currently applied}. Reset whenever Tab is
        # pressed somewhere that isn't a continuation of this same cycle.
        self._mention_cycle: "dict | None" = None

    def action_history_prev(self) -> None:
        self.value = self.history.prev(self.value)
        self.cursor_position = len(self.value)

    def action_history_next(self) -> None:
        self.value = self.history.next(self.value)
        self.cursor_position = len(self.value)

    def reset_mention_cache(self) -> None:
        self._mention_files = None
        self._mention_cycle = None

    def _word_bounds(self) -> tuple[int, int]:
        """The whitespace-delimited word touching the cursor -- the token Tab
        would complete if it starts with '@'."""
        text, pos = self.value, self.cursor_position
        start = pos
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        end = pos
        while end < len(text) and not text[end].isspace():
            end += 1
        return start, end

    async def _mention_matches(self, prefix: str) -> list[str]:
        if self._mention_files is None:
            root = self.workspace_root or Path.cwd()
            # _list_workspace_files shells out to git (subprocess.run, twice)
            # or falls back to a bounded os.walk -- both blocking. Off the
            # event loop via an executor so Tab-completion never stalls the
            # app (I-3 + parked-1); cache semantics (once per prompt-session,
            # reset by reset_mention_cache on submit) are unchanged.
            self._mention_files = await asyncio.get_running_loop().run_in_executor(
                None, _list_workspace_files, root
            )
        needle = prefix.lower()
        return [f for f in self._mention_files if needle in f.lower()]

    async def action_complete_mention(self) -> None:
        start, end = self._word_bounds()
        word = self.value[start:end]
        cyc = self._mention_cycle
        if cyc is not None and cyc["start"] == start and word == "@" + cyc["matches"][cyc["index"]]:
            # Continuing an in-progress cycle: advance to the next match (wraps).
            cyc["index"] = (cyc["index"] + 1) % len(cyc["matches"])
            self._apply_mention_completion(start, end, cyc["matches"][cyc["index"]])
            return
        if not word.startswith("@"):
            # No @-token under the cursor -- fall through to the pre-Task-6
            # default (focus-next) instead of swallowing Tab silently, since a
            # Binding here would otherwise shadow Screen's own "tab" binding.
            self._mention_cycle = None
            self.screen.focus_next()
            return
        matches = await self._mention_matches(word[1:])
        if not matches:
            self._mention_cycle = None
            return  # nothing to complete; stay put rather than jump focus
        self._mention_cycle = {"start": start, "matches": matches, "index": 0}
        self._apply_mention_completion(start, end, matches[0])

    def _apply_mention_completion(self, start: int, end: int, replacement: str) -> None:
        new_word = "@" + replacement
        self.value = self.value[:start] + new_word + self.value[end:]
        self.cursor_position = start + len(new_word)


class HarnessApp(App[None]):
    CSS = """
    #transcript-stack {
        height: 1fr;
        width: 100%;
        layers: transcript images;
    }
    #transcript {
        height: 100%;
        width: 100%;
        layer: transcript;
    }
    .math-sixel {
        position: absolute;
        layer: images;
        background: transparent;
    }
    #live { height: auto; }
    #stats { dock: bottom; height: 1; }
    #statusbar { dock: bottom; height: 1; }
    #prompt { dock: bottom; }
    """
    BINDINGS = [
        Binding("escape", "interrupt", "Interrupt", priority=True),
        # f2: an unbound function key, safe from collision with normal typing
        # in the prompt Input (unlike a printable key) -- priority=True mirrors
        # Escape above so it fires regardless of which widget currently has
        # focus, same "focused-widget-vs-modal conflict" class of reasoning.
        Binding("f2", "toggle_panel", "Activity panel", priority=True),
    ]
    _THOUGHT_MODES = ("collapse", "full", "off")
    _MARKDOWN_MODES = ("on", "off")

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
        # /compact's own worker (item 8): tracked SEPARATELY from
        # _turn_worker so Esc during /compact takes its own cancellation
        # path (_after_compact_interrupt) rather than _after_interrupt's
        # loop.interrupt_turn() -- /compact is an internal admin call, not
        # a user turn, and a UserInterrupt envelope for it would be a false
        # fact in the event-sourced log.
        self._compact_worker = None
        self._interrupting = False
        # True for the full span of a kernel rebuild (/clear, /resume) --
        # set at entry to _rebuild_kernel, cleared in its finally. A turn
        # cannot start, another rebuild cannot start, and Esc cannot cancel
        # while this is true: the old kernel/session may be mid-teardown
        # (loop.end, mcp.stop) or the new one mid-startup (mcp.start), and
        # none of those are safe to interleave with or interrupt.
        self._rebuild_in_progress = False
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
        # /markdown mode: session-local, not persisted -- like _thought_mode,
        # survives a /clear rebuild since it lives on the app, not the kernel.
        self._markdown_mode = True
        self._thought_started: float | None = None
        self._thought_collapsed = False
        self._stats_conn = None
        self._stats_sub = None
        self._stats_queue = None
        # Last rollup dict seen by refresh_stats -- the status bar's tool-count
        # and cost segments reuse it instead of re-querying telemetry on every
        # /model switch or kernel rebuild. Reset to None on a kernel rebuild
        # (fresh session, stale counts would otherwise linger until the next tick).
        self._last_rollup: "dict | None" = None
        # mtime-keyed Catalog cache for the 1s tick's ctx/cost segments
        # (parked-4): reloaded only when catalog_path's mtime changes, so a
        # steady-state valid file is parsed once, not on every tick.
        self._catalog_cache: "tuple[float, object] | None" = None
        self._mcp_errlog = None
        # The checklist selection (or headless default set) from the FIRST mount
        # -- a /clear rebuild restarts exactly these servers, never re-prompting.
        self._mcp_enabled: "set[str] | None" = None
        # Task 8: the activity panel. Mounted once, after _mcp_enabled is known
        # (_mount_panel), and persists across a /clear rebuild -- only its bus
        # subscription and local event history are torn down/reset then.
        self._panel: "ActivityPanel | None" = None
        self._panel_pump_worker = None
        if ask is not None:
            ask.app = self
        # Build plugin command lookup: name -> CommandDef (from all loaded plugins).
        self._plugin_commands: dict[str, CommandDef] = {}
        if kernel.plugins is not None:
            for cmd in kernel.plugins.commands:
                self._plugin_commands[cmd.name] = cmd

    def compose(self) -> ComposeResult:
        with Vertical():
            with MathTranscriptStack(id="transcript-stack"):
                yield MathTranscript(
                    id="transcript", wrap=True, markup=False, max_lines=10_000
                )
            yield Static(id="live")
        yield Static(id="stats")
        yield Static(id="statusbar")
        yield HistoryInput(
            id="prompt",
            placeholder="prompt (/help for commands)",
            workspace_root=self._workspace_root,
        )

    def say(self, prefix: str, text: str, *, style: str | None = None) -> None:
        line = Text(prefix)
        content = _plain(text)
        if style:
            content.stylize(style)
        line.append(content)
        self.query_one("#transcript", RichLog).write(line)

    def _render_reply(self, text: str) -> RenderableType:
        """The seam a completed assistant reply is written through -- markdown
        by default, /markdown off reverts to the plain-text seam used
        everywhere else. Thought summaries, errors, and system lines call
        say() (-> _plain) directly and never pass through here."""
        if self._markdown_mode:
            theme = self.current_theme
            math_color = theme.foreground or ("#f4f4f4" if theme.dark else "#202020")
            return MathMarkdown(text, color=math_color, sixel_widgets=True)
        return _plain(text)

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
        # _mcp_enabled is final now (whether or not kernel.mcp exists) -- safe
        # to decide the agent-swarm section's presence and mount the panel.
        self._mount_panel(kernel)
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

    def _mount_panel(self, kernel: Kernel) -> None:
        """Mount the (hidden) activity panel once _mcp_enabled is known, and
        start its bus subscription. Called once, at first mount only -- a
        /clear rebuild reuses this same widget (see _rebuild_kernel_body),
        it just re-subscribes and resets local state, exactly like a plugin
        pump but for one dedicated, always-on "subscriber"."""
        enabled = self._mcp_enabled or set()
        panel = ActivityPanel(
            agent_swarm_enabled=_AGENT_SWARM_SERVER_NAME in enabled,
            fetch_agent_swarm_state=self._fetch_agent_swarm_state,
            id="activity-panel",
        )
        self._panel = panel
        self.mount(panel)
        self._start_panel_subscriber(kernel)

    def _start_panel_subscriber(self, kernel: Kernel) -> None:
        """Same bus-subscription mechanism _start_plugin_subscribers uses:
        one queue off kernel.session.bus, one pump worker in the driver
        group. Called at mount (_mount_panel) AND again after every kernel
        rebuild, since a rebuilt kernel's session owns a brand-new bus."""
        if self._panel is None:
            return
        queue = kernel.session.bus.subscribe(maxsize=1024)
        self._panel_pump_worker = self.run_worker(
            self._panel_pump(queue), group="driver", exit_on_error=False
        )

    async def _panel_pump(self, queue) -> None:
        while True:
            envelope = await queue.get()
            if self._panel is not None:
                self._panel.record(envelope)

    async def _fetch_agent_swarm_state(self) -> str:
        """Evented, permission-gated fetch through the CURRENT kernel's
        dispatcher -- resolved at call time (not captured at panel-mount
        time) so a /clear rebuild's new dispatcher is picked up for free,
        the same reason _inject_mentions reads self.kernel.loop.dispatcher
        fresh on every call rather than binding it once."""
        dispatcher = self.kernel.loop.dispatcher
        outcome = await dispatcher.dispatch_tool(
            ProposedToolCall(call_id=new_call_id(), tool=ToolName(_AGENT_SWARM_STATE_TOOL), args={})
        )
        if outcome.is_error:
            raise RuntimeError(outcome.text or "agent-swarm state fetch failed")
        return outcome.text or ""

    def _toggle_panel(self) -> None:
        panel = self._panel
        if panel is None:
            return  # not mounted yet (very early in startup) -- no-op
        panel.display = not panel.display
        if panel.display:
            panel.refresh_files_and_agents()
        # Deliberately leaves focus on #prompt: opening the panel must not
        # interrupt typing flow. Interacting with the panel (e.g. the "r"
        # refresh-workflows key) needs a click/Tab into it first, same as
        # any other Textual sidebar widget.

    def action_toggle_panel(self) -> None:
        self._toggle_panel()

    def _render_resumed_history(self) -> None:
        """Replay a resumed session's history into the transcript. ASSISTANT
        replies go through the SAME _render_reply seam a live turn's
        completed reply uses (parked-2) -- so an old markdown reply matches
        live rendering under the current /markdown mode -- rather than the
        always-plain say()/_plain() seam. User lines keep their plain '> '
        prefix; system/tool lines stay plain, exactly as before."""
        for message in self.kernel.loop.history:
            text = message.text()
            if not text:
                continue
            if message.role == Role.ASSISTANT:
                self.query_one("#transcript", RichLog).write(self._render_reply(text))
            else:
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

        Sets _rebuild_in_progress for the full span (cleared in `finally`,
        so it still clears if the rebuild itself raises): every "is
        something running" guard -- plain-text submit, /clear, /compact,
        /resume, plugin commands, and Esc -- refuses to interleave with a
        kernel swap that's mid-teardown (loop.end/mcp.stop) or mid-startup
        (mcp.start), rather than racing a half-torn-down kernel.
        """
        self._rebuild_in_progress = True
        try:
            await self._rebuild_kernel_body(resume_session_id)
        finally:
            self._rebuild_in_progress = False

    async def _rebuild_kernel_body(self, resume_session_id: "SessionId | None" = None) -> None:
        old_kernel = self.kernel
        old_mcp = old_kernel.mcp
        # These pumps are tied to the OLD session's bus; cancel them now so
        # they don't leak as zombie workers forever draining an orphaned
        # queue. Fresh ones start below, after the new kernel exists.
        for worker in self._plugin_pump_workers:
            worker.cancel()
        self._plugin_pump_workers = []
        if self._panel_pump_worker is not None:
            self._panel_pump_worker.cancel()
            self._panel_pump_worker = None
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
        # A fresh/reopened session has its own telemetry root -- the OLD
        # rollup's tool count must not linger in the status bar until the
        # next 1s tick.
        self._last_rollup = None

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
        # Same widget, new session: reset its local event history BEFORE
        # re-subscribing (so nothing from the fresh bus is wiped by the
        # reset) -- mirrors _last_rollup's reset just above for the status bar.
        if self._panel is not None:
            self._panel.reset()
        self._start_panel_subscriber(kernel)
        self._refresh_statusbar()

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
            # I-2: a resumed session's SessionStarted/SessionResumed always
            # predates this app's own stats-queue subscription (build_kernel's
            # resume_session() publishes it before _rebuild_kernel_body/
            # _session_driver ever subscribes), so this session id never gets
            # a `sessions` row in the LIVE store -- tool count/cost genuinely
            # have no rollup to draw from and stay blank. ctx% does not: it's
            # computed straight off loop.history, so the bar must still
            # refresh here rather than freezing forever.
            self._refresh_statusbar()
            return
        cost = rollup["cost"]
        cost_text = f"${cost:.4f}" if cost is not None else "n/a"
        inp = rollup["input_tokens"]
        out = rollup["output_tokens"]
        tc = rollup["tool_calls"]
        model = self.kernel.loop.model
        self.query_one("#stats", Static).update(
            _plain(f"{model} | in {inp} out {out} | cost {cost_text} | tools {tc}")
        )
        self._refresh_statusbar(rollup)

    def _statusbar_catalog_segments(self) -> tuple["str | None", "str | None"]:
        """Resolve the current model against the catalog for the status bar's
        ctx/cost segments. Both None when the model isn't a catalog alias
        (echo mode, or a bare --model run) -- there's no limit or pricing to
        judge fill or cost against -- and ALSO both None on any failure to
        load or resolve the catalog (parked-4): this runs off the 1s tick
        (refresh_stats -> _refresh_statusbar), so a mid-session malformed
        models.toml must drop these segments for the refresh rather than
        raising once a second. Reload is mtime-keyed: a steady-state valid
        file is parsed once, not re-parsed every tick. Mirrors the resolve
        idiom in _maybe_warn_context / _switch_model (those are mount-time/
        /model-only, narrower UnknownAliasError handling is fine there)."""
        if self.catalog_path is None:
            return None, None
        path = Path(self.catalog_path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None, None
        from harness.catalog import Catalog

        try:
            if self._catalog_cache is not None and self._catalog_cache[0] == mtime:
                catalog = self._catalog_cache[1]
            else:
                catalog = Catalog.load(path)
                self._catalog_cache = (mtime, catalog)
            resolved = catalog.resolve(str(self.kernel.loop.model))
        except Exception:
            return None, None

        ctx_segment = None
        limit = resolved.max_input_tokens
        if limit is None and "local" in resolved.tags:
            limit = _LOCAL_CONTEXT_DEFAULT
        if limit:
            specs = self.kernel.registry.specs()
            est = _schema_token_estimate(specs) + sum(
                len(m.text()) // 4 for m in self.kernel.loop.history
            )
            ctx_segment = f"ctx {round(est / limit * 100)}%"

        cost = self._last_rollup["cost"] if self._last_rollup else None
        cost_segment = f"${cost if cost is not None else 0.0:.4f}"

        return ctx_segment, cost_segment

    def _refresh_statusbar(self, rollup: "dict | None" = None) -> None:
        """Recompute the persistent #statusbar. Called at turn end (reusing
        the rollup refresh_stats already computed -- no second telemetry
        query), from /model (no turn required), after a kernel rebuild
        (/clear, /resume), and after /compact (history shrinks, ctx% moves)."""
        if rollup is not None:
            self._last_rollup = rollup
        tool_calls = self._last_rollup["tool_calls"] if self._last_rollup else 0
        segments = [str(self.kernel.loop.model)]
        ctx_segment, cost_segment = self._statusbar_catalog_segments()
        if ctx_segment is not None:
            segments.append(ctx_segment)
        if cost_segment is not None:
            segments.append(cost_segment)
        segments.append(f"tools {tool_calls}")
        self.query_one("#statusbar", Static).update(_plain(" | ".join(segments)))

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

    def _refuse_if_busy(self) -> bool:
        """Shared guard for anything that would touch the kernel or start a
        turn: refuses (with a visible message) while a kernel rebuild is
        mid-flight, then while a turn is already running, then while a
        /compact is already running (tracked separately -- item 8). Returns
        True if the caller should bail out without acting."""
        if self._rebuild_in_progress:
            self.say("! ", "a session rebuild is in progress -- try again in a moment")
            return True
        if self._turn_worker is not None and self._turn_worker.is_running:
            self.say("! ", "a turn is already running -- Esc to interrupt it first")
            return True
        if self._compact_worker is not None and self._compact_worker.is_running:
            self.say("! ", "a /compact is already running -- Esc to cancel it first")
            return True
        return False

    @on(Input.Submitted, "#prompt")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        prompt_input = self.query_one("#prompt", HistoryInput)
        event.input.clear()
        prompt_input.reset_mention_cache()  # next prompt re-lists workspace files
        if not text:
            return
        prompt_input.history.remember(text)
        command = parse_slash_command(text)
        if command is not None:
            # Commands are deliberately NOT blocked mid-turn: /quit during a
            # stuck turn must remain possible (it cancels the agent group in
            # _finish); /help and /tools are read-only; /model mutates the
            # loop only between dispatches.
            await self._run_command(command)
            return
        if self._refuse_if_busy():
            return
        self.say("> ", text)
        # @-mentions expand for the MODEL, never for the log: the literal text
        # (with its @tokens) is what run_turn persists as the user message;
        # any file content a mention resolves to rides in per-turn context
        # (loop.set_turn_context) that dispatch_model sees but history never
        # does. Awaited here, before the worker starts, so a permission
        # prompt for a mentioned read (same dispatcher path a model-issued
        # read_file takes) can be answered before the turn itself begins.
        context = await self._inject_mentions(text)
        # M-1: re-check busy-ness AFTER the await above -- another submit
        # (or a /clear) can slip in and start running while THIS submit was
        # parked in a slow injection (a permission ask, a slow dispatch);
        # without this, resuming here would silently overwrite _turn_worker
        # and turn_context and start a SECOND concurrent run_turn worker.
        if self._refuse_if_busy():
            return
        self.kernel.loop.set_turn_context(context)
        self._turn_worker = self.run_worker(
            self._run_turn(text), group="agent", exit_on_error=False
        )

    async def _inject_mentions(self, text: str) -> list[Message]:
        """Dispatch a read_file call for each @-mention in text through
        kernel.loop.dispatcher -- evented and permission-gated exactly like a
        model-initiated read. A denial is shown (turn still runs on the literal
        text); a missing/unreadable path or an unregistered read tool (native
        tools off) passes through silently as plain text (no error)."""
        seen: set[str] = set()
        tokens: list[str] = []
        for raw in _MENTION_TOKEN_RE.findall(text):
            token = raw.rstrip(_MENTION_TRAILING_PUNCT)
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
        if not tokens:
            return []
        dispatcher = self.kernel.loop.dispatcher
        blocks: list[str] = []
        for token in tokens:
            outcome = await dispatcher.dispatch_tool(
                ProposedToolCall(
                    call_id=new_call_id(), tool=ToolName("read_file"), args={"file_path": token}
                )
            )
            if outcome.is_error:
                reason = outcome.text or ""
                # dispatch_tool's own execution-failure text is always prefixed
                # "tool error: " (missing file, not-a-tool, ...) -- anything
                # else here is a policy denial (Block reason / "denied by
                # user" / a permission-channel error), which contract (b)
                # requires to be visible even though the turn still runs.
                if not reason.startswith("tool error:"):
                    self.say("! ", f"@{token}: {reason}")
                continue
            content = outcome.text
            if content is None and outcome.blob is not None:
                content = self.kernel.session.blobs.get(outcome.blob).decode(
                    "utf-8", errors="replace"
                )
            content = content or ""
            encoded = content.encode()
            if len(encoded) > _MENTION_CONTEXT_CAP:
                content = encoded[:_MENTION_CONTEXT_CAP].decode("utf-8", errors="replace")
                content += f"\n... [truncated to {_MENTION_CONTEXT_CAP} bytes]"
            blocks.append(f"--- @{token} ---\n{content}")
        if not blocks:
            return []
        return [
            Message.system_text(
                "Context from @-mentions in the user's message:\n\n" + "\n\n".join(blocks)
            )
        ]

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
        self.query_one("#transcript", RichLog).write(self._render_reply(reply))
        if self._panel is not None:
            self._panel.refresh_files_and_agents()

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
        self._refresh_statusbar()  # history shrank to 1 message -- ctx% moves

    def _preflight_resume(self, session_id: "SessionId") -> "str | None":
        """Lock-liveness + log-readability check for `session_id`, run BEFORE
        the current kernel is torn down (I-1): a locked or unreadable target
        must refuse here, with the current session untouched, rather than
        being discovered only after _rebuild_kernel_body has already closed
        it. May clear an already-stale lock or quarantine an already-torn
        tail -- exactly what resume_session's own reopen would do a moment
        later -- but a LIVE lock is never touched. Returns None when it's
        safe to proceed, else a human-readable reason."""
        from harness.log import SessionLockedError, TornLogError, read_session
        from harness.resume import _clear_stale_lock

        base = self.kernel.session.base
        try:
            _clear_stale_lock(base, session_id)
            read_session(base, session_id, repair=True)
        except (SessionLockedError, TornLogError, OSError) as exc:
            return str(exc)
        return None

    async def _run_resume(self) -> None:
        """/resume: pick a prior session and rebuild the kernel onto it.
        Runs as its own worker (see the /resume dispatch) since
        push_screen_wait needs one. Excludes the CURRENT session from the
        picker -- resuming into the session you're already in is a
        no-op-shaped trap, not a real choice.

        I-1: a pre-flight check runs BEFORE any teardown (see
        _preflight_resume) so a locked or unreadable target refuses cleanly
        with the current session still functional. Belt-and-braces: the
        rebuild itself is still wrapped below -- if it fails AFTER the old
        kernel is already torn down (anything the pre-flight didn't catch),
        fall back to a fresh session rather than leaving the app stuck on a
        closed kernel."""
        current_id = self.kernel.session.id
        # list_sessions fully reads+parses every session log under base --
        # blocking; off the event loop via an executor (I-3) so a large
        # sessions directory can't stall the whole app while /resume builds
        # its picker.
        all_sessions = await asyncio.get_running_loop().run_in_executor(
            None, list_sessions, self.kernel.session.base
        )
        sessions = [s for s in all_sessions if s.session_id != current_id]
        if not sessions:
            self.say("! ", "no sessions to resume")
            return
        chosen = await self.push_screen_wait(SessionPickerScreen(sessions))
        if chosen is None:
            return
        preflight_error = self._preflight_resume(chosen)
        if preflight_error is not None:
            self.say("! ", f"cannot resume {chosen}: {preflight_error}")
            return
        self._clear_live()
        self.query_one("#transcript", RichLog).clear()
        try:
            await self._rebuild_kernel(resume_session_id=chosen)
        except Exception as exc:
            self.say(
                "! ",
                f"resume of {chosen} failed ({exc}) -- falling back to a fresh session",
            )
            try:
                await self._rebuild_kernel()
            except Exception as exc2:
                # The fallback is itself a build_kernel call and can fail too
                # -- this must not escape silently (_run_resume runs as a
                # worker with exit_on_error=False) and leave app.kernel
                # pointing at the already-torn-down kernel from the first
                # attempt with no visible sign anything is wrong.
                self.say(
                    "! ",
                    f"resume failed: {exc}; fresh-session fallback also failed: {exc2}"
                    " -- restart the app",
                )
                return
            self.say("", f"started fresh session {self.kernel.session.id}")
            return
        self.say("", f"resumed session {self.kernel.session.id}")

    async def _run_command(self, command: SlashCommand) -> None:
        if command.name == "help":
            self.say(
                "",
                "/help  /model [alias]  /thoughts [collapse|full|off]  /markdown [on|off]  "
                "/clear  /compact  /resume  /panel  /tools  /quit  — @path mentions a file "
                "(Tab completes), read for the model only; F2 also toggles the activity panel",
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
            # parked-5: gated on _rebuild_in_progress ONLY -- the old kernel may
            # be mid-teardown or the new one mid-startup, same reasoning as the
            # other rebuild guards. Deliberately NOT the full _refuse_if_busy():
            # /model stays allowed mid-TURN (unchanged; a turn finishes its
            # current dispatch with the old alias and picks the new one up next).
            if self._rebuild_in_progress:
                self.say("! ", "a session rebuild is in progress -- try again in a moment")
                return
            # catalog.resolve lazily imports litellm (seconds) -- never block the
            # message handler; the switch applies on the next dispatch anyway
            self.run_worker(self._switch_model(command.arg), group="driver", exit_on_error=False)
        elif command.name == "thoughts":
            self._set_thought_mode(command.arg.strip())
        elif command.name == "markdown":
            self._set_markdown_mode(command.arg.strip())
        elif command.name == "clear":
            if self._refuse_if_busy():
                return
            await self._rebuild_kernel()
            self._clear_live()
            self.query_one("#transcript", RichLog).clear()
            self.say("", f"cleared -- new session {self.kernel.session.id}")
        elif command.name == "compact":
            if self._refuse_if_busy():
                return
            # NOT _turn_worker (item 8): /compact gets its own cancellation
            # path via action_interrupt/_after_compact_interrupt, so Esc
            # here never routes through loop.interrupt_turn()'s UserInterrupt.
            self._compact_worker = self.run_worker(
                self._run_compact(), group="agent", exit_on_error=False
            )
        elif command.name == "resume":
            if self._refuse_if_busy():
                return
            # push_screen_wait must run from a worker (Textual requirement) --
            # unlike /clear and /compact, this command needs a modal answer
            # before it can act, so the picker + rebuild both live in one.
            self.run_worker(self._run_resume(), group="driver", exit_on_error=False)
        elif command.name == "panel":
            # Deliberately NOT gated by _refuse_if_busy: toggling a UI panel
            # is harmless mid-turn (same reasoning as /quit above).
            self._toggle_panel()
        elif command.name in self._plugin_commands:
            body = self._plugin_commands[command.name].body
            prompt = body.replace("$ARGUMENTS", command.arg)
            if not prompt.strip():
                self.say("! ", f"command /{command.name} is empty after expansion")
                return
            # A plugin command IS a turn: route through the same guard as plain input.
            # (@file mentions deliberately do NOT expand inside command bodies v1 --
            # the body is the plugin author's text.)
            if self._refuse_if_busy():
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

    def _set_markdown_mode(self, arg: str) -> None:
        if not arg:
            self.say("", f"markdown mode: {'on' if self._markdown_mode else 'off'} (on|off)")
        elif arg in self._MARKDOWN_MODES:
            self._markdown_mode = arg == "on"
            self.say("", f"markdown mode -> {arg}")
        else:
            self.say(
                "! ",
                f"unknown markdown mode: {arg!r} (valid: {', '.join(self._MARKDOWN_MODES)})",
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
            from harness.provider_antigravity import AntigravityProvider
            from harness.provider_claude_code import ClaudeCodeProvider
            from harness.provider_codex import CodexProvider

            # Upgrading out of echo mode (no --model): match cli.py's
            # construction — backend entries must be dispatchable — and swap
            # via the kernel so every holder (loop, subagent runner, kernel)
            # gets the new provider, not just loop.provider.
            self.kernel.set_provider(
                CatalogProvider(
                    catalog,
                    claude_code=ClaudeCodeProvider(),
                    codex=CodexProvider(),
                    antigravity=AntigravityProvider(),
                )
            )
        # Unpinned subagents inherit the session's CURRENT model — the build-time
        # default (in echo mode not even a catalog alias) would dispatch experts
        # to the wrong place. Pricing lookups follow the fresh snapshot too.
        self.kernel.runner.default_model = ModelId(alias)
        self.kernel.runner.pricing = loop.pricing
        self.kernel.runner.pricing_for = loop.pricing_for = _make_pricing_for(catalog)
        self.say("", f"model → {alias} ({resolved.route})")
        self._maybe_warn_context(resolved)
        self._refresh_statusbar()

    def action_interrupt(self) -> None:
        # The priority Esc binding preempts modal bindings: with a permission
        # modal up, Esc means "deny this ask", not "kill the turn".
        if isinstance(self.screen, PermissionScreen):
            self.screen.action_answer("deny")
            return
        if isinstance(self.screen, SessionPickerScreen):
            self.screen.dismiss(None)
            return
        if self._rebuild_in_progress:
            # The picker is already dismissed by the time a rebuild starts,
            # so neither branch above catches this: a kernel swap mid-flight
            # (loop.end/mcp.stop/mcp.start) is not safe to cancel, so Esc is
            # a no-op here rather than tearing down a half-built kernel.
            return
        # item 8: /compact's own worker takes priority over _turn_worker --
        # it's tracked separately precisely so Esc during a /compact never
        # routes through _after_interrupt's loop.interrupt_turn() (a
        # UserInterrupt would be a false fact for an internal admin call).
        compact_worker = self._compact_worker
        if compact_worker is not None and not compact_worker.is_finished and not self._interrupting:
            self._interrupting = True
            compact_worker.cancel()
            self.run_worker(
                self._after_compact_interrupt(compact_worker), group="driver", exit_on_error=False
            )
            return
        worker = self._turn_worker
        if worker is None or worker.is_finished or self._interrupting:
            return
        # once per logical interrupt -- an invariant, not a timing bet: a second
        # Esc in the same tick still sees is_finished=False, so the flag guards it.
        self._interrupting = True
        worker.cancel()
        self.run_worker(self._after_interrupt(worker), group="driver", exit_on_error=False)

    async def _after_compact_interrupt(self, worker) -> None:
        """Esc-during-/compact's own cancellation path (item 8): cancels the
        summarize call cleanly withOUT loop.interrupt_turn()'s UserInterrupt
        -- /compact never touched loop.history yet at cancellation time (it
        only replaces it on a SUCCESSFUL summarize, after this worker would
        already be finished), so there's nothing to repair either."""
        try:
            try:
                await worker.wait()
            except (WorkerCancelled, WorkerFailed):
                pass
            self.say("! ", "compact cancelled")
        finally:
            self._interrupting = False

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
