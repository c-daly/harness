"""Read-only diagnostics for a fresh Harness session. Never launches a provider."""

import argparse
import json
import os
import shutil
from pathlib import Path

from harness.catalog import Catalog, CatalogConfigError, UnknownAliasError
from harness.context import ContextPolicy
from harness.resident import ResidentConfig
from harness.routing import RoutingConfigError, load_routing
from harness.setup_cli import AGENT_EXECUTABLES, subscription_transport_description


def inspect_configuration(catalog_path, resident_path, override):
    checks = []

    def report(name, ok, detail):
        checks.append({"check": name, "ok": ok, "detail": detail})

    try:
        catalog = Catalog.load(catalog_path)
    except CatalogConfigError as exc:
        report("catalog", False, str(exc))
        return checks
    except OSError as exc:
        report("catalog", False, f"{catalog_path}: {exc.strerror}. Run harness setup or supply --catalog.")
        return checks
    report("catalog", True, f"{catalog_path} ({len(catalog.entries)} aliases)")
    try:
        resident = ResidentConfig.load(resident_path)
        if resident.context_profile:
            ContextPolicy.load(resident.context_profile)
    except (OSError, ValueError) as exc:
        path = resident_path or Path.home() / ".config/harness/resident.toml"
        report("resident", False, f"Check {path} and its context_profile ({type(exc).__name__}).")
        return checks
    try:
        rules = load_routing(project_dir=Path.cwd())
    except (RoutingConfigError, OSError, ValueError, TypeError, AttributeError):
        report("routing", False, "Check the project and user routing.toml files.")
        return checks
    routed = rules.default if rules else None
    alias = override or routed or resident.model
    source = "--model" if override else "routing default" if routed else "resident default"
    if not alias:
        report("selection", False, "Choose a registered alias with --model ALIAS or run harness setup.")
        return checks
    try:
        resolved = catalog.resolve(alias)
    except UnknownAliasError:
        report("selection", False, f"{source} selects an absent alias. Run harness models and choose one.")
        return checks
    except CatalogConfigError as exc:
        report("selection", False, str(exc))
        return checks
    kind = "external agent" if resolved.backend else "inference for Saoirse"
    report("selection", True, f"{alias!r} ({source}, {kind})")
    if resolved.backend or resolved.transport == "claude-cli":
        exe = AGENT_EXECUTABLES[resolved.backend] if resolved.backend else "claude"
        found = shutil.which(exe) is not None
        report("executable", found, f"{exe} on PATH" if found else f"Install {exe} and add it to PATH.")
        if resolved.transport and found:
            report("transport", True, subscription_transport_description())
    if resolved.api_key_env:
        present = bool(os.environ.get(resolved.api_key_env, "").strip())
        report("credential", present, "Environment variable set." if present else
               f"Set the environment variable named by models.{alias!r}.api_key_env in your shell.")
    if resolved.local:
        missing = [p for p in resolved.local.required_files if not p.is_file()]
        report("runtime files", not missing, "Required files present." if not missing else
               "A required runtime file is missing. Check the local profile or register the GGUF again.")
    return checks


def main(argv):
    parser = argparse.ArgumentParser(prog="harness doctor", description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path.home() / ".config/harness/models.toml")
    parser.add_argument("--resident-config", type=Path)
    parser.add_argument("--model", metavar="ALIAS", help="Check an explicit alias for a fresh session.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    checks = inspect_configuration(args.catalog, args.resident_config, args.model)
    passed = all(check["ok"] for check in checks)
    scope = "Configuration only. Live connectivity, authentication and inference are untested."
    if args.json:
        print(json.dumps({"ok": passed, "scope": scope, "checks": checks}))
    else:
        for check in checks:
            print(f"{'ok' if check['ok'] else 'fix'}  {check['check']}: {check['detail']}")
        print(scope)
    if not passed:
        raise SystemExit(1)
