"""Pure TUI logic - importable and testable without Textual.

TuiResolver is the decision-provider half of the TUI: the dispatcher awaits
resolve() while a turn is suspended; the injected ask-callable renders the
prompt (a Textual modal in production, a stub in tests)."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from harness.hooks import ProposedModelCall, ProposedToolCall
from harness.interaction import PermissionRequest
from harness.permissions import PermissionEngine

_MENTION_RE = re.compile(r"@(\S+)")

Answer = str


class HistoryRing:
    """Input history with shell-like up/down semantics."""

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


def expand_file_mentions(
    text: str, *, max_bytes: int = 32 * 1024
) -> tuple[str, list[str], list[str]]:
    """Expand @<path> mentions into fenced blocks appended to the prompt.

    Returns (expanded_text, attached_paths, errors). Any error means the
    caller should NOT send the prompt (errors name the offending path)."""
    attached: list[str] = []
    errors: list[str] = []
    blocks: list[str] = []
    for raw in _MENTION_RE.findall(text):
        path = Path(raw).expanduser()
        if not path.is_file():
            errors.append(f"@{raw}: no such file")
            continue
        size = path.stat().st_size
        if size > max_bytes:
            errors.append(f"@{raw}: {size} bytes exceeds the {max_bytes} byte cap")
            continue
        try:
            content = path.read_text(errors="replace")
        except OSError as exc:
            errors.append(f"@{raw}: {exc}")
            continue
        attached.append(str(path))
        blocks.append(f"\n\n[attached {path}, {size} bytes]\n```\n{content}\n```")
    if errors:
        return text, [], errors
    return text + "".join(blocks), attached, []


def grant_pattern(request: PermissionRequest) -> str:
    action = request.action
    if isinstance(action, ProposedToolCall):
        return str(action.tool)
    if isinstance(action, ProposedModelCall):
        return f"model:{action.model}"
    raise TypeError(f"unknown action: {action!r}")


@dataclass
class TuiResolver:
    ask: Callable[[PermissionRequest], Awaitable[Answer]]
    engine: PermissionEngine | None = None
    name: str = "tui"
    answers_seen: list[Answer] = field(default_factory=list)

    async def resolve(self, request: PermissionRequest) -> bool:
        answer = await self.ask(request)
        self.answers_seen.append(answer)
        if answer == "always":
            if self.engine is not None:
                self.engine.grant(grant_pattern(request), persist=True)
            return True
        return answer == "allow"
