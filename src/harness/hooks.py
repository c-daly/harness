"""Two-family hook contract: dispatch hooks decide, lifecycle hooks contribute."""

import asyncio
import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping

from harness.types import CallId, ModelId, ToolName

# --- proposed actions ---

@dataclass(frozen=True)
class ProposedToolCall:
    call_id: CallId
    tool: ToolName
    args: Mapping[str, Any]


@dataclass(frozen=True)
class ProposedModelCall:
    call_id: CallId
    model: ModelId


ProposedAction = ProposedToolCall | ProposedModelCall

# --- dispatch decisions ---

@dataclass(frozen=True)
class Block:
    reason: str


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Rewrite:
    action: ProposedAction


@dataclass(frozen=True)
class Ask:
    reason: str


DispatchDecision = Block | Allow | Rewrite | Ask

# --- lifecycle contributions ---

class LifecyclePoint(StrEnum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PROMPT_SUBMIT = "prompt_submit"
    POST_TOOL = "post_tool"
    PRE_COMPACTION = "pre_compaction"


@dataclass(frozen=True)
class Inject:
    text: str


@dataclass(frozen=True)
class Annotate:
    note: str


@dataclass(frozen=True)
class Emit:
    namespace: str
    name: str
    data: Mapping[str, Any]


LifecycleContribution = Inject | Annotate | Emit


def decision_to_payload(decision: DispatchDecision) -> dict[str, Any]:
    """Serialize a decision for the HookDecided event -- rewrites carry the full call body
    so the executed call is always reconstructable from events alone."""
    match decision:
        case Block(reason=reason):
            return {"kind": "block", "reason": reason}
        case Allow():
            return {"kind": "allow"}
        case Ask(reason=reason):
            return {"kind": "ask", "reason": reason}
        case Rewrite(action=ProposedToolCall(tool=tool, args=args)):
            return {"kind": "rewrite", "tool": str(tool), "args": dict(args)}
        case Rewrite(action=ProposedModelCall(model=model)):
            return {"kind": "rewrite", "model": str(model)}
    raise TypeError(f"unknown decision: {decision!r}")


@dataclass(frozen=True)
class ChainOutcome:
    decisions: tuple[tuple[str, DispatchDecision], ...]  # (hook name, decision) per hook run
    effective: ProposedAction | None  # post-rewrites; None iff blocked
    blocked: Block | None
    ask: Ask | None  # first Ask in chain order (highest priority); every Ask is in decisions


_DispatchFn = Callable[[ProposedAction], DispatchDecision | Awaitable[DispatchDecision]]
_LifecycleFn = Callable[[Any], Any]


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class HookBus:
    """Hook registry + chain runner.

    Dispatch hooks fail CLOSED: a timeout or exception becomes Block with a
    structurally distinct reason, recorded like any decision -- a buggy plugin
    hook can never crash a session or silently permit a call. Lifecycle hooks
    fail OPEN (skip + warning). Timeouts cancel the in-flight hook coroutine
    (asyncio.wait_for): hooks holding resources must release them in finally.
    register_*_async are aliases of their sync counterparts; both accept sync
    or async callables.
    """

    def __init__(self, *, dispatch_timeout: float = 10.0, lifecycle_timeout: float = 10.0) -> None:
        self._dispatch: list[tuple[int, int, str, _DispatchFn]] = []
        self._lifecycle: dict[LifecyclePoint, list[tuple[str, _LifecycleFn]]] = {}
        self._counter = 0
        self._dispatch_timeout = dispatch_timeout
        self._lifecycle_timeout = lifecycle_timeout

    # sync-callable registration sugar; async variants for explicitness in tests
    def register_dispatch(self, name: str, fn: _DispatchFn, *, priority: int = 100) -> None:
        self._dispatch.append((priority, self._counter, name, fn))
        self._counter += 1
        self._dispatch.sort(key=lambda t: (t[0], t[1]))

    register_dispatch_async = register_dispatch

    def register_lifecycle(self, name: str, point: LifecyclePoint, fn: _LifecycleFn) -> None:
        self._lifecycle.setdefault(point, []).append((name, fn))

    register_lifecycle_async = register_lifecycle

    async def run_dispatch(self, action: ProposedAction) -> ChainOutcome:
        decisions: list[tuple[str, DispatchDecision]] = []
        current = action
        ask: Ask | None = None
        for _, _, name, fn in list(self._dispatch):
            try:
                decision = await asyncio.wait_for(
                    _maybe_await(fn(current)), timeout=self._dispatch_timeout
                )
            except TimeoutError:
                decision = Block(reason=f"hook {name!r} timed out (fail closed)")
            except Exception as exc:
                decision = Block(
                    reason=f"hook {name!r} raised {type(exc).__name__}: {exc} (fail closed)"
                )
            decisions.append((name, decision))
            match decision:
                case Block():
                    return ChainOutcome(tuple(decisions), None, decision, None)
                case Rewrite(action=new_action):
                    current = new_action
                case Ask():
                    if ask is None:
                        ask = decision  # first (highest-priority) Ask wins
                case Allow():
                    pass
        return ChainOutcome(tuple(decisions), current, None, ask)

    async def run_lifecycle(
        self, point: LifecyclePoint, ctx: Any
    ) -> tuple[tuple[LifecycleContribution, ...], list[str]]:
        contributions: list[LifecycleContribution] = []
        warnings: list[str] = []
        for name, fn in list(self._lifecycle.get(point, [])):
            try:
                result = await asyncio.wait_for(
                    _maybe_await(fn(ctx)), timeout=self._lifecycle_timeout
                )
            except TimeoutError:
                warnings.append(f"lifecycle hook {name!r} timed out (skipped)")
                continue
            except Exception as exc:  # lifecycle hooks fail open
                warnings.append(f"lifecycle hook {name!r} raised: {exc!r} (skipped)")
                continue
            if result:
                contributions.extend(result)
        return tuple(contributions), warnings
