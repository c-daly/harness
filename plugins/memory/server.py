"""Memory plugin MCP server.

Four append-only tools wrapping the flat store:
  memory_write  -- write a new entry
  memory_get    -- retrieve entry markdown by name+entry_type
  memory_list   -- list entries with optional entry_type/subject filter
  memory_brief  -- return the current memory brief

Errors are returned as values ("error: ..."), never raised to the client.
Run: python3 server.py  (stdio transport)
"""

import importlib.util
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Self-load the sibling store module (isolated; does not pollute sys.path)
_spec = importlib.util.spec_from_file_location(
    "harness_plugin_memory_store", Path(__file__).parent / "store.py"
)
store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(store)

mcp = FastMCP(
    "memory",
    instructions="Durable observation memory; write with memory_write.",
)


def _root() -> Path:
    """Resolve the memory store root from HARNESS_MEMORY_DIR env, or default."""
    env = os.environ.get("HARNESS_MEMORY_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "harness" / "memory"


@mcp.tool()
def memory_write(
    entry_type: str,
    name: str,
    subject: str,
    description: str,
    body: str,
) -> str:
    """Write a new memory entry. Returns the relative path on success.

    entry_type must be one of: user, feedback, project, reference.
    Append-only: existing name+entry_type pairs cannot be overwritten.
    """
    try:
        return store.write(
            _root(),
            entry_type=entry_type,
            name=name,
            subject=subject,
            description=description,
            body=body,
        )
    except Exception as exc:
        return f"error: {exc}"


@mcp.tool()
def memory_get(name: str, entry_type: str) -> str:
    """Retrieve the full markdown for a memory entry by name and entry_type."""
    try:
        result = store.get(_root(), name, entry_type)
        if result is None:
            return f"error: entry name={name!r} type={entry_type!r} not found"
        return result
    except Exception as exc:
        return f"error: {exc}"


@mcp.tool()
def memory_list(entry_type: str = "", subject: str = "") -> str:
    """List memory entries, optionally filtered by entry_type and/or subject."""
    try:
        entries = store.list_entries(
            _root(),
            entry_type=entry_type if entry_type else None,
            subject=subject if subject else None,
        )
        if not entries:
            return "(no entries)"
        lines = []
        for e in entries:
            nm = e.get("name", "?")
            tp = e.get("type", "?")
            sb = e.get("subject", "?")
            desc = e.get("description", "")
            lines.append(f"{nm} ({tp}, {sb}): {desc}")
        return "\n".join(lines)
    except Exception as exc:
        return f"error: {exc}"


@mcp.tool()
def memory_brief() -> str:
    """Return the current memory brief (# Memory header with user-level bullets)."""
    try:
        return store.brief(_root())
    except Exception as exc:
        return f"error: {exc}"


if __name__ == "__main__":
    mcp.run("stdio")
