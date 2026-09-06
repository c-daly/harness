"""Subagents: child sessions, concurrent in-process, parent's enforcement applies."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.callctx import current_call_id
from harness.agent import AgentTask
from harness.events import ErrorRaised, SubagentFinished, SubagentSpawned
from harness.execution import BudgetExceeded, ExecutionScope, current_scope
from harness.frontmatter import AgentDef
from harness.hooks import HookBus
from harness.interaction import Resolver
from harness.loop import AgentLoop
from harness.provider import ModelProvider
from harness.session import Session
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
    registry: ToolRegistry
    hooks: HookBus
    resolver: Resolver
    default_model: ModelId
    pricing: dict[str, float] | None = None
    pricing_for: "Callable[[ModelId], dict[str, float]] | None" = None
    agents: dict[str, AgentDef] = field(default_factory=dict)
    _root_scopes: dict[str, ExecutionScope] = field(default_factory=dict, init=False, repr=False)

    async def run(
        self, *, prompt: str, model: ModelId | None, parent: Session, agent: str | None = None
    ) -> str:
        scope = current_scope.get()
        if scope is None:
            scope = self._root_scopes.setdefault(str(parent.id), ExecutionScope(parent, self.registry))
        parent = scope.session  # Includes calls through root-bound coordination tools.
        return await self._run_in_scope(prompt=prompt, model=model, parent=parent,
                                        agent=agent, scope=scope)

    async def _run_in_scope(
        self, *, prompt: str, model: ModelId | None, parent: Session,
        agent: str | None, scope: ExecutionScope,
    ) -> str:
        system_prompt = "You are a focused subagent. Complete the task and report."
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
                return f"[subagent error] unknown agent {agent!r}; available: {available}"
            if definition.strategy is not None:
                # a coordination agent-def fans out to its experts instead of
                # running one child loop (experts become children of `parent`)
                from harness.mixture import Expert, run_strategy

                experts = [Expert(model=m) for m in (definition.experts or ())]
                narrowed = (FilteredRegistry(registry, allowed=definition.tools)
                            if definition.tools is not None else registry)
                try:
                    scope.budget.reserve_child(scope.depth + 1)
                except BudgetExceeded as exc:
                    return f"[subagent error] {exc}"
                token = current_scope.set(ExecutionScope(parent, narrowed, scope.budget, scope.depth + 1))
                try:
                    return await run_strategy(definition.strategy, self, parent, prompt, experts)
                finally:
                    current_scope.reset(token)
                    scope.budget.release_child()
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
            scope.budget.reserve_child(scope.depth + 1)
        except BudgetExceeded as exc:
            return f"[subagent error] {exc}"
        try:
            return await self._run_child(prompt=prompt, parent=parent, agent=agent,
                                         chosen=chosen, pinned=pinned, registry=registry,
                                         system_prompt=system_prompt, limit=limit, scope=scope)
        finally:
            scope.budget.release_child()

    async def _run_child(self, *, prompt, parent, agent, chosen, pinned, registry,
                         system_prompt, limit, scope):
        child_id = new_session_id()
        spawn_env = parent.append(
            SubagentSpawned(
                child_session_id=child_id,
                call_id=current_call_id(),
                agent=AgentId(agent) if agent is not None else None,
                model=chosen,
            )
        )
        child = None
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
                scope=ExecutionScope(child, registry, scope.budget, scope.depth + 1),
            )
            await loop.start()
            result = await loop.run_task(AgentTask(prompt=prompt, agent=AgentId(agent) if agent else None))
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
            status = "ok" if result.status == "completed" else "incomplete"
            text = _bound(result.read_text(child.blobs), limit)
            if status == "incomplete":
                text = f"[subagent error] incomplete ({result.reason}): {text}"
        except asyncio.CancelledError:
            parent.append(SubagentFinished(child_session_id=child_id, status="cancelled"))
            raise
        except Exception as exc:
            parent.append(SubagentFinished(child_session_id=child_id, status="error"))
            return f"[subagent error] {exc}"
        else:
            # Publish only after the result is readable. A failed terminal write
            # must not generate a contradictory second terminal event.
            parent.append(SubagentFinished(child_session_id=child_id, status=status))
            return text
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
