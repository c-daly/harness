"""Headless entrypoint. Phase 1: FakeProvider demo; Phase 2: catalog/--model/--resume/SIGINT."""

import argparse
import asyncio
import signal
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from harness.hooks import HookBus
from harness.controller import InteractionController
from harness.context import ContextPolicy
from harness.execution import ExecutionBudget, ExecutionLimits, ExecutionScope
from harness.usage_budget import UsageBudget, UsageBudgetConfigError, UsageLimits
from harness.fallback import FallbackPolicy
from harness.interaction import HeadlessResolver, Resolver
from harness.loop import AgentLoop
from harness.mcp_config import McpConfigError, McpServerSpec, load_mcp_config, load_mcp_file
from harness.mcp_host import McpHost
from harness.permissions import PermissionEngine, default_engine
from harness.provider import FakeProvider, ModelProvider, text_turn
from harness.session import Session
from harness.subagent import DispatchAgentTool, SubagentRunner
from harness.tools import FilteredRegistry, ToolRegistry
from harness.types import ModelId, SessionId, new_session_id

if TYPE_CHECKING:
    from harness.plugins import LoadedPlugins
    from harness.routing import RoutingRuleSet


@dataclass
class Kernel:
    session: Session
    loop: AgentLoop
    registry: ToolRegistry
    hooks: HookBus
    provider: ModelProvider
    runner: "SubagentRunner"
    resumed: bool = field(default=False)
    tags: list[str] = field(default_factory=list)
    mcp: McpHost | None = None
    plugins: "LoadedPlugins | None" = None
    plugin_warnings: list[str] = field(default_factory=list)
    _plugin_pumps: list = field(default_factory=list)
    controller: InteractionController = field(default_factory=InteractionController)

    @property
    def resources(self):
        return self.loop.dispatcher.scope.resources

    @property
    def context_policy(self):
        return self.loop.dispatcher.scope.context_policy

    @cached_property
    def tasks(self):
        from harness.tasks import TaskService
        return TaskService(self.session)

    @cached_property
    def improvements(self):
        """Core lifecycle records are available even when all plugins are disabled."""
        from harness.improvement_journal import ImprovementJournal
        return ImprovementJournal(self.session)

    @cached_property
    def semantics(self):
        from harness.semantics import SemanticService
        return SemanticService(self.loop.dispatcher, lambda: self.provider)

    @cached_property
    def compaction(self):
        from harness.compaction import CompactionService
        return CompactionService(self)

    @cached_property
    def improvement_service(self):
        from harness.prompt_improvement import PromptImprovementService
        return PromptImprovementService(self)

    @cached_property
    def handoffs(self):
        from harness.handoff import HandoffService
        return HandoffService(self)

    def set_provider(self, provider: ModelProvider) -> None:
        """Single point for retargeting the model provider mid-session. The
        loop, the subagent runner, and this kernel share one provider instance
        by construction; a swap that touches only loop.provider leaves
        dispatch_agent and mixture experts on the old one."""
        self.provider = provider
        self.loop.provider = provider
        self.runner.provider = provider
        if hasattr(provider, "bind_dispatcher"):
            provider.bind_dispatcher(self.loop.dispatcher)


def _make_pricing_for(catalog) -> "Callable[[ModelId], dict[str, float]]":
    """Pricing keyed on the EFFECTIVE (post-routing) model alias, so telemetry's
    per-model cost reflects the model actually used. Unknown alias -> {} (no cost)."""
    from harness.catalog import UnknownAliasError

    def _pricing(model: ModelId) -> dict[str, float]:
        try:
            return catalog.resolve(str(model)).pricing_dict()
        except UnknownAliasError:
            return {}

    return _pricing


def _catalog_provider(catalog, previous=None):
    from harness.provider_antigravity import AntigravityProvider
    from harness.provider_claude_code import ClaudeCodeProvider
    from harness.provider_codex import CodexProvider
    from harness.provider_litellm import CatalogProvider

    # Keep adapter configuration on an in-app resume, without mutating the
    # old session's catalog while the new selection is being validated.
    if isinstance(previous, CatalogProvider):
        from dataclasses import replace
        return replace(previous, catalog=catalog)
    return CatalogProvider(catalog, claude_code=ClaudeCodeProvider(),
                           codex=CodexProvider(), antigravity=AntigravityProvider())


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
    tags: list[str] | None = None,
    mcp: Sequence[McpServerSpec] | None = None,
    plugins: "LoadedPlugins | None" = None,
    workspace_root: Path | None = None,
    native_tools: bool = False,
    pricing_for: "Callable[[ModelId], dict[str, float]] | None" = None,
    routing_rules: "RoutingRuleSet | None" = None,
    model_pinned: bool = False,
    inherit_model_selection: bool = False,
    explicit_model_selection: bool = False,
    catalog_path: Path | None = None,
    execution_limits: ExecutionLimits | None = None,
    usage_limits: UsageLimits | None = None,
    resources=None,
    context_policy: ContextPolicy | None = None,
    inherit_context_policy: bool = True,
    fallback_policy: FallbackPolicy | None = None,
) -> Kernel:
    from harness.resume import resume_session

    resolver = resolver or HeadlessResolver()
    if fallback_policy is None and routing_rules is not None:
        fallback_policy = routing_rules.fallback
    hooks = hooks or HookBus()
    if permissions is not None:
        hooks.register_dispatch(permissions.name, permissions, priority=permissions.priority)
    registry = ToolRegistry()
    read_state = None  # set below when native tools are on; used by routing signals
    resumed = False
    if resume_session_id is not None:
        def configure(state):
            nonlocal provider, model, model_pinned, pricing, pricing_for, context_policy
            if state.usage_budget.root_session_id is not None:
                raise UsageBudgetConfigError(f"shared usage accounting belongs to session {state.usage_budget.root_session_id}; "
                                 "resume that root session")
            state.usage_budget.limits.narrow(usage_limits or UsageLimits())
            if inherit_model_selection and not explicit_model_selection and state.model_selection is not None:
                from harness.model_selection import load_selected_catalog
                catalog, resolved = load_selected_catalog(state.model_selection, catalog_path)
                provider = _catalog_provider(catalog, provider)
                model = state.model_selection.model
                model_pinned = state.model_selection.pinned
                pricing = resolved.pricing_dict() or None
                pricing_for = _make_pricing_for(catalog)
            if context_policy is None and inherit_context_policy:
                context_policy = state.context_policy

        session, transcript = resume_session(base_dir, resume_session_id, default_model=model,
                                             configure=configure)
        resumed = True
    else:
        session = Session(base_dir, new_session_id(), default_model=model)
        transcript = None
    try:
        usage_budget = UsageBudget.restore(session, usage_limits)
    except BaseException:
        session.close()
        raise
    if native_tools:
        from harness.fold import fold
        from harness.log import TornLogError, read_session
        from harness.native_tools import (
            CompoundCommandGuard,
            ReadState,
            baseline_ruleset,
            register_native_tools,
        )
        from harness.permissions import PermissionEngine as _PermissionEngine
        from harness.workspace import WorkspaceGuard, resolve_in_workspace

        ws_root = (workspace_root or Path.cwd()).resolve()
        seed: set[str] = set()
        if resume_session_id is not None:
            try:
                raw_paths = fold(list(read_session(base_dir, resume_session_id))).read_paths
            except (TornLogError, OSError):
                # seeding is best-effort; a torn log seeds empty rather than blocking the
                # kernel (resume repair is the loop’s job)
                raw_paths = set()
            for p in raw_paths:
                try:
                    resolved = resolve_in_workspace(ws_root, p)
                    seed.add(str(resolved))
                except Exception:
                    pass
        # Retain path hints for routing; only live observations establish file
        # versions. A resumed agent must reread before overwriting existing files.
        read_state = ReadState(seed)  # noqa: F841 (captured by routing signals below)
        register_native_tools(
            registry,
            workspace_root=ws_root,
            read_state=read_state,
            emit=session.append,
        )
        if permissions is None:
            permissions = _PermissionEngine([baseline_ruleset()])
            permissions._baseline_installed = True
            # hooks is a fresh HookBus() per build_kernel call (callers do not pass one),
            # so cross-call double registration on the bus is impossible; the shared
            # engine object is what accumulates, hence the sentinel flag below.
            hooks.register_dispatch(permissions.name, permissions, priority=permissions.priority)
        elif not getattr(permissions, "_baseline_installed", False):
            # a caller-supplied engine may be reused across rebuilds (TUI); install the
            # baseline layer exactly once so it does not accumulate per call
            permissions.layers.append(baseline_ruleset())
            permissions._baseline_installed = True
        hooks.register_dispatch("workspace-guard", WorkspaceGuard(ws_root), priority=900)
        hooks.register_dispatch("bash-compound-guard", CompoundCommandGuard(), priority=950)
    agents_sink: dict = {}
    plugin_warnings: list[str] = []
    if plugins is not None:
        from harness.plugins import apply_plugins

        plugin_warnings = apply_plugins(
            plugins, registry=registry, hooks=hooks, agents_sink=agents_sink
        )
    runner = SubagentRunner(
        base=base_dir,
        provider=provider,
        registry=registry,
        hooks=hooks,
        resolver=resolver,
        default_model=model,
        pricing=pricing,
        pricing_for=pricing_for,
        agents=agents_sink,
    )
    registry.register(DispatchAgentTool(runner=runner, parent=session))
    from harness.mixture import register_mixture_tools

    register_mixture_tools(registry, runner=runner, parent=session)
    effective_registry = (FilteredRegistry(registry, allowed=context_policy.tools)
                          if context_policy is not None and context_policy.tools is not None
                          else registry)
    runner.registry = effective_registry
    loop_kwargs: dict = dict(
        session=session,
        provider=provider,
        registry=effective_registry,
        hooks=hooks,
        resolver=resolver,
        model=model,
        system_prompt=system_prompt,
        pricing=pricing,
        pricing_for=pricing_for,
        pinned=model_pinned,
        fallback_policy=fallback_policy,
    )
    if transcript is not None:
        loop_kwargs["history"] = transcript
    loop = AgentLoop(**loop_kwargs)
    # A resume baseline (including a departing TUI pin) is not new selection
    # intent. Restored preferences are already durable. Only a conversational
    # override may establish/replace one; administrative model choices may not.
    if resumed and explicit_model_selection:
        loop.record_model_selection()
    from harness.resources import LocalResources
    scope = ExecutionScope(session, effective_registry,
                           ExecutionBudget(execution_limits or ExecutionLimits(), usage=usage_budget),
                           resources=resources if resources is not None else LocalResources(),
                           context_policy=context_policy)
    loop.dispatcher.scope = scope
    runner._root_scopes[str(session.id)] = scope
    if resumed and (context_policy is not None or not inherit_context_policy):
        from harness.events import ContextPolicyConfigured
        session.append(ContextPolicyConfigured(policy=context_policy))
    if resumed:
        from harness.events import FallbackConfigured
        from harness.log import read_session
        if fallback_policy is not None or any(isinstance(e.event, FallbackConfigured)
                for e in read_session(base_dir, session.id, repair=False)):
            session.append(FallbackConfigured(policy=fallback_policy))
    if hasattr(provider, "bind_dispatcher"):
        provider.bind_dispatcher(loop.dispatcher)
    if routing_rules is not None:
        from harness.messages import Role
        from harness.routing import RoutingContext, RoutingEngine

        def _routing_signals() -> "RoutingContext":
            prompt = ""
            for _m in reversed(loop.history):
                if _m.role == Role.USER and _m.text():
                    prompt = _m.text()
                    break
            _paths = tuple(read_state.paths()) if read_state is not None else ()
            return RoutingContext(tags=tuple(tags or []), prompt=prompt, paths=_paths)

        routing_engine = RoutingEngine(routing_rules, _routing_signals)
        hooks.register_dispatch(
            routing_engine.name, routing_engine, priority=routing_engine.priority
        )
    mcp_host = None
    if mcp:
        mcp_host = McpHost(mcp, registry=registry, hooks=hooks, session=session)
    return Kernel(
        session=session,
        loop=loop,
        registry=registry,
        hooks=hooks,
        provider=provider,
        runner=runner,
        resumed=resumed,
        tags=tags or [],
        mcp=mcp_host,
        plugins=plugins,
        plugin_warnings=plugin_warnings,
    )


async def run_once(kernel: Kernel, prompt: str) -> str:
    from harness.events import CustomEvent
    from harness.plugins import start_subscriber_pumps

    pump_tasks: list = []
    try:
        if kernel.mcp is not None:
            # Headless never shows the TUI checklist -- there is no one to ask,
            # so only the servers pre-checked by default_enabled ever start.
            only = {s.name for s in kernel.mcp.specs if s.default_enabled}
            for warning in await kernel.mcp.start(only=only):
                print(f"warning: {warning}", file=sys.stderr)
        if not kernel.resumed:
            await kernel.loop.start()
        for t in kernel.tags:
            kernel.session.append(CustomEvent(namespace="harness", name="tag", data={"tag": t}))
        if kernel.mcp is not None:
            kernel.mcp.flush_events()
        if kernel.plugins is not None:
            for warning in kernel.plugin_warnings:
                print(f"warning: {warning}", file=sys.stderr)
            for _plugin in kernel.plugins.plugins:
                kernel.session.append(
                    CustomEvent(
                        namespace="plugin",
                        name="plugin_loaded",
                        data={"plugin": _plugin.name, "version": _plugin.version},
                    )
                )
            kernel._plugin_pumps = start_subscriber_pumps(kernel)
            pump_tasks = kernel._plugin_pumps
        result = ""

        async def execute(pending):
            nonlocal result
            kernel.controller.phase = "working"
            outcome = await kernel.loop.run_task(kernel.tasks.prepare(pending.text))
            result = outcome.read_text(kernel.session.blobs)
            return outcome

        kernel.controller.submit(prompt, expand_mentions=False)
        await kernel.controller.run_next(execute)
        await kernel.loop.end()
        return result
    except asyncio.CancelledError:
        try:
            kernel.loop.interrupt_turn()
        except Exception:
            pass
        raise
    finally:
        try:
            if pump_tasks:
                for _task in pump_tasks:
                    _task.cancel()
                await asyncio.gather(*pump_tasks, return_exceptions=True)
            if kernel.mcp is not None:
                await kernel.mcp.stop()
                kernel.mcp.flush_events()
        finally:
            try:
                await kernel.resources.close(emit=kernel.session.append)
            finally:
                kernel.session.close()


async def _amain(kernel: Kernel, prompt: str) -> str:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, task.cancel)
    try:
        return await run_once(kernel, prompt)
    finally:
        loop.remove_signal_handler(signal.SIGINT)


def _apply_allow_flags(engine: PermissionEngine, allows: list[str]) -> None:
    """Grant each --allow pattern at session scope, expanding compact tool(arg) form."""
    from harness.native_tools import desugar_pattern

    for pattern in allows:
        rule = desugar_pattern(pattern)
        engine.grant(rule.tool, dict(rule.match) or None)


def _subcommand(argv: list[str]) -> None:
    import argparse

    from harness.resume import append_events
    from harness.telemetry import (
        rebuild_index,
        render_compare,
        render_stats,
        run_rollup,
        stats_summary,
    )

    command, rest = argv[0], argv[1:]
    parser = argparse.ArgumentParser(prog=f"harness {command}")
    parser.add_argument(
        "--base-dir", type=Path, default=Path.home() / ".local" / "share" / "harness"
    )
    if command == "improvements":
        from harness.blobs import BlobStore
        from harness.improvement_journal import inspect_improvement, read_improvements, render_improvements
        parser.add_argument("session_id")
        parser.add_argument("--show", metavar="RECORD_ID")
        args = parser.parse_args(rest)
        state = read_improvements(args.base_dir, SessionId(args.session_id))
        if args.show:
            root = args.base_dir / "sessions" / args.session_id / "blobs"
            if not root.is_dir():
                parser.error("session blob directory is missing")
            try:
                print(inspect_improvement(state, BlobStore(root), args.show))
            except ValueError as exc:
                parser.error(str(exc))
        else:
            print(render_improvements(state))
    elif command == "stats":
        parser.add_argument("--tag", default=None)
        args = parser.parse_args(rest)
        conn, warnings = rebuild_index(args.base_dir)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print(render_stats(stats_summary(conn, tag=args.tag)))
    elif command == "compare":
        parser.add_argument("run_a")
        parser.add_argument("run_b")
        args = parser.parse_args(rest)
        conn, warnings = rebuild_index(args.base_dir)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        try:
            print(render_compare(run_rollup(conn, args.run_a), run_rollup(conn, args.run_b)))
        except KeyError as exc:
            raise SystemExit(str(exc).strip("\u0027\u0022")) from exc
    elif command == "outcome":
        parser.add_argument("session_id")
        parser.add_argument("status", choices=("ok", "fail", "abandoned"))
        parser.add_argument("--score", type=float, default=None)
        parser.add_argument("--note", default="")
        args = parser.parse_args(rest)
        from harness.events import SessionOutcome
        from harness.log import SessionLockedError
        try:
            append_events(
                args.base_dir,
                SessionId(args.session_id),
                [SessionOutcome(status=args.status, score=args.score, note=args.note)],
            )
        except SessionLockedError as exc:
            raise SystemExit(f"session is still running: {exc}") from exc
        print(f"recorded {args.status} for {args.session_id}")


def _run_main() -> None:
    parser = argparse.ArgumentParser(prog="harness", epilog=(
        "Portable continuation: harness export SESSION_ID NEW_FILE.zip [--task ID]. "
        "Use 'harness export --help' for details."))
    parser.add_argument("-p", "--prompt", default=None)
    parser.add_argument(
        "--base-dir", type=Path, default=Path.home() / ".local" / "share" / "harness"
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Catalog alias to use for the model (requires a catalog file).",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path.home() / ".config" / "harness" / "models.toml",
        help="Path to the model catalog TOML (default: ~/.config/harness/models.toml).",
    )
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume", dest="resume_session_id", default=None, help="Session ID to resume."
    )
    resume_group.add_argument(
        "--continue",
        dest="continue_last",
        action="store_true",
        help="Resume the most recently active session under --base-dir.",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="TOOL_GLOB",
        help="Grant a tool glob at session scope (can be repeated).",
    )
    parser.add_argument(
        "--tag", action="append", default=[], metavar="TAG", help="Tag this run (can be repeated)."
    )
    parser.add_argument(
        "--mcp-config",
        type=Path,
        default=None,
        help="Explicit mcp.toml; overrides the standard locations.",
    )
    parser.add_argument("--no-mcp", action="store_true", help="Skip MCP server startup entirely.")
    parser.add_argument(
        "--plugin-dir",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help="Extra plugin directory (can be repeated).",
    )
    parser.add_argument(
        "--no-plugins", action="store_true", help="Disable plugin discovery entirely."
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--budget-input-tokens", type=int, default=None,
                        help="Shared input-token stop limit; resumed sessions retain stricter stored limits.")
    parser.add_argument("--budget-output-tokens", type=int, default=None,
                        help="Shared output-token stop limit; already running calls may cross it.")
    parser.add_argument("--budget-cost-usd", type=float, default=None,
                        help="Shared estimated token-cost stop limit in USD; not a provider billing cap.")
    context_flags = parser.add_mutually_exclusive_group()
    context_flags.add_argument("--context-profile", type=Path,
                               help="TOML profile limiting history, input bytes, and exact tool names.")
    context_flags.add_argument("--no-context-profile", action="store_true",
                               help="Explicitly clear an inherited context profile when resuming.")
    args = parser.parse_args()
    try:
        usage_limits = UsageLimits(max_input_tokens=args.budget_input_tokens,
                                  max_output_tokens=args.budget_output_tokens,
                                  max_cost_usd=args.budget_cost_usd)
    except ValueError as exc:
        parser.error(str(exc))
    context_policy = None
    if args.context_profile is not None:
        try:
            context_policy = ContextPolicy.load(args.context_profile)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"context profile unavailable or invalid ({type(exc).__name__})") from None

    resume_session_id = SessionId(args.resume_session_id) if args.resume_session_id else None
    if args.continue_last:
        from harness.sessions import list_sessions

        sessions = list_sessions(args.base_dir)
        if not sessions:
            raise SystemExit(f"--continue: no sessions found under {args.base_dir}")
        resume_session_id = sessions[0].session_id

    from harness.routing import RoutingConfigError, load_routing

    try:
        routing_rules = load_routing(project_dir=Path.cwd())
    except RoutingConfigError as exc:
        raise SystemExit(f"routing config error: {exc}")
    pricing_for: Callable[[ModelId], dict[str, float]] | None = None
    model_pinned = args.model is not None
    selected_alias = args.model
    if resume_session_id is not None and args.model is None:
        from harness.model_selection import read_model_selection, load_selected_catalog
        from harness.log import SessionLockedError, TornLogError
        try:
            selection = read_model_selection(args.base_dir, resume_session_id)
            if selection is not None:
                load_selected_catalog(selection, args.catalog)
                selected_alias = str(selection.model)
                model_pinned = selection.pinned
        except (OSError, ValueError, SessionLockedError, TornLogError) as exc:
            raise SystemExit(f"cannot resume {resume_session_id}: {exc}") from None

    if selected_alias is not None:
        from harness.catalog import Catalog, UnknownAliasError

        try:
            catalog = Catalog.load(args.catalog)
        except FileNotFoundError:
            raise SystemExit(
                f"catalog not found at {args.catalog}; create it or pass --catalog <path>"
            )
        try:
            resolved = catalog.resolve(selected_alias)
        except UnknownAliasError:
            raise SystemExit(
                f"unknown model alias {selected_alias!r}; known aliases: "
                f"{', '.join(catalog.aliases()) or '(none)'}"
            )
        # the catalog-aware provider resolves endpoint+key per call from the alias,
        # so the model string carried through dispatch is the ALIAS, not the route
        provider: ModelProvider = _catalog_provider(catalog)
        model = ModelId(selected_alias)
        pricing = resolved.pricing_dict() or None
        pricing_for = _make_pricing_for(catalog)
    elif routing_rules is not None and routing_rules.default is not None:
        # no explicit --model, but a routing config declares a routable baseline:
        # run the default alias UNpinned so per-turn rules can rewrite it
        from harness.catalog import Catalog, UnknownAliasError

        try:
            catalog = Catalog.load(args.catalog)
        except FileNotFoundError:
            raise SystemExit(
                f"routing default {routing_rules.default!r} needs a catalog; "
                f"none at {args.catalog} (pass --catalog <path>)"
            )
        try:
            resolved = catalog.resolve(routing_rules.default)
        except UnknownAliasError:
            raise SystemExit(
                f"routing default {routing_rules.default!r} is not a known alias; "
                f"known aliases: {', '.join(catalog.aliases()) or '(none)'}"
            )
        provider = _catalog_provider(catalog)
        model = ModelId(routing_rules.default)
        pricing = resolved.pricing_dict() or None
        pricing_for = _make_pricing_for(catalog)
    elif args.prompt is not None:
        provider = FakeProvider([text_turn(f"echo: {args.prompt}")])
        model = ModelId("fake:echo")
        pricing = None
    else:
        from harness.provider import EchoProvider

        provider = EchoProvider()
        model = ModelId("echo")
        pricing = None

    # The CLI always installs native tools. Keep one engine for its baseline,
    # explicit session grants, and the interactive permission resolver even on
    # a fresh installation without a permissions.toml.
    engine = default_engine(project_dir=Path.cwd()) or PermissionEngine()
    if args.allow:
        _apply_allow_flags(engine, args.allow)

    mcp_specs: tuple[McpServerSpec, ...] = ()
    if not args.no_mcp:
        try:
            if args.mcp_config is not None:
                mcp_specs = load_mcp_file(args.mcp_config, source="adhoc")
            else:
                mcp_specs = load_mcp_config(project_dir=Path.cwd())
        except (McpConfigError, OSError) as exc:
            raise SystemExit(str(exc)) from exc

    loaded_plugins = None
    if not args.no_plugins:
        from harness.plugins import PluginError, load_plugins

        config_home = Path.home() / ".config" / "harness"
        plugin_dirs = []
        _default_dirs = [config_home / "plugins", Path.cwd() / ".harness" / "plugins"]
        for _d in _default_dirs:
            if _d.is_dir():
                plugin_dirs.append(_d)
        plugin_dirs.extend(args.plugin_dir)
        if plugin_dirs:
            try:
                loaded_plugins = load_plugins(plugin_dirs)
            except PluginError as exc:
                raise SystemExit(f"plugin error: {exc}") from exc
            if loaded_plugins.mcp_servers:
                plugin_specs = {s.name: s for s in loaded_plugins.mcp_servers}
                config_specs = {s.name: s for s in mcp_specs}
                merged = {**plugin_specs, **config_specs}
                mcp_specs = tuple(merged.values())

    if args.prompt is None:
        from harness.tui import AppBoundAsk, run_tui
        from harness.tui_support import TuiResolver

        ask = AppBoundAsk()
        resolver = TuiResolver(ask=ask, engine=engine)
        kernel = build_kernel(
            provider=provider,
            base_dir=args.base_dir,
            model=model,
            pricing=pricing,
            resume_session_id=resume_session_id,
            permissions=engine,
            tags=args.tag,
            mcp=mcp_specs or None,
            resolver=resolver,
            plugins=loaded_plugins,
            workspace_root=args.workspace,
            native_tools=True,
            usage_limits=usage_limits,
            pricing_for=pricing_for,
            routing_rules=routing_rules,
            model_pinned=model_pinned,
            inherit_model_selection=args.model is None,
            explicit_model_selection=args.model is not None,
            catalog_path=args.catalog,
            context_policy=context_policy,
            inherit_context_policy=not args.no_context_profile,
        )
        asyncio.run(
            run_tui(
                kernel,
                catalog_path=args.catalog,
                ask=ask,
                native_tools=True,
                workspace_root=args.workspace,
                routing_rules=routing_rules,
            )
        )
        return
    kernel = build_kernel(
        provider=provider,
        base_dir=args.base_dir,
        model=model,
        pricing=pricing,
        pricing_for=pricing_for,
        routing_rules=routing_rules,
        model_pinned=model_pinned,
        inherit_model_selection=args.model is None,
        explicit_model_selection=args.model is not None,
        catalog_path=args.catalog,
        context_policy=context_policy,
        inherit_context_policy=not args.no_context_profile,
        resume_session_id=resume_session_id,
        usage_limits=usage_limits,
        permissions=engine,
        tags=args.tag,
        mcp=mcp_specs or None,
        plugins=loaded_plugins,
        workspace_root=args.workspace,
        native_tools=True,
    )
    from harness.errors import ProviderError

    try:
        print(asyncio.run(_amain(kernel, args.prompt)))
        outcome = kernel.controller.last_result
        if outcome is not None and outcome.status != "completed":
            raise SystemExit(f"task {outcome.status}: {outcome.reason}")
    except ProviderError as exc:
        raise SystemExit(f"provider error: {exc}") from exc


def _parse_add_spec(args, refs: dict, headers: dict):
    """Build a spec from add-flags; round-trips through _parse_server so CLI
    input obeys exactly the same validation laws as a config file.
    """
    from harness.mcp_config import _parse_server

    body: dict = {"restart": args.restart, "tool_timeout_s": args.tool_timeout}
    if args.command is not None:
        body["command"] = args.command
        if args.args:
            body["args"] = args.args
        if args.cwd:
            body["cwd"] = args.cwd
    if args.url is not None:
        body["url"] = args.url
    if refs:
        body["env"] = refs
    if headers:
        body["headers"] = headers
    return _parse_server(args.name, body, source="adhoc")


def _mcp_subcommand(argv: list[str]) -> None:
    from harness.mcp_config import (
        McpConfigError,
        McpServerSpec,
        load_mcp_config,
        load_mcp_file,
        project_mcp_path,
        user_mcp_path,
        write_scope_file,
    )

    parser = argparse.ArgumentParser(prog="harness mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scope", choices=("user", "project"), default="user")
        p.add_argument(
            "--config-home",
            type=Path,
            default=None,
            help="Override the user config dir (for tests).",
        )

    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--command")
    add.add_argument("--arg", action="append", default=[], dest="args")
    add.add_argument("--cwd")
    add.add_argument("--url")
    add.add_argument(
        "--env",
        action="append",
        default=[],
        help="VAR=ENV_VAR_NAME (a reference, never a literal value)",
    )
    add.add_argument(
        "--header",
        action="append",
        default=[],
        dest="headers",
        help="Header=ENV_VAR_NAME holding the full header value",
    )
    add.add_argument("--restart", choices=("never", "on_failure"), default="on_failure")
    add.add_argument("--tool-timeout", type=float, default=60.0, dest="tool_timeout")
    _common(add)

    lst = sub.add_parser(
        "list", help="Show the merged user+project view; --scope does not filter it."
    )
    _common(lst)

    rem = sub.add_parser("remove")
    rem.add_argument("name")
    _common(rem)

    imp = sub.add_parser("import")
    imp.add_argument("path", type=Path)
    imp.add_argument(
        "--write", action="store_true", help="Merge into the scope file instead of printing."
    )
    _common(imp)

    args = parser.parse_args(argv)

    def scope_path() -> Path:
        if args.scope == "project":
            return project_mcp_path(Path.cwd())
        return user_mcp_path(args.config_home)

    def existing() -> dict[str, McpServerSpec]:
        path = scope_path()
        if not path.exists():
            return {}
        return {s.name: s for s in load_mcp_file(path, source=args.scope)}

    try:
        if args.cmd == "add":
            try:
                refs = dict(pair.split("=", 1) for pair in args.env)
                headers = dict(pair.split("=", 1) for pair in args.headers)
            except ValueError:
                raise SystemExit("--env/--header values must be NAME=ENV_VAR_NAME") from None
            spec = _parse_add_spec(args, refs, headers)
            path = scope_path()
            servers = existing()
            servers[spec.name] = spec
            write_scope_file(path, tuple(servers.values()))
            print(f"added {spec.name} ({spec.transport}) to {path}")
        elif args.cmd == "list":
            specs = load_mcp_config(project_dir=Path.cwd(), config_home=args.config_home)
            if not specs:
                print("no mcp servers configured")
            for spec in specs:
                target = spec.command if spec.transport == "stdio" else spec.url
                print(f"{spec.name}\t{spec.transport}\t{spec.source}\t{target}")
        elif args.cmd == "remove":
            path = scope_path()
            servers = existing()
            if args.name not in servers:
                raise SystemExit(f"no server {args.name!r} in {path}")
            del servers[args.name]
            write_scope_file(path, tuple(servers.values()))
            print(f"removed {args.name} from {path}")
        elif args.cmd == "import":
            from harness.mcp_import import McpImportError, convert_mcp_json

            try:
                specs, import_warnings = convert_mcp_json(args.path.read_text())
            except (OSError, McpImportError) as exc:
                raise SystemExit(f"import failed: {exc}") from exc
            for warning in import_warnings:
                print(f"warning: {warning}", file=sys.stderr)
            if not specs:
                print("warning: nothing converted", file=sys.stderr)
            if not args.write:
                from harness.mcp_config import emit_mcp_toml

                print(emit_mcp_toml(tuple(specs)), end="")
                return
            servers = existing()
            inserted = 0
            for spec in specs:
                if spec.name in servers:
                    print(f"warning: {spec.name} already configured; skipped", file=sys.stderr)
                    continue
                servers[spec.name] = spec
                inserted += 1
            path = scope_path()
            write_scope_file(path, tuple(servers.values()))
            print(f"imported {inserted} server(s) into {path}")
    except McpConfigError as exc:
        raise SystemExit(str(exc)) from exc


def _import_subcommand(argv: list[str]) -> None:
    from harness.cc_import import (
        CcImportError,
        _default_catalog,
        convert_plugin,
        eject_plugin,
    )

    parser = argparse.ArgumentParser(prog="harness import")
    parser.add_argument("path", help="Local CC plugin root, OR a target dir with --eject")
    parser.add_argument(
        "--out", type=Path, default=None, help="Output dir (default: ./<plugin-name>)"
    )
    parser.add_argument(
        "--eject",
        action="store_true",
        help="Flip an imported plugin (the path arg) to owned; re-import then refused",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite a non-generated output dir")
    args = parser.parse_args(argv)

    if args.eject:
        try:
            eject_plugin(Path(args.path))
        except CcImportError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"ejected {args.path} (re-import now refused; use --force to override)")
        return

    if args.path.startswith(("http://", "https://", "git@", "ssh://")):
        raise SystemExit(
            "importing from a git URL is not supported in v1; run `git clone <url>` first and"
            " point `harness import` at the cloned plugin root"
        )

    src = Path(args.path)
    from harness.cc_import import read_cc_plugin

    try:
        cc = read_cc_plugin(src)
    except CcImportError as exc:
        raise SystemExit(str(exc)) from exc
    out = args.out or (Path.cwd() / _sanitize_for_out(cc.name))
    try:
        result = convert_plugin(src, out=out, catalog=_default_catalog(), force=args.force)
    except CcImportError as exc:
        raise SystemExit(str(exc)) from exc
    action = "re-imported (overwrote generated output)" if result.overwritten else "imported"
    print(f"{action} {cc.name} -> {result.out}")
    print(f"report: {result.report_path}")


def _sanitize_for_out(name: str) -> str:
    from harness.cc_import import _sanitize_name

    return _sanitize_name(name)


def _resources_subcommand(argv: list[str]) -> None:
    import json
    from harness.catalog import Catalog, UnknownAliasError
    from harness.resources import LocalResources, render_resources

    parser = argparse.ArgumentParser(prog="harness resources", description="Inspect local runtime readiness.")
    parser.add_argument("--catalog", type=Path, default=Path.home() / ".config/harness/models.toml")
    parser.add_argument("--check", metavar="ALIAS", help="Refresh a local inventory; does not launch a runtime.")
    parser.add_argument("--json", action="store_true", help="Print timestamped observations as JSON.")
    args = parser.parse_args(argv)
    try:
        catalog = Catalog.load(args.catalog)
        entries = ([catalog.resolve(args.check)] if args.check else
                   [catalog.resolve(alias) for alias in catalog.aliases() if "local" in catalog.entries[alias]])
    except (OSError, ValueError, UnknownAliasError) as exc:
        raise SystemExit(f"local resource configuration unavailable ({type(exc).__name__})") from None

    async def inspect():
        resources = LocalResources()
        if args.check:
            return [await resources.check(entries[0], emit=lambda e: None)]
        return [resources.snapshot(entry) for entry in entries]

    observations = asyncio.run(inspect())
    print(json.dumps([o.model_dump(mode="json") for o in observations], indent=2)
          if args.json else render_resources(observations))
    if args.check and observations[0].status != "ready":
        raise SystemExit(1)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "budget":
        from harness.budget_cli import main as budget_main
        budget_main(argv[1:])
        return
    if argv and argv[0] == "coordination":
        from harness.coordination_cli import main as coordination_main
        coordination_main(argv[1:])
        return
    if argv and argv[0] == "export":
        from harness.export_cli import main as export_main
        export_main(argv[1:])
        return
    if argv and argv[0] == "models":
        from harness.models_cli import main as models_main
        models_main(argv[1:])
        return
    if argv and argv[0] == "handoff":
        from harness.handoff_cli import main as handoff_main
        handoff_main(argv[1:])
        return
    if argv and argv[0] == "improve":
        from harness.improvement_cli import main as improve_main
        improve_main(argv[1:])
        return
    if argv and argv[0] == "status":
        from harness.status_cli import main as status_main
        status_main(argv[1:])
        return
    if argv and argv[0] == "tasks":
        from harness.task_cli import main as tasks_main
        tasks_main(argv[1:])
        return
    if argv and argv[0] == "semantic":
        from harness.semantic_cli import main as semantic_main
        semantic_main(argv[1:])
        return
    if argv and argv[0] == "resources":
        _resources_subcommand(argv[1:])
        return
    if argv and argv[0] in ("stats", "compare", "outcome", "improvements"):
        _subcommand(argv)
        return
    if argv and argv[0] == "mcp":
        _mcp_subcommand(argv[1:])
        return
    if argv and argv[0] == "import":
        _import_subcommand(argv[1:])
        return
    from harness.model_selection import ModelSelectionError
    from harness.execution import BudgetExceeded
    try:
        _run_main()
    except ModelSelectionError as exc:
        raise SystemExit(str(exc)) from None
    except (BudgetExceeded, UsageBudgetConfigError) as exc:
        raise SystemExit(f"stopped: {exc}") from None
