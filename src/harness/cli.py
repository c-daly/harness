"""Headless entrypoint. Phase 1: FakeProvider demo only; real providers are Phase 2."""

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from harness.hooks import HookBus
from harness.interaction import HeadlessResolver, Resolver
from harness.loop import AgentLoop
from harness.provider import FakeProvider, ModelProvider, text_turn
from harness.session import Session
from harness.subagent import DispatchAgentTool, SubagentRunner
from harness.tools import ToolRegistry
from harness.types import ModelId, new_session_id


@dataclass
class Kernel:
    session: Session
    loop: AgentLoop
    registry: ToolRegistry
    hooks: HookBus
    provider: ModelProvider


def build_kernel(
    *,
    provider: ModelProvider,
    base_dir: Path,
    model: ModelId,
    system_prompt: str = "You are a helpful agent.",
    resolver: Resolver | None = None,
    hooks: HookBus | None = None,
) -> Kernel:
    resolver = resolver or HeadlessResolver()
    hooks = hooks or HookBus()
    registry = ToolRegistry()
    session = Session(base_dir, new_session_id(), default_model=model)
    runner = SubagentRunner(
        base=base_dir, provider=provider, registry=registry,
        hooks=hooks, resolver=resolver, default_model=model,
    )
    registry.register(DispatchAgentTool(runner=runner, parent=session))
    loop = AgentLoop(
        session=session, provider=provider, registry=registry, hooks=hooks,
        resolver=resolver, model=model, system_prompt=system_prompt,
    )
    return Kernel(session=session, loop=loop, registry=registry, hooks=hooks, provider=provider)


async def run_once(kernel: Kernel, prompt: str) -> str:
    try:
        await kernel.loop.start()
        result = await kernel.loop.run_turn(prompt)
        await kernel.loop.end()
        return result
    finally:
        kernel.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument("-p", "--prompt", required=True)
    parser.add_argument("--base-dir", type=Path,
                        default=Path.home() / ".local" / "share" / "harness")
    args = parser.parse_args()
    provider = FakeProvider([text_turn(f"echo: {args.prompt}")])
    kernel = build_kernel(provider=provider, base_dir=args.base_dir, model=ModelId("fake:echo"))
    print(asyncio.run(run_once(kernel, args.prompt)))
