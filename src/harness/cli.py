"""Headless entrypoint. Phase 1: FakeProvider demo; Phase 2: catalog/--model/--resume/SIGINT."""

import argparse
import asyncio
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from harness.hooks import HookBus
from harness.interaction import HeadlessResolver, Resolver
from harness.loop import AgentLoop
from harness.permissions import PermissionEngine, default_engine
from harness.provider import FakeProvider, ModelProvider, text_turn
from harness.session import Session
from harness.subagent import DispatchAgentTool, SubagentRunner
from harness.tools import ToolRegistry
from harness.types import ModelId, SessionId, new_session_id


@dataclass
class Kernel:
    session: Session
    loop: AgentLoop
    registry: ToolRegistry
    hooks: HookBus
    provider: ModelProvider
    resumed: bool = field(default=False)


def build_kernel(
    *,
    provider: ModelProvider,
    base_dir: Path,
    model: ModelId,
    system_prompt: str = "You are a helpful agent.",
    resolver: Resolver | None = None,
    hooks: HookBus | None = None,
    pricing: dict[str, float] | None = None,
    resume_session_id: SessionId | None = None,
    permissions: PermissionEngine | None = None,
) -> Kernel:
    from harness.resume import resume_session

    resolver = resolver or HeadlessResolver()
    hooks = hooks or HookBus()
    if permissions is not None:
        hooks.register_dispatch(permissions.name, permissions, priority=permissions.priority)
    registry = ToolRegistry()
    resumed = False
    if resume_session_id is not None:
        session, transcript = resume_session(base_dir, resume_session_id, default_model=model)
        resumed = True
    else:
        session = Session(base_dir, new_session_id(), default_model=model)
        transcript = None
    runner = SubagentRunner(
        base=base_dir, provider=provider, registry=registry,
        hooks=hooks, resolver=resolver, default_model=model,
        pricing=pricing,
    )
    registry.register(DispatchAgentTool(runner=runner, parent=session))
    loop_kwargs: dict = dict(
        session=session, provider=provider, registry=registry, hooks=hooks,
        resolver=resolver, model=model, system_prompt=system_prompt,
        pricing=pricing,
    )
    if transcript is not None:
        loop_kwargs["history"] = transcript
    loop = AgentLoop(**loop_kwargs)
    return Kernel(session=session, loop=loop, registry=registry, hooks=hooks, provider=provider, resumed=resumed)


async def run_once(kernel: Kernel, prompt: str) -> str:
    from harness.events import UserInterrupt

    try:
        if not kernel.resumed:
            await kernel.loop.start()
        result = await kernel.loop.run_turn(prompt)
        await kernel.loop.end()
        return result
    except asyncio.CancelledError:
        try:
            kernel.session.append(UserInterrupt())
        except Exception:
            pass
        raise
    finally:
        kernel.session.close()


async def _amain(kernel: Kernel, prompt: str) -> str:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, task.cancel)
    # run_once owns the UserInterrupt record; this wrapper only owns the signal handler lifecycle
    try:
        return await run_once(kernel, prompt)
    finally:
        loop.remove_signal_handler(signal.SIGINT)


def _apply_allow_flags(engine: PermissionEngine, allows: list[str]) -> None:
    """Grant each tool glob in allows at session scope."""
    for pattern in allows:
        engine.grant(pattern)


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument("-p", "--prompt", required=True)
    parser.add_argument("--base-dir", type=Path,
                        default=Path.home() / ".local" / "share" / "harness")
    parser.add_argument("--model", default=None,
                        help="Catalog alias to use for the model (requires a catalog file).")
    parser.add_argument("--catalog", type=Path,
                        default=Path.home() / ".config" / "harness" / "models.toml",
                        help="Path to the model catalog TOML (default: ~/.config/harness/models.toml).")
    parser.add_argument("--resume", dest="resume_session_id", default=None,
                        help="Session ID to resume.")
    parser.add_argument("--allow", action="append", default=[],
                        metavar="TOOL_GLOB",
                        help="Grant a tool glob at session scope (can be repeated).")
    args = parser.parse_args()

    resume_session_id = SessionId(args.resume_session_id) if args.resume_session_id else None

    if args.model is not None:
        from harness.catalog import Catalog
        from harness.provider_litellm import LiteLLMProvider

        try:
            resolved = Catalog.load(args.catalog).resolve(args.model)
        except FileNotFoundError:
            raise SystemExit(
                f"catalog not found at {args.catalog}; create it or pass --catalog <path>"
            )
        provider: ModelProvider = LiteLLMProvider(api_base=resolved.api_base)
        model = resolved.route
        pricing = resolved.pricing_dict() or None
    else:
        provider = FakeProvider([text_turn(f"echo: {args.prompt}")])
        model = ModelId("fake:echo")
        pricing = None

    engine = default_engine(project_dir=Path.cwd())
    if args.allow and engine is None:
        print(
            "warning: --allow given but no permission config found; "
            "flags have no effect (tool calls are not gated)",
            file=sys.stderr,
        )
    if engine and args.allow:
        _apply_allow_flags(engine, args.allow)

    kernel = build_kernel(
        provider=provider,
        base_dir=args.base_dir,
        model=model,
        pricing=pricing,
        resume_session_id=resume_session_id,
        permissions=engine,
    )
    from harness.errors import ProviderError
    try:
        print(asyncio.run(_amain(kernel, args.prompt)))
    except ProviderError as exc:
        raise SystemExit(f"provider error: {exc}") from exc
