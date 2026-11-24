"""Configure the existing catalog and first-session default from the terminal."""

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from harness.catalog import UnknownAliasError
from harness.model_management import ModelSetupError, load_setup_catalog, register_connection, register_model
from harness.persistence import PublishedWriteError, atomic_write
from harness.resident import ResidentConfig
from harness.context import ContextPolicy
from harness.resources import local_endpoint
from harness.routing import RoutingConfigError, load_routing


AGENT_EXECUTABLES = {"claude-code": "claude", "codex": "codex", "antigravity": "agy"}


def subscription_transport_description():
    from harness.provider_cli_model import sdk_available

    if sdk_available():
        return "Claude Agent SDK detected and preferred. Direct CLI fallback is available before submission."
    return "Direct Claude CLI available. The standard installation includes the SDK. Check your Harness environment."


def _parser():
    parser = argparse.ArgumentParser(
        prog="harness setup", description="Connect a resource for your first session. "
        "Run without arguments for guided setup. Configuration makes no inference calls."
    )
    modes = parser.add_subparsers(dest="mode")
    local = modes.add_parser("local", help="Connect a running OpenAI-compatible loopback server.")
    local.add_argument("alias")
    local.add_argument("--url", required=True)
    local.add_argument("--model", required=True, help="Model identifier served by the endpoint.")
    api = modes.add_parser("api", help="Connect a provider through LiteLLM.")
    api.add_argument("alias")
    api.add_argument("--route", required=True, help="Provider/model route.")
    api.add_argument("--key-env", required=True, help="Name of the variable holding the API key.")
    cli = modes.add_parser("cli", help="Use a Claude model for Saoirse via SDK or direct CLI with your subscription.")
    cli.add_argument("alias")
    cli.add_argument("--model", default="default", help="Claude model name, or the CLI account default.")
    agent = modes.add_parser("agent", help="Register an installed external agent CLI.")
    agent.add_argument("alias")
    agent.add_argument("--backend", choices=AGENT_EXECUTABLES, required=True)
    gguf = modes.add_parser("gguf", help="Register an installed GGUF and llama-server.")
    gguf.add_argument("alias")
    gguf.add_argument("--file", required=True, type=Path)
    gguf.add_argument("--runtime", required=True, type=Path)
    existing = modes.add_parser("existing", help="Use an already registered alias.")
    existing.add_argument("alias")
    return parser


def _guided(catalog):
    print("Connect Harness\n  local     Running local model server\n"
          "  cli       Claude model via your CLI subscription (Saoirse runs the task)\n"
          "  api       Provider API\n  agent     External agent CLI\n"
          "  gguf      Installed GGUF and llama-server")
    if catalog.entries:
        print("  existing  Registered alias (" + ", ".join(map(repr, catalog.aliases())) + ")")
    mode = input("Connection type: ").strip().lower()
    if mode not in ("local", "cli", "api", "agent", "gguf", "existing"):
        raise ModelSetupError("Choose local, cli, api, agent, gguf, or existing.")
    alias = input("Alias: ").strip()
    # Terminal answers are literal field values. A leading dash must never
    # turn an answer into a CLI flag or terminate the retry loop.
    selection = argparse.Namespace(mode=mode, alias=alias)
    if mode == "local":
        selection.url = input("Loopback endpoint [http://127.0.0.1:8080/v1]: ").strip() or "http://127.0.0.1:8080/v1"
        selection.model = input("Served model name: ").strip()
    elif mode == "cli":
        selection.model = input("Claude model [default]: ").strip() or "default"
    elif mode == "api":
        selection.route = input("Provider/model route: ").strip()
        selection.key_env = input("API key environment variable name (leave the key in your environment): ").strip()
    elif mode == "agent":
        print("  " + ", ".join(f"{name} ({'found' if shutil.which(exe) else 'missing'})"
                              for name, exe in AGENT_EXECUTABLES.items()))
        backend = input("Agent backend: ").strip()
        if backend not in AGENT_EXECUTABLES:
            raise ModelSetupError("Choose one of the listed agent backends.")
        selection.backend = backend
    elif mode == "gguf":
        selection.file = Path(input("Installed GGUF path: ").strip()).expanduser()
        selection.runtime = Path(input("llama-server executable path: ").strip()).expanduser()
    return selection


def _model_name(value):
    if not re.fullmatch(r"[\w.:-]+(?:/[\w.:+-]+)*", value):
        raise ModelSetupError("Enter a model identifier, for example provider/model.")
    return value


def _connect(args, path, catalog):
    if not args.alias.isprintable():
        raise ModelSetupError("Use a printable alias.")
    if args.mode == "existing":
        if args.alias not in catalog.entries:
            raise ModelSetupError("Alias is absent. Run 'harness models' to list configured names.")
        catalog.resolve(args.alias)
        return
    if args.mode == "gguf":
        result = asyncio.run(register_model(catalog_path=path, alias=args.alias,
            model_file=args.file, runtime=args.runtime, progress=lambda line: print(line, file=sys.stderr)))
        if result["warning"]:
            raise ModelSetupError(result["warning"])
        return
    if args.mode == "local":
        entry = {"route": "openai/" + _model_name(args.model), "api_base": local_endpoint(args.url),
                 "tags": ["local"], "local": {}}
    elif args.mode == "cli":
        if not shutil.which("claude"):
            raise ModelSetupError("Install Claude Code and sign in with your subscription, then retry setup.")
        entry = {"route": f"claude-cli/{args.model}", "transport": "claude-cli"}
    elif args.mode == "agent":
        exe = AGENT_EXECUTABLES[args.backend]
        if not shutil.which(exe):
            raise ModelSetupError(f"Install {exe} and put it on PATH, then retry setup.")
        entry = {"backend": args.backend, "route": f"{args.backend}/default"}
    else:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", args.key_env):
            raise ModelSetupError("Use an environment variable name for --key-env.")
        entry = {"route": _model_name(args.route), "api_key_env": args.key_env}
    register_connection(path, args.alias, entry)


def main(argv):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.mode and not sys.stdin.isatty():
        parser.error("use a setup subcommand in scripts, or run harness setup in a terminal")
    config = Path.home() / ".config/harness"
    catalog_path = config / "models.toml"
    try:
        try:
            rules = load_routing(project_dir=Path.cwd())
        except (RoutingConfigError, OSError, ValueError, TypeError, AttributeError):
            raise ModelSetupError("Check the project and user routing.toml files, then retry setup.") from None
        while True:
            try:
                catalog = load_setup_catalog(catalog_path)
                try:
                    resident = ResidentConfig.load()
                    if resident.context_profile:
                        ContextPolicy.load(resident.context_profile)
                except (ValueError, OSError) as exc:
                    raise ModelSetupError(
                        f"Check {config / 'resident.toml'} and its context_profile "
                        f"({type(exc).__name__}), then retry setup."
                    ) from None
                selection = args if args.mode else _guided(catalog)
                _connect(selection, catalog_path, catalog)
                break
            except ValueError as exc:
                if args.mode:
                    raise
                print(str(exc), file=sys.stderr)
                if input("Retry setup? [Y/n] ").strip().lower() not in ("", "y", "yes"):
                    return
        alias = selection.alias
        try:
            connection = load_setup_catalog(catalog_path).resolve(alias)
        except UnknownAliasError:
            raise ModelSetupError("The connection changed during setup. Check the catalog and retry.") from None
        external = connection.execution_kind == "agent"
        if not external:
            # An existing resident file can carry only a context profile. Preserve
            # it as well as any explicitly selected model.
            try:
                atomic_write(config / "resident.toml", f"model = {json.dumps(alias, ensure_ascii=False)}\n".encode(),
                             replace=False)
            except FileExistsError:
                print("Kept your existing resident settings.")
        if connection.transport == "claude-cli":
            print(subscription_transport_description())
        if connection.api_key_env and not os.environ.get(connection.api_key_env, "").strip():
            print("Set the API key environment variable you named before running the first prompt.")
        default = (rules.default if rules else None) or ResidentConfig.load().model
        if not external and default == alias:
            print("Start a session: harness")
        else:
            # Existing catalogs can contain aliases that look like option names.
            option_words = [f"--model={alias}"] if alias.startswith("-") else ["--model", alias]
            label = "Run the external agent for one session" if external else "Pin this model for one session"
            print(f"{label}: " + shlex.join(["harness", *option_words]))
        print("In a project with a README, ask: Read the README and tell me how to run this project.")
        print("Run harness doctor to inspect the configuration.")
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("Setup interrupted. Run harness setup again; use existing for a registered alias.") from None
    except PublishedWriteError:
        raise SystemExit("Settings were published but could not be synced. Run harness doctor before retrying.") from None
    except OSError as exc:
        raise SystemExit(f"Cannot access setup files ({exc.strerror}). Check permissions and retry.") from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
