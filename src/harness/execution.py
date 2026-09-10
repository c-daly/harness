"""Core execution scope: cumulative authority and limits for one live session tree."""

from contextvars import ContextVar
from dataclasses import dataclass, field
import math
from typing import TYPE_CHECKING
from harness.resources import LocalResources
from harness.usage_budget import UsageBudget

if TYPE_CHECKING:
    from harness.context import ContextPolicy
    from harness.session import Session
    from harness.tools import FilteredRegistry, ToolRegistry


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionLimits:
    max_model_calls: int = 1024
    max_tool_calls: int = 4096
    max_children: int = 128
    max_depth: int = 4
    max_active_children: int = 16
    max_active_coordinators: int = 16
    coordination_timeout_seconds: float = 600.0

    def __post_init__(self):
        for name, value in vars(self).items():
            if name == "coordination_timeout_seconds":
                if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                    raise ValueError(f"{name} must be positive and finite")
                continue
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")


@dataclass
class ExecutionBudget:
    """Reservations are synchronous on the owning event loop; descendants share this object.

    Counts never refund work. Active-child capacity is released on all exits;
    exhaustion rejects a spawn rather than deadlocking ancestors waiting on
    descendants. Pure coordinators have separate active capacity but consume
    the same cumulative descendant and depth limits. Usage stop limits use a
    durable ledger shared by this tree.
    """

    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    model_calls: int = 0
    tool_calls: int = 0
    children: int = 0
    active_children: int = 0
    active_coordinators: int = 0
    usage: UsageBudget = field(default_factory=UsageBudget)

    @property
    def busy(self) -> bool:
        return bool(self.active_children or self.active_coordinators)

    def reserve_call(self, kind: str) -> None:
        if kind not in ("model", "tool"):
            raise ValueError("unknown execution kind")
        counter = f"{kind}_calls"
        limit = getattr(self.limits, f"max_{counter}")
        if getattr(self, counter) >= limit:
            raise BudgetExceeded(f"root execution budget: {counter} limit ({limit}) reached")
        setattr(self, counter, getattr(self, counter) + 1)

    def reserve_child(self, depth: int) -> None:
        self._reserve_descendant(depth, coordinator=False)

    def reserve_coordinator(self, depth: int) -> None:
        self._reserve_descendant(depth, coordinator=True)

    def _reserve_descendant(self, depth: int, *, coordinator: bool) -> None:
        if depth > self.limits.max_depth:
            raise BudgetExceeded(f"root execution budget: depth limit ({self.limits.max_depth}) reached")
        if self.children >= self.limits.max_children:
            raise BudgetExceeded(f"root execution budget: child limit ({self.limits.max_children}) reached")
        counter = "active_coordinators" if coordinator else "active_children"
        if getattr(self, counter) >= getattr(self.limits, f"max_{counter}"):
            label = "coordinator" if coordinator else "child"
            raise BudgetExceeded(f"root execution budget: active {label} capacity exhausted")
        self.children += 1
        setattr(self, counter, getattr(self, counter) + 1)

    def release_child(self) -> None:
        self.active_children -= 1

    def release_coordinator(self) -> None:
        self.active_coordinators -= 1


@dataclass(frozen=True)
class ExecutionScope:
    session: "Session"
    registry: "ToolRegistry | FilteredRegistry"
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    depth: int = 0
    resources: LocalResources = field(default_factory=LocalResources)
    context_policy: "ContextPolicy | None" = None


current_scope: ContextVar[ExecutionScope | None] = ContextVar("harness_execution_scope", default=None)
current_model_call_id: ContextVar[str | None] = ContextVar("harness_model_call_id", default=None)
