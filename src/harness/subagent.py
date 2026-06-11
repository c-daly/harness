"""Subagents: child sessions, concurrent in-process, parent's enforcement applies."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from harness.events import SubagentFinished, SubagentSpawned
from harness.hooks import HookBus
from harness.interaction import Resolver
from harness.loop import AgentLoop
from harness.provider import ModelProvider
from harness.session import Session
from harness.tools import ToolRegistry, ToolSpec
from harness.types import ModelId, ToolName, new_session_id


@dataclass
class SubagentRunner:
    base: Path
    provider: ModelProvider
    registry: ToolRegistry
    hooks: HookBus
    resolver: Resolver
    default_model: ModelId

    async def run(self, *, prompt: str, model: ModelId | None, parent: Session) -> str:
        child_id = new_session_id()
        chosen = model or self.default_model
        spawn_env = parent.append(
            SubagentSpawned(child_session_id=child_id, model=chosen)
        )
        child = Session(
            self.base, child_id, parent=(parent.id, spawn_env.seq), default_model=chosen
        )
        loop = AgentLoop(
            session=child, provider=self.provider, registry=self.registry,
            hooks=self.hooks, resolver=self.resolver, model=chosen,
            system_prompt="You are a focused subagent. Complete the task and report.",
        )
        try:
            await loop.start()
            result = await loop.run_turn(prompt)
            await loop.end()
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
    spec: ToolSpec = ToolSpec(
        name=ToolName("dispatch_agent"),
        description="Launch a subagent with its own session to perform a task. "
                    "Args: prompt (required), model (optional model id).",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "model": {"type": "string"},
            },
            "required": ["prompt"],
        },
    )

    async def __call__(self, args: dict) -> str:
        model = ModelId(args["model"]) if args.get("model") else None
        return await self.runner.run(prompt=args["prompt"], model=model, parent=self.parent)
