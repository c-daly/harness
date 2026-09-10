"""Root-owned, durable usage accounting and admission stop limits.

Admission uses settled usage, not an invented token estimate. Already admitted
calls may cross a limit. Incomplete accounting holds subsequent bounded work.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import math
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from harness.provider import Usage


class UsageBudgetConfigError(ValueError):
    pass


class UsageLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_cost_usd: float | None = Field(default=None, ge=0, strict=True, allow_inf_nan=False)

    @property
    def enabled(self):
        return any(v is not None for v in self.model_dump().values())

    def narrow(self, requested):
        """Omission never clears a stored limit; resumption can only narrow it."""
        values = {}
        for key, old in self.model_dump().items():
            new = getattr(requested, key)
            if old is not None and new is not None and new > old:
                raise UsageBudgetConfigError(f"{key} cannot increase a stored usage limit; start a new session")
            values[key] = old if new is None else new
        return UsageLimits(**values)


def valid_pricing(pricing):
    keys = ("input_cost_per_token", "output_cost_per_token")
    return (isinstance(pricing, dict) and all(type(pricing.get(k)) in (int, float)
            and math.isfinite(pricing[k]) and pricing[k] >= 0 for k in keys))


@dataclass
class UsageState:
    limits: UsageLimits = field(default_factory=UsageLimits)
    configured: bool = False
    root_session_id: str | None = None
    attempts: dict = field(default_factory=dict)
    finished: set = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    unknown_input: int = 0
    unknown_output: int = 0
    unknown_cost: int = 0
    last_stop: str | None = None

    @property
    def pending(self):
        return self.attempts.keys() - self.finished

    def apply(self, event):
        from harness.events import (UsageAttemptFinished, UsageAttemptStarted,
                                    UsageBudgetBlocked, UsageBudgetConfigured, UsageBudgetLinked)
        if isinstance(event, UsageBudgetConfigured):
            if self.configured and self.limits.narrow(event.limits) != event.limits:
                raise ValueError("stored usage limits cannot be removed")
            self.limits = event.limits
            self.configured = True
            self.unknown_input += event.untracked_prior_work
            self.unknown_output += event.untracked_prior_work
            self.unknown_cost += event.untracked_prior_work
        elif isinstance(event, UsageBudgetLinked):
            self.root_session_id = str(event.root_session_id)
        elif isinstance(event, UsageAttemptStarted):
            if event.id in self.attempts:
                raise ValueError("duplicate usage attempt")
            self.attempts[event.id] = event
        elif isinstance(event, UsageAttemptFinished):
            if event.id not in self.attempts or event.id in self.finished:
                raise ValueError("usage settlement has no unique pending attempt")
            self.finished.add(event.id)
            self.input_tokens += event.input_tokens or 0
            self.output_tokens += event.output_tokens or 0
            self.unknown_input += int(not event.complete or event.input_tokens is None)
            self.unknown_output += int(not event.complete or event.output_tokens is None)
            prices = self.attempts[event.id].pricing
            known_cost = valid_pricing(prices) and event.input_tokens is not None and event.output_tokens is not None
            if known_cost:
                self.cost_usd += (Decimal(str(prices["input_cost_per_token"])) * event.input_tokens
                                 + Decimal(str(prices["output_cost_per_token"])) * event.output_tokens)
            self.unknown_cost += int(not event.complete or not known_cost)
        elif isinstance(event, UsageBudgetBlocked):
            self.last_stop = event.reason


def project_usage(events):
    state = UsageState()
    for envelope in events:
        state.apply(envelope.event)
    return state


@dataclass
class UsageBudget:
    limits: UsageLimits = field(default_factory=UsageLimits)
    session: object = None
    state: UsageState = field(default_factory=UsageState)
    linked: set = field(default_factory=set)
    healthy: bool = True

    def attach(self, session):
        """Bind once to the root; descendants record only the ownership link."""
        from harness.events import UsageBudgetConfigured, UsageBudgetLinked
        if self.session is None:
            self.session = session
        if session is not self.session:
            if session.id not in self.linked:
                session.append(UsageBudgetLinked(root_session_id=self.session.id))
                self.linked.add(session.id)
            return
        if not self.state.configured:
            self._record(UsageBudgetConfigured(limits=self.limits))

    def _record(self, event):
        try:
            recorded = self.session.append(event)
            if recorded.event != event:
                raise ValueError("usage accounting records cannot be rewritten")
            self.state.apply(event)
        except BaseException:
            self.healthy = False
            raise

    @classmethod
    def restore(cls, session, requested=None):
        from harness.events import (ModelCallStarted, SubagentSpawned, UsageAttemptFinished,
                                    UsageBudgetConfigured)
        from harness.log import read_session
        events = read_session(session.base, session.id, repair=False) if session._seq else []
        state = project_usage(events)
        if state.root_session_id is not None:
            raise UsageBudgetConfigError(f"shared usage accounting belongs to session {state.root_session_id}; resume that root session")
        limits = state.limits.narrow(requested or UsageLimits())
        budget = cls(limits=limits, session=session, state=state)
        if session._seq:
            if not state.configured or limits != state.limits:
                prior = int(not state.configured and any(isinstance(e.event, (ModelCallStarted, SubagentSpawned))
                                                         for e in events))
                budget._record(UsageBudgetConfigured(limits=limits, untracked_prior_work=prior))
            for attempt in sorted(state.pending):
                budget._record(UsageAttemptFinished(id=attempt, complete=False, status="aborted"))
        return budget

    def begin(self, session, *, call_id, model, purpose, attempt, pricing, accounting):
        from harness.events import UsageAttemptStarted, UsageBudgetBlocked
        from harness.execution import BudgetExceeded
        if not self.healthy:
            raise BudgetExceeded("root usage budget: accounting write failed; restart to reconcile the ledger")
        self.attach(session)
        reason = None
        for dimension, observed, unknown in (
            ("input_tokens", self.state.input_tokens, self.state.unknown_input),
            ("output_tokens", self.state.output_tokens, self.state.unknown_output),
            ("cost_usd", self.state.cost_usd, self.state.unknown_cost),
        ):
            limit = getattr(self.limits, f"max_{dimension}")
            if limit is None:
                continue
            if unknown:
                reason = f"{dimension} accounting is incomplete; inspect /budget before starting a new session"
            elif observed >= Decimal(str(limit)):
                reason = f"{dimension} stop limit ({limit}) reached"
            elif accounting != "reported":
                reason = 'route has no declared usage accounting; select an alias with usage_accounting = "reported"'
            elif dimension == "cost_usd" and not valid_pricing(pricing):
                reason = "cost limit requires finite nonnegative input and output token prices"
            if reason:
                break
        if reason:
            self._record(UsageBudgetBlocked(source_session_id=session.id, call_id=call_id, reason=reason))
            raise BudgetExceeded(f"root usage budget: {reason}")
        identifier = uuid4().hex
        self._record(UsageAttemptStarted(id=identifier, source_session_id=session.id,
                     call_id=call_id, model=model, purpose=purpose, attempt=attempt,
                     pricing=dict(pricing) if valid_pricing(pricing) else {}))
        return identifier

    def finish(self, identifier, usage: Usage, *, status: Literal["completed", "failed", "cancelled"], complete=None):
        from harness.events import UsageAttemptFinished
        self._record(UsageAttemptFinished(id=identifier, input_tokens=usage.input_tokens,
                     output_tokens=usage.output_tokens, complete=status == "completed" if complete is None else complete,
                     status=status))
