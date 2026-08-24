"""Subagents: child sessions, concurrent in-process, parent's enforcement applies."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.events import ErrorRaised, SubagentFinished, SubagentSpawned
from harness.frontmatter import AgentDef
from harness.hooks import HookBus
from harness.interaction import Resolver
from harness.loop import AgentLoop
from harness.provider import ModelProvider
from harness.session import Session
from harness.tools import FilteredRegistry, ToolRegistry, ToolSpec
from harness.types import ModelId, ToolName, new_session_id


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

    async def run(
        self, *, prompt: str, model: ModelId | None, parent: Session, agent: str | None = None
    ) -> str:
        system_prompt = "You are a focused subagent. Complete the task and report."
        registry: ToolRegistry | FilteredRegistry = self.registry
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
                return await run_strategy(definition.strategy, self, parent, prompt, experts)
            system_prompt = definition.body or system_prompt
            if definition.model is not None:
                chosen = ModelId(definition.model)
                pinned = True
            if definition.tools is not None:
                registry = FilteredRegistry(self.registry, allowed=definition.tools)
        # explicit model arg beats agent default
        if model is not None:
            chosen = model
            pinned = True
        child_id = new_session_id()
        spawn_env = parent.append(SubagentSpawned(child_session_id=child_id, model=chosen))
        child = Session(
            self.base, child_id, parent=(parent.id, spawn_env.seq), default_model=chosen
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
        )
        try:
            await loop.start()
            result = await loop.run_turn(prompt)
            try:
                await loop.end()
            except Exception as exc:
                # the WORK succeeded; teardown failure is logged, never converted
                # into a false child error
                parent.append(
                    ErrorRaised(
                        where="subagent:teardown",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
            parent.append(SubagentFinished(child_session_id=child_id, status="ok"))
            return result
        except asyncio.CancelledError:
            parent.append(SubagentFinished(child_session_id=child_id, status="cancelled"))
            raise
        except Exception as exc:
            parent.append(SubagentFinished(child_session_id=child_id, status="error"))
            return f"[subagent error] {exc}"
        finally:
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
