"""Opt-in local fallback before a native task has accepted its first response.

One prepared inference request is retried through dispatch. Task execution,
context acquisition and tool calls are never restarted by this service.
"""

import asyncio
import hashlib
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.errors import AuthFailed, LocalUnavailable, NetworkFailed, Overloaded, RateLimited
from harness.types import CallId, ModelId

Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")]
FAILURES = (NetworkFailed, AuthFailed, RateLimited, Overloaded, LocalUnavailable)


class FallbackPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # An empty list explicitly disables an inherited routing-layer policy.
    models: tuple[Alias, ...] = Field(default=(), max_length=3)
    required_tags: tuple[Alias, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.models)) != len(self.models) or len(set(self.required_tags)) != len(self.required_tags):
            raise ValueError("fallback aliases and tags must be unique")
        return self

    @property
    def digest(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class FallbackDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    run_id: str
    failed_call_id: CallId | None
    from_model: ModelId
    to_model: ModelId | None = None
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["selected", "skipped", "held"]
    reason: Literal[
        "network", "authentication", "rate_limit", "overloaded", "local_unavailable",
        "pinned", "reconciliation_required", "external_runtime", "unknown_failure",
        "unknown_alias", "not_local_inference", "missing_capability", "busy", "exhausted",
    ]


class TaskFallback:
    """One root run's finite candidate chain; selection never changes the user's pin."""
    def __init__(self, loop):
        self.loop = loop
        self.policy = loop.fallback_policy
        self.model = loop.model
        self.explicit_pin = loop.model_pinned
        self.selected = False
        self.failed_calls = 0
        self.tried = {str(self.model)}
        self.children_before = loop.dispatcher.scope.budget.children

    @property
    def pinned(self):
        return self.explicit_pin or self.selected

    def record(self, error, status, reason, target=None):
        from harness.agent import current_agent_run
        from harness.events import FallbackDecided
        active = current_agent_run.get()
        decision = FallbackDecision(task_id=active.task.id, run_id=active.run_id,
            failed_call_id=error.call_id, from_model=ModelId(error.model or self.model),
            to_model=ModelId(target) if target else None, policy_digest=self.policy.digest,
            status=status, reason=reason)
        self.loop.session.append(FallbackDecided(decision=decision))

    def hold_external(self, error):
        if self.policy is not None and self.policy.models:
            self.record(error, "held", "external_runtime")

    def has_work(self):
        from harness.agent import current_agent_run
        from harness.events import AgentRunStarted, ModelCallCompleted, ToolCallProposed
        from harness.log import read_session
        # Native subagents keep separate session logs. Their shared cumulative
        # reservation also covers child activity that is absent from this log.
        if self.loop.dispatcher.scope.budget.children != self.children_before:
            return True
        active = current_agent_run.get()
        for env in read_session(self.loop.session.base, self.loop.session.id, repair=False):
            event = env.event
            if isinstance(event, AgentRunStarted) and event.parent_run_id == active.run_id:
                return True
            if isinstance(event, ToolCallProposed) and event.agent_run_id == active.run_id and event.purpose != "context":
                return True
            if (isinstance(event, ModelCallCompleted) and event.agent_run_id == active.run_id
                    and event.purpose == "conversation"):
                return True
        return False

    def choose(self, error, *, before_work, tools):
        from harness.catalog import UnknownAliasError
        if self.explicit_pin:
            self.record(error, "held", "pinned")
            return None
        if not before_work or self.loop.dispatcher.scope.depth or self.has_work():
            self.record(error, "held", "reconciliation_required")
            return None
        if error.execution_kind != "inference":
            self.record(error, "held", "external_runtime")
            return None
        if error.call_id is None or error.model is None:
            self.record(error, "held", "unknown_failure")
            return None
        catalog = getattr(self.loop.provider, "catalog", None)
        if catalog is None:
            self.record(error, "held", "unknown_alias")
            return None
        self.tried.add(str(error.model))
        required = set(self.policy.required_tags) | ({"tools"} if tools else set())
        for alias in self.policy.models:
            if alias in self.tried:
                continue
            self.tried.add(alias)
            try:
                resolved = catalog.resolve(alias)
            except UnknownAliasError:
                self.record(error, "skipped", "unknown_alias", alias)
                continue
            if resolved.execution_kind != "inference" or resolved.local is None:
                self.record(error, "skipped", "not_local_inference", alias)
                continue
            if not required <= set(resolved.tags):
                self.record(error, "skipped", "missing_capability", alias)
                continue
            resource = self.loop.dispatcher.scope.resources.snapshot(resolved)
            if resource.status == "busy" and not resource.stale:
                self.record(error, "skipped", "busy", alias)
                continue
            reason = {NetworkFailed: "network", AuthFailed: "authentication", RateLimited: "rate_limit",
                      Overloaded: "overloaded", LocalUnavailable: "local_unavailable"}
            self.record(error, "selected", next(label for kind, label in reason.items() if isinstance(error, kind)), alias)
            self.model, self.selected = ModelId(alias), True
            self.loop.active_model = self.model
            return resolved
        self.record(error, "held", "exhausted")
        return None

    async def dispatch(self, request, *, before_work, on_chunk, on_switch):
        loop = self.loop

        async def attempt(current, pricing):
            if self.selected:
                return await loop.dispatcher.dispatch_inference(
                    provider=loop.provider, request=current, pricing=pricing,
                    pinned=True, exact_model=True, on_chunk=on_chunk)
            return await loop.dispatcher.dispatch_response(
                provider=loop.provider, request=current, pricing=loop.pricing,
                pricing_for=loop.pricing_for, pinned=self.pinned, on_chunk=on_chunk)

        if self.policy is None or not self.policy.models:
            return await attempt(request, loop.pricing)
        deadline = time.monotonic() + request.timeout_seconds
        pricing = getattr(loop.provider, "catalog", None)
        pricing = pricing.resolve(str(self.model)).pricing_dict() if self.selected and pricing else loop.pricing
        async with asyncio.timeout(request.timeout_seconds):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("fallback inference deadline exceeded")
                try:
                    return await attempt(request.model_copy(update={"model": self.model,
                        "timeout_seconds": remaining}), pricing)
                except FAILURES as exc:
                    self.failed_calls += 1
                    resolved = self.choose(exc, before_work=before_work, tools=request.tools)
                    if resolved is None:
                        raise
                    pricing = resolved.pricing_dict()
                    on_switch()


def render_fallback(decision):
    from harness.telemetry import _safe
    target = f" -> {decision.to_model}" if decision.to_model else ""
    reason = decision.reason.replace("_", " ")
    return _safe(f"Fallback {decision.status}: {decision.from_model}{target}; {reason}.")
