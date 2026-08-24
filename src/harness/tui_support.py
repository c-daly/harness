"""Pure TUI logic - importable and testable without Textual.

TuiResolver is the decision-provider half of the TUI: the dispatcher awaits
resolve() while a turn is suspended; the injected ask-callable renders the
prompt (a Textual modal in production, a stub in tests)."""

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from harness.hooks import ProposedModelCall, ProposedToolCall
from harness.interaction import PermissionRequest
from harness.permissions import PermissionEngine

Answer = str


class HistoryRing:
    """Input history with shell-like up/down semantics.

    Known v1 gap: prev() does not stash the unsaved draft - walking up from a
    half-typed line and back down loses it."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._idx: int | None = None

    def remember(self, line: str) -> None:
        if line and (not self._items or self._items[-1] != line):
            self._items.append(line)
        self._idx = None

    def prev(self, current: str) -> str:
        if not self._items:
            return current
        if self._idx is None:
            self._idx = len(self._items) - 1
        elif self._idx > 0:
            self._idx -= 1
        return self._items[self._idx]

    def next(self, current: str) -> str:
        if self._idx is None:
            return current
        if self._idx < len(self._items) - 1:
            self._idx += 1
            return self._items[self._idx]
        self._idx = None
        return ""


@dataclass(frozen=True)
class SlashCommand:
    name: str
    arg: str


def parse_slash_command(text: str) -> SlashCommand | None:
    if not text.startswith("/") or len(text) < 2:
        return None
    name, _, arg = text[1:].partition(" ")
    if not name:
        return None
    return SlashCommand(name=name, arg=arg.strip())


def grant_pattern(request: PermissionRequest) -> tuple[str, dict[str, str]]:
    """The (tool, match) an [a]lways answer records. Arg-aware so [a] on bash never persists
    allow-all-bash (L5): bash -> first-token prefix; write/edit -> workspace path glob."""
    action = request.action
    if isinstance(action, ProposedModelCall):
        return f"model:{action.model}", {}
    if isinstance(action, ProposedToolCall):
        tool = str(action.tool)
        if tool == "bash":
            command = str(action.args.get("command", "")).strip()
            first = command.split()[0] if command.split() else ""
            return "bash", {"command": f"{first} *"} if first else {}
        if tool in ("write_file", "edit_file"):
            fp = str(action.args.get("file_path", ""))
            parent = fp.rsplit("/", 1)[0] if "/" in fp else ""
            return tool, {"file_path": f"{parent}/*"} if parent else {}
        return tool, {}
    raise TypeError(f"unknown action: {action!r}")


@dataclass
class TuiResolver:
    ask: Callable[[PermissionRequest], Awaitable[Answer]]
    engine: PermissionEngine | None = None
    name: str = "tui"
    #: Audit trail for tests/UI - unbounded by design (one per permission prompt).
    answers_seen: list[Answer] = field(default_factory=list)

    async def resolve(self, request: PermissionRequest) -> bool:
        answer = await self.ask(request)
        self.answers_seen.append(answer)
        if answer == "always":
            if self.engine is not None:
                import os

                tool, match = grant_pattern(request)
                # bash grants are session-scoped unless explicitly opted into persistence (L5);
                # workspace-qualified path grants are safe to persist.
                persist = (tool != "bash") or os.environ.get("HARNESS_PERSIST_GRANTS") == "1"
                if match:
                    self.engine.grant(tool, match, persist=persist)
                else:
                    # empty match = allow-all-tool; never persist that from a single keystroke
                    self.engine.grant(tool, None, persist=False)
            return True
        return answer == "allow"
