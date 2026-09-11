"""Subagents: child sessions, concurrent in-process, parent's enforcement applies."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.callctx import current_call_id
from harness.agent import AgentTask, DelegationResult, current_agent_run
from harness.events import ErrorRaised, SubagentFinished, SubagentSpawned
from harness.execution import BudgetExceeded, ExecutionScope, current_scope
from harness.frontmatter import AgentDef
from harness.hooks import HookBus
from harness.interaction import Resolver
from harness.loop import AgentLoop
from harness.provider import ModelProvider
from harness.session import Session
from harness.tasks import TaskRequirement, TaskService
from harness.tools import FilteredRegistry, ToolRegistry, ToolSpec
from harness.types import AgentId, ModelId, ToolName, new_session_id


_TRUNCATION_MARKER = "\n\u2026[truncated]"


def _bound(text: str, limit: int | None) -> str:
    """Cap what a child returns to its parent. None = unbounded."""
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


@dataclass
class SubagentRunner:
    base: Path
    provider: ModelProvider
    registry: ToolRegistry | FilteredRegistry
    hooks: HookBus
    resolver: Resolver
    default_model: ModelId
    pricing: dict[str, float] | None = None
    pricing_for: "Callable[[ModelId], dict[str, float]] | None" = None
    agents: dict[str, AgentDef] = field(default_factory=dict)
    _root_scopes: dict[str, ExecutionScope] = field(default_factory=dict, init=False, repr=False)

    def scope_for(self, parent: Session) -> ExecutionScope:
        """Resolve the actual caller and retain one budget for standalone calls."""
        scope = current_scope.get()
        if scope is None:
            if str(parent.id) not in self._root_scopes:
                budget = parent._execution_budget
                scope = (ExecutionScope(parent, self.registry) if budget is None
                         else ExecutionScope(parent, self.registry, budget))
                self._root_scopes[str(parent.id)] = scope
            scope = self._root_scopes[str(parent.id)]
        return scope

    async def run(
        self, *, prompt: str, model: ModelId | None, parent: Session, agent: str | None = None
    ) -> str:
        return (await self.run_result(prompt=prompt, model=model, parent=parent, agent=agent)).render()

    async def run_result(
        self, *, prompt: str, model: ModelId | None, parent: Session, agent: str | None = None,
        on_result: Callable[[DelegationResult], None] | None = None,
        requirements: tuple[TaskRequirement, ...] | None = None,
        requirement_title: str | None = None,
    ) -> DelegationResult:
        if requirements is not None:
            requirements = tuple(TaskRequirement.model_validate(r.model_dump()) for r in requirements)
        scope = self.scope_for(parent)
        parent = scope.session  # Includes calls through root-bound coordination tools.
        return await self._run_in_scope(prompt=prompt, model=model, parent=parent,
                                        agent=agent, scope=scope, on_result=on_result, requirements=requirements,
                                        requirement_title=requirement_title)

    async def _run_in_scope(
        self, *, prompt: str, model: ModelId | None, parent: Session,
        agent: str | None, scope: ExecutionScope, on_result=None, requirements=None, requirement_title=None,
    ) -> DelegationResult:
        system_prompt = "You are a focused subagent. Complete the task and report."
        scope.budget.attach(parent)
        scope.budget.usage.attach(parent)
        registry: ToolRegistry | FilteredRegistry = scope.registry
        limit: int | None = None
        chosen = model or self.default_model
        # an explicit dispatch_agent model= or an AgentDef.model is a pin (routing-exempt);
        # an unpinned child inherits the routable default_model
        pinned = model is not None
        if agent is not None:
            definition = self.agents.get(agent)
            if definition is None:
                available = ", ".join(sorted(self.agents)) or "(none)"
                return DelegationResult(status="blocked", reason=f"unknown agent {agent!r}; available: {available}")
            if definition.strategy is not None:
                if requirements is not None:
                    return DelegationResult(status="blocked", reason="recorded checks require a direct child execution")
                # a coordination agent-def fans out to its experts instead of
                # running one child loop (experts become children of `parent`)
                from harness.mixture import Expert, run_strategy_result

                experts = [Expert(model=m) for m in (definition.experts or ())]
                narrowed = (FilteredRegistry(registry, allowed=definition.tools)
                            if definition.tools is not None else registry)
                # The strategy owns coordinator admission and increments depth
                # once, for both configured agents and direct native tools.
                token = current_scope.set(ExecutionScope(parent, narrowed, scope.budget, scope.depth,
                                                        scope.resources, scope.context_policy))
                try:
                    return await run_strategy_result(definition.strategy, self, parent, prompt, experts,
                                                     require_checks=definition.require_checks)
                finally:
                    current_scope.reset(token)
            system_prompt = definition.body or system_prompt
            limit = definition.max_output_chars
            if definition.model is not None:
                chosen = ModelId(definition.model)
                pinned = True
            if definition.tools is not None:
                registry = FilteredRegistry(registry, allowed=definition.tools)
        # explicit model arg beats agent default
        if model is not None:
            chosen = model
            pinned = True
        try:
            scope.budget.reserve_child(scope.depth + 1, session=parent, call_id=current_call_id())
        except BudgetExceeded as exc:
            return DelegationResult(status="blocked", reason=str(exc))
        try:
            return await self._run_child(prompt=prompt, parent=parent, agent=agent,
                                         chosen=chosen, pinned=pinned, registry=registry,
                                         system_prompt=system_prompt, limit=limit, scope=scope,
                                         on_result=on_result, requirements=requirements, requirement_title=requirement_title)
        finally:
            scope.budget.release_child()

    async def _run_child(self, *, prompt, parent, agent, chosen, pinned, registry,
                         system_prompt, limit, scope, on_result=None, requirements=None, requirement_title=None):
        child_id = new_session_id()
        active = current_agent_run.get()
        spawn_env = parent.append(
            SubagentSpawned(
                child_session_id=child_id,
                call_id=current_call_id(),
                agent_run_id=active.run_id if active else None,
                agent=AgentId(agent) if agent is not None else None,
                model=chosen,
            )
        )
        child = None

        def interrupted(status, reason):
            from harness.events import AgentRunFinished
            from harness.log import read_session, TornLogError
            terminal = None
            if child is not None:
                try:
                    terminal = next((e.event.result for e in reversed(read_session(self.base, child_id, repair=False))
                                     if isinstance(e.event, AgentRunFinished)), None)
                except (OSError, ValueError, TornLogError) as exc:
                    parent.append(ErrorRaised(where="subagent:outcome",
                        message=f"Child terminal could not be inspected ({type(exc).__name__})"))
            if terminal is not None and terminal.status == "incomplete" and status == "failed":
                status, reason = "incomplete", terminal.reason
            return DelegationResult(status=status, reason=reason, child_session_id=child_id,
                                    run_id=terminal.run_id if terminal else None)

        def finish(outcome):
            status = {"completed": "ok", "failed": "error", "blocked": "error",
                      "incomplete": "incomplete", "cancelled": "cancelled"}[outcome.status]
            parent.append(SubagentFinished(child_session_id=child_id, status=status,
                run_id=outcome.run_id, output=outcome.output, reason=outcome.reason,
                truncated=outcome.truncated))
            if on_result is not None:
                on_result(outcome)
            return outcome

        try:
            child = Session(
                self.base, child_id, parent=(parent.id, spawn_env.seq), default_model=chosen,
                redactors=parent._redactors,
            )
            loop = AgentLoop(
                session=child,
                provider=self.provider,
                registry=registry,
                hooks=self.hooks,
                resolver=self.resolver,
                model=chosen,
                system_prompt=system_prompt,
                pricing=self.pricing,
                pricing_for=self.pricing_for,
                pinned=pinned,
                scope=ExecutionScope(child, registry, scope.budget, scope.depth + 1,
                                     scope.resources, scope.context_policy),
            )
            await loop.start()
            task = AgentTask(prompt=prompt, agent=AgentId(agent) if agent else None)
            if requirements is not None:
                service = TaskService(child)
                service.create(requirement_title if requirement_title is not None else prompt[:4096])
                for requirement in requirements:
                    service.add_requirement(requirement)
                task = service.prepare(prompt).model_copy(update={"agent": task.agent})
            result = await loop.run_task(task)
            if requirements is not None:
                service.check()
            try:
                await loop.end()
            except Exception as exc:
                # Preserve the task outcome; log teardown failure separately.
                parent.append(
                    ErrorRaised(
                        where="subagent:teardown",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
            original = result.read_text(child.blobs)
            text = _bound(original, limit)
            outcome = DelegationResult(status=result.status if result.status in {"completed", "cancelled"} else "incomplete",
                reason=result.reason, text=text, child_session_id=child_id,
                run_id=result.run_id, output=result.output, truncated=text != original)
        except asyncio.CancelledError:
            finish(interrupted("cancelled", "cancelled"))
            raise
        except Exception as exc:
            return finish(interrupted("failed", type(exc).__name__))
        else:
            # Publish only after the result is readable. A failed terminal write
            # must not generate a contradictory second terminal event.
            return finish(outcome)
        finally:
            if child is not None:
                child.close()


@dataclass
class DispatchAgentTool:
    runner: SubagentRunner
    parent: Session
    spec: ToolSpec = field(init=False)

    def __post_init__(self) -> None:
        if self.runner.agents:
            names = ", ".join(sorted(self.runner.agents))
            description = (
                f"Launch a subagent with its own session to perform a task. "
                f"Args: prompt (required), model (optional model id), "
                f"agent (optional agent name; available: {names})."
            )
        else:
            description = (
                "Launch a subagent with its own session to perform a task. "
                "Args: prompt (required), model (optional model id)."
            )
        self.spec = ToolSpec(
            name=ToolName("dispatch_agent"),
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "agent": {"type": "string"},
                },
                "required": ["prompt"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        model = ModelId(args["model"]) if args.get("model") else None
        return await self.runner.run(
            prompt=args["prompt"], model=model, parent=self.parent, agent=args.get("agent")
        )
