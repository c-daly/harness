"""The tool call currently being executed, for tools that spawn.

The Tool protocol is ``__call__(args)`` -- no call id -- because almost no tool
needs one. The four that spawn subagents do: a spawn event must name the call
that caused it, or concurrent coordination calls become unattributable. The
dispatcher publishes the id here for the duration of the call rather than
widening the protocol for every tool.

asyncio semantics: each tool call in a turn runs in its own Task (loop.py's
gather over ``_run_one``), and a Task receives a copy of the context at
creation, so concurrent calls never observe each other's id. Spawns nested
inside a coordination tool correctly inherit that tool's id, which is exactly
the grouping the activity panel needs.
"""

from contextvars import ContextVar, Token

from harness.types import CallId

_current: ContextVar[CallId | None] = ContextVar("harness_current_call_id", default=None)


def current_call_id() -> CallId | None:
    """The call being executed in this context, or None outside a tool call."""
    return _current.get()


def set_current_call_id(call_id: CallId | None) -> Token:
    """Publish the current call id. Reset with the returned token in a finally."""
    return _current.set(call_id)


def reset_current_call_id(token: Token) -> None:
    _current.reset(token)
