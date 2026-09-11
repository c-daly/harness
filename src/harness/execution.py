"""Core execution scope: cumulative authority and limits for one live session tree."""

from contextvars import ContextVar
from dataclasses import dataclass, field, fields
import math
from typing import TYPE_CHECKING
from harness.resources import LocalResources
from harness.usage_budget import UsageBudget
from harness.execution_counts import ExecutionLedger
from harness.activity import ActivityTracker
from harness.run_budgets import RunBudgets
from harness.run_controls import RunControls

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
    task_timeout_seconds: float = 600.0
    inference_timeout_seconds: float = 120.0

    @classmethod
    def from_record(cls, values: dict) -> "ExecutionLimits":
        """Project supported limits from an additive, forward-compatible record.

        Missing or invalid known fields are corruption, not permission to use
        defaults. New fields belong to newer readers and remain in the event.
        """
        names = {f.name for f in fields(cls)}
        if not names <= values.keys():
            raise ValueError("execution configuration must record every supported limit")
        return cls(**{name: values[name] for name in names})

    def __post_init__(self):
        for name, value in vars(self).items():
            if name.endswith("_timeout_seconds"):
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
    durable ledger shared by this tree. Admission totals are also root-owned
    and durable; active capacity is never restored as live work.
    """

    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    ledger: ExecutionLedger = field(default_factory=ExecutionLedger)
    active_children: int = 0
    active_coordinators: int = 0
    usage: UsageBudget = field(default_factory=UsageBudget)
    activity: ActivityTracker = field(default_factory=ActivityTracker)
    runs: RunBudgets = field(default_factory=RunBudgets)
    controls: RunControls = field(default_factory=RunControls)

    @property
    def model_calls(self):
        return self.ledger.state.model_calls

    @property
    def tool_calls(self):
        return self.ledger.state.tool_calls

    @property
    def children(self):
        return self.ledger.state.children

    def attach(self, session):
        if session._execution_budget is not None and session._execution_budget is not self:
            raise ValueError("session already has an execution budget; share its execution scope")
        self.ledger.attach(session)
        session._execution_budget = self
        self.controls.attach(self.ledger.session)

    @property
    def busy(self) -> bool:
        return bool(self.active_children or self.active_coordinators)

    def reserve_call(self, kind: str, *, session=None, call_id=None) -> None:
        self.controls.check_active()
        if kind not in ("model", "tool"):
            raise ValueError("unknown execution kind")
        if session is not None:
            self.attach(session)
        counter = f"{kind}_calls"
        limit = getattr(self.limits, f"max_{counter}")
        if getattr(self, counter) >= limit:
            raise BudgetExceeded(f"root execution budget: {counter} limit ({limit}) reached")
        self.ledger.reserve(kind, session, call_id=call_id)

    def reserve_child(self, depth: int, *, session=None, call_id=None) -> None:
        self._reserve_descendant(depth, coordinator=False, session=session, call_id=call_id)

    def reserve_coordinator(self, depth: int, *, session=None, call_id=None) -> None:
        self._reserve_descendant(depth, coordinator=True, session=session, call_id=call_id)

    def _reserve_descendant(self, depth: int, *, coordinator: bool, session=None, call_id=None) -> None:
        self.controls.check_active()
        if session is not None:
            self.attach(session)
        if depth > self.limits.max_depth:
            raise BudgetExceeded(f"root execution budget: depth limit ({self.limits.max_depth}) reached")
        if self.children >= self.limits.max_children:
            raise BudgetExceeded(f"root execution budget: child limit ({self.limits.max_children}) reached")
        counter = "active_coordinators" if coordinator else "active_children"
        if getattr(self, counter) >= getattr(self.limits, f"max_{counter}"):
            label = "coordinator" if coordinator else "child"
            raise BudgetExceeded(f"root execution budget: active {label} capacity exhausted")
        self.ledger.reserve("coordinator" if coordinator else "child", session, call_id=call_id)
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
current_agent_timeout: ContextVar[float | None] = ContextVar("harness_agent_timeout", default=None)


def agent_timeout(configured: float | None) -> float:
    """Default process timers follow the owned call; explicit adapter caps remain.

    A context-local binding avoids mutating a provider shared by concurrent
    children. Standalone adapter calls keep the historical 600 second default.
    """
    owned = current_agent_timeout.get()
    if owned is None:
        return configured if configured is not None else 600.0
    return min(configured, owned) if configured is not None else owned
