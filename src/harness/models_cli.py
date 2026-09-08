"""Shared operator model controls for the terminal and headless CLI."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from harness.model_management import ModelSetupError, inspect_hub, register_model, render_catalog, render_hub


HELP = """Model setup:
  /models                         List configured models and agents
  /models hub OWNER/REPO [--revision REF] [--offline] [--json]
  /models add ALIAS --file GGUF --runtime LLAMA_SERVER [options]
Add options: --library-path DIR --port PORT --context TOKENS --gpu-layers N
             --threads N --disable-thinking
             --hub OWNER/REPO --revision REF --hub-file FILENAME --offline
Registration adds a new alias; existing aliases are preserved. Select it with /model ALIAS.
Hub inspection downloads metadata only. Install weights and the runtime separately.
CLI: harness models [--catalog FILE] [--cache-dir DIR] <same action and options>."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ModelSetupError(message)


def parser(*, catalog_path=None, cache_dir=None):
    root = _Parser(prog="models", add_help=False, allow_abbrev=False)
    root.add_argument("--catalog", type=Path, default=catalog_path or Path.home()/".config/harness/models.toml")
    root.add_argument("--cache-dir", type=Path, default=cache_dir or Path.home()/".cache/harness/model-index")
    sub = root.add_subparsers(dest="action", parser_class=_Parser)
    sub.add_parser("list", add_help=False)
    hub = sub.add_parser("hub", add_help=False)
    hub.add_argument("repo")
    hub.add_argument("--revision", default="main")
    hub.add_argument("--offline", action="store_true")
    hub.add_argument("--json", action="store_true")
    add = sub.add_parser("add", add_help=False)
    add.add_argument("alias")
    add.add_argument("--file", dest="model_file", type=Path, required=True)
    add.add_argument("--runtime", type=Path, required=True)
    add.add_argument("--library-path", type=Path)
    add.add_argument("--port", type=int, default=8080)
    add.add_argument("--context", type=int, default=8192)
    add.add_argument("--gpu-layers", type=int, default=99)
    add.add_argument("--threads", type=int, default=4)
    add.add_argument("--disable-thinking", action="store_true")
    add.add_argument("--hub")
    add.add_argument("--revision", default="main")
    add.add_argument("--hub-file")
    add.add_argument("--offline", action="store_true")
    return root


async def perform(words: list[str], *, catalog_path=None, cache_dir=None, progress=lambda text: None) -> str:
    if words == ["help"] or any(word in ("--help", "-h") for word in words):
        return HELP
    args = parser(catalog_path=catalog_path, cache_dir=cache_dir).parse_args(words)
    if args.action in (None, "list"):
        return render_catalog(args.catalog)
    if args.action == "hub":
        progress("Reading Hugging Face model metadata; Esc cancels.")
        inventory, cached = await inspect_hub(args.repo, args.revision, cache_dir=args.cache_dir, offline=args.offline)
        return (json.dumps({"inventory": inventory.model_dump(mode="json"), "cached": cached}, indent=2)
                if args.json else render_hub(inventory, cached))
    inventory = expected = None
    cached = False
    if args.hub:
        progress("Reading Hugging Face metadata before verifying the installed file; Esc cancels.")
        inventory, cached = await inspect_hub(args.hub, args.revision, cache_dir=args.cache_dir, offline=args.offline)
        name = args.hub_file or args.model_file.name
        expected = next((f for f in inventory.files if f.filename == name), None)
        if expected is None:
            raise ModelSetupError("That GGUF filename is not listed in the selected Hub revision; use --hub-file.")
    elif args.hub_file:
        raise ModelSetupError("--hub-file requires --hub OWNER/REPO.")
    progress("Verifying the installed GGUF; Esc cancels before registration.")
    result = await register_model(catalog_path=args.catalog, alias=args.alias, model_file=args.model_file,
        runtime=args.runtime, library_path=args.library_path, port=args.port, context=args.context,
        gpu_layers=args.gpu_layers, threads=args.threads, disable_thinking=args.disable_thinking,
        inventory=inventory, expected=expected, progress=progress)
    lines = [f"Registered {args.alias} in {result['catalog']}.",
             f"SHA-256: {result['artifact']['sha256']}"]
    if inventory:
        lines.append(f"File matches {inventory.repo}@{inventory.revision} ({'cached' if cached else 'live'} metadata).")
    if result["backup"]:
        lines.append(f"Previous catalog backed up to {result['backup']}.")
    lines.extend([f"Select with /model {args.alias}; the model starts on the next turn.",
                  "Registration checks the file and setup; inference quality and hardware fit are untested."])
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    try:
        print(asyncio.run(perform(argv, progress=lambda text: print(text.replace("Esc", "Ctrl-C"), file=sys.stderr))))
    except KeyboardInterrupt:
        raise SystemExit("Model setup cancelled; no further registration will occur.") from None
    except (ModelSetupError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ModelSetupError) else f"Model setup failed ({type(exc).__name__})."
        raise SystemExit(message) from None
