"""Core execution scope: cumulative authority and limits for one live session tree."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from harness.resources import LocalResources

if TYPE_CHECKING:
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

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")


@dataclass
class ExecutionBudget:
    """Reservations are synchronous on the owning event loop; descendants share this object.

    Counts never refund work. Active-child capacity is released on all exits;
    exhaustion rejects a spawn rather than deadlocking ancestors waiting on
    descendants. Token/cost reservations need the inference contract in M2.
    """

    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    model_calls: int = 0
    tool_calls: int = 0
    children: int = 0
    active_children: int = 0

    def reserve_call(self, kind: str) -> None:
        if kind not in ("model", "tool"):
            raise ValueError("unknown execution kind")
        counter = f"{kind}_calls"
        limit = getattr(self.limits, f"max_{counter}")
        if getattr(self, counter) >= limit:
            raise BudgetExceeded(f"root execution budget: {counter} limit ({limit}) reached")
        setattr(self, counter, getattr(self, counter) + 1)

    def reserve_child(self, depth: int) -> None:
        if depth > self.limits.max_depth:
            raise BudgetExceeded(f"root execution budget: depth limit ({self.limits.max_depth}) reached")
        if self.children >= self.limits.max_children:
            raise BudgetExceeded(f"root execution budget: child limit ({self.limits.max_children}) reached")
        if self.active_children >= self.limits.max_active_children:
            raise BudgetExceeded("root execution budget: active child capacity exhausted")
        self.children += 1
        self.active_children += 1

    def release_child(self) -> None:
        self.active_children -= 1


@dataclass(frozen=True)
class ExecutionScope:
    session: "Session"
    registry: "ToolRegistry | FilteredRegistry"
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    depth: int = 0
    resources: LocalResources = field(default_factory=LocalResources)


current_scope: ContextVar[ExecutionScope | None] = ContextVar("harness_execution_scope", default=None)
