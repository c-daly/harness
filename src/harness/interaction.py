"""The interaction channel: the one place a suspended dispatch waits for a human.

The loop blocks here; subscribers never do. The TUI implements Resolver in a
later phase; HeadlessResolver (deny) keeps `-p` mode safe by default.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from harness.hooks import ProposedAction
from harness.types import CallId


@dataclass(frozen=True)
class PermissionRequest:
    call_id: CallId
    action: ProposedAction
    reason: str


@runtime_checkable
class Resolver(Protocol):
    name: str

    async def resolve(self, request: PermissionRequest) -> bool: ...


class HeadlessResolver:
    name = "headless-deny"

    async def resolve(self, request: PermissionRequest) -> bool:
        return False


@dataclass
class ScriptedResolver:
    answers: list[bool]
    seen: list[PermissionRequest] = field(default_factory=list)
    name: str = "scripted"

    async def resolve(self, request: PermissionRequest) -> bool:
        self.seen.append(request)
        return self.answers.pop(0)
