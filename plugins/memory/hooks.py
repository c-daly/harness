"""Memory plugin hooks. Fail-open is the law: a broken store must never
break a session (the real plugin's Provider Principle)."""

import importlib.util as _ilu
import os
from pathlib import Path as _Path

from harness.hooks import Inject

# Self-load the sibling store module: synthetic module names break plain
# sibling imports, so we use importlib directly. This is the v1 pattern
# for same-dir imports in the harness plugin system (documented in README.md).
_spec = _ilu.spec_from_file_location(
    "harness_plugin_memory_store", _Path(__file__).parent / "store.py"
)
store = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(store)


def session_brief(ctx):
    """SESSION_START lifecycle hook: inject the memory brief into context.

    Fail-open: an unreadable/missing memory dir returns [] (never raises).
    Kill-switch: HARNESS_MEMORY_BRIEF=0 disables the hook entirely.

    v1 caveat: store.brief() emits EVERY user-level description, so a store
    with hundreds of entries grows this single per-session injection without
    bound. A future version should cap the brief at N entries / K bytes.
    """
    if os.environ.get("HARNESS_MEMORY_BRIEF", "1") == "0":
        return []
    raw = os.environ.get("HARNESS_MEMORY_DIR")
    root = _Path(raw) if raw else _Path.home() / ".local" / "share" / "harness" / "memory"
    try:
        text = store.brief(root)
    except Exception:
        return []  # fail-open, always
    return [Inject(text=text)]
