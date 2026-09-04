#!/usr/bin/env bash
# Smoke-test a built harness wheel: install it into a throwaway virtualenv, run
# the console script, and import every module the package ships.
#
# Usage: scripts/smoke_wheel.sh dist/harness-0.0.1-py3-none-any.whl
#
# Runs from any working directory and leaves the repo checkout and its .venv
# untouched. The throwaway venv lives in a mktemp dir removed on every exit; uv
# still writes to its own cache and managed-Python dirs outside that tempdir.
set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <path-to-wheel>" >&2
    exit 2
}

if [ "$#" -ne 1 ]; then
    usage
fi

if [ ! -f "$1" ]; then
    echo "error: not a file: $1" >&2
    usage
fi

# Preflight: the smoke installs the wheel into a uv-managed venv, so uv must be
# on PATH before anything else runs.
if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv not found on PATH" >&2
    echo "  the wheel smoke installs the built wheel into a uv-managed venv, so uv is required" >&2
    echo "  install uv (https://docs.astral.sh/uv/) or add it to PATH, then re-run" >&2
    exit 1
fi

# Resolve to absolute paths before changing directory anywhere.
wheel="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

# The module list comes from the source tree, but the imports run against the
# installed wheel: a module the wheel fails to ship is a failure, not a skip.
modules=()
for path in "$repo_root"/src/harness/*.py; do
    stem="$(basename "$path" .py)"
    if [ "$stem" = "__init__" ]; then
        continue
    fi
    modules+=("harness.$stem")
done

if [ "${#modules[@]}" -eq 0 ]; then
    echo "error: no modules found under $repo_root/src/harness" >&2
    exit 1
fi

tmp="$(mktemp -d)"
cleanup() {
    rm -rf "$tmp"
}
trap cleanup EXIT

echo "== creating a clean venv"
# SMOKE_PYTHON lets CI pin the matrix interpreter; it defaults to 3.13, the
# version CI runs the smoke on.
uv venv --python "${SMOKE_PYTHON:-3.13}" "$tmp/venv"

echo "== installing $wheel"
uv pip install --python "$tmp/venv/bin/python" "$wheel"

echo "== running the console script: harness --help"
# Strip any inherited PYTHONPATH/PYTHONHOME so a source tree cannot shadow the
# installed wheel.
if ! help_out="$(env -u PYTHONPATH -u PYTHONHOME "$tmp/venv/bin/harness" --help 2>&1)"; then
    echo "error: harness --help exited nonzero" >&2
    echo "$help_out" >&2
    exit 1
fi
echo "ok harness --help"

echo "== importing ${#modules[@]} modules from the installed wheel"
# Leave the checkout so that src/ can never shadow the installed package.
cd "$tmp"
env -u PYTHONPATH -u PYTHONHOME "$tmp/venv/bin/python" - "${modules[@]}" <<"PY"
import importlib
import sys

failed = []
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # a broken import can raise anything at all
        failed.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"FAIL {name}")
    else:
        print(f"ok {name}")

if failed:
    print(f"{len(failed)} module(s) failed to import:", file=sys.stderr)
    for name, err in failed:
        print(f"  {name}: {err}", file=sys.stderr)
    sys.exit(1)
PY

echo "== wheel smoke OK"
