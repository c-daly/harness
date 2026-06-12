"""Headless entrypoint. Phase 1: FakeProvider demo; Phase 2: catalog/--model/--resume/SIGINT."""

import argparse
import asyncio
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from harness.hooks import HookBus
from harness.interaction import HeadlessResolver, Resolver
from harness.loop import AgentLoop
from harness.mcp_config import McpConfigError, McpServerSpec, load_mcp_config, load_mcp_file
from harness.mcp_host import McpHost
from harness.permissions import PermissionEngine, default_engine
from harness.provider import FakeProvider, ModelProvider, text_turn
from harness.session import Session
from harness.subagent import DispatchAgentTool, SubagentRunner
from harness.tools import ToolRegistry
from harness.types import ModelId, SessionId, new_session_id

if TYPE_CHECKING:
    from harness.plugins import LoadedPlugins


@dataclass
class Kernel:
    session: Session
    loop: AgentLoop
    registry: ToolRegistry
    hooks: HookBus
    provider: ModelProvider
    resumed: bool = field(default=False)
    tags: list[str] = field(default_factory=list)
    mcp: McpHost | None = None
    plugins: "LoadedPlugins | None" = None
    plugin_warnings: list[str] = field(default_factory=list)
    _plugin_pumps: list = field(default_factory=list)


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
    mcp: Sequence[McpServerSpec] | None = None,  # None disables MCP entirely
    plugins: "LoadedPlugins | None" = None,
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
        agents=agents_sink,
    )
    registry.register(DispatchAgentTool(runner=runner, parent=session))
    loop_kwargs: dict = dict(
        session=session,
        provider=provider,
        registry=registry,
        hooks=hooks,
        resolver=resolver,
        model=model,
        system_prompt=system_prompt,
        pricing=pricing,
    )
    if transcript is not None:
        loop_kwargs["history"] = transcript
    loop = AgentLoop(**loop_kwargs)
    mcp_host = None
    if mcp:
        mcp_host = McpHost(mcp, registry=registry, hooks=hooks, session=session)
    return Kernel(
        session=session,
        loop=loop,
        registry=registry,
        hooks=hooks,
        provider=provider,
        resumed=resumed,
        tags=tags or [],
        mcp=mcp_host,
        plugins=plugins,
        plugin_warnings=plugin_warnings,
    )


async def run_once(kernel: Kernel, prompt: str) -> str:
    from harness.events import CustomEvent, UserInterrupt
    from harness.plugins import start_subscriber_pumps

    pump_tasks: list = []
    try:
        # host.start() BEFORE loop.start(): the instructions hook must be registered
        # before SESSION_START fires so it can inject into the system prompt.
        if kernel.mcp is not None:
            for warning in await kernel.mcp.start():
                print(f"warning: {warning}", file=sys.stderr)
        if not kernel.resumed:
            await kernel.loop.start()
        # tags are per-run annotations: emitted after start (new) or before the
        # turn (resumed) -- i.e., here, unconditionally
        for t in kernel.tags:
            kernel.session.append(CustomEvent(namespace="harness", name="tag", data={"tag": t}))
        # flush AFTER loop.start()+tags: nothing precedes SessionStarted in the log.
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
        if pump_tasks:
            for _task in pump_tasks:
                _task.cancel()
            await asyncio.gather(*pump_tasks, return_exceptions=True)
        # stop() then flush() BEFORE session.close(): server_stopped lands in the log;
        # closed sessions drop events.
        # server teardown is post-session: session_ended is NOT the final envelope when
        # MCP is active (mcp/server_stopped follows). Teardown is administrative, not
        # conversational, so it must not precede SESSION_END hooks.
        if kernel.mcp is not None:
            await kernel.mcp.stop()
            # crash path: if flush never ran (loop.start raised), drain the buffered
            # mcp events now as post-crash diagnostics; no-op on the happy path
            kernel.mcp.flush_events()
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
    if command == "stats":
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
            raise SystemExit(str(exc).strip("'\"")) from exc
    elif command == "outcome":
        parser.add_argument("session_id")
        parser.add_argument("status", choices=("ok", "fail", "abandoned"))
        parser.add_argument("--score", type=float, default=None)
        parser.add_argument("--note", default="")
        args = parser.parse_args(rest)
        from harness.events import SessionOutcome
        from harness.log import SessionLockedError
        from harness.types import SessionId

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
    parser = argparse.ArgumentParser(prog="harness")
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
    parser.add_argument(
        "--resume", dest="resume_session_id", default=None, help="Session ID to resume."
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
    elif args.prompt is not None:
        provider = FakeProvider([text_turn(f"echo: {args.prompt}")])
        model = ModelId("fake:echo")
        pricing = None
    else:
        from harness.provider import EchoProvider

        provider = EchoProvider()
        model = ModelId("echo")
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
        )
        # Textual owns the terminal: no SIGINT handler here (Esc interrupts; Ctrl+C quits)
        asyncio.run(run_tui(kernel, catalog_path=args.catalog, ask=ask))
        return
    kernel = build_kernel(
        provider=provider,
        base_dir=args.base_dir,
        model=model,
        pricing=pricing,
        resume_session_id=resume_session_id,
        permissions=engine,
        tags=args.tag,
        mcp=mcp_specs or None,
        plugins=loaded_plugins,
    )
    from harness.errors import ProviderError

    try:
        print(asyncio.run(_amain(kernel, args.prompt)))
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


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("stats", "compare", "outcome"):
        _subcommand(argv)
        return
    if argv and argv[0] == "mcp":
        _mcp_subcommand(argv[1:])
        return
    _run_main()
