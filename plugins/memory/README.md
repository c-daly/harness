# memory plugin

Golden reference plugin for the harness agent framework. Implements a flat,
append-only memory store with a fail-open SESSION_START brief, four MCP tools,
one skill, and one command.

## Layout

```
plugins/memory/
  plugin.toml          -- manifest: hooks, MCP server, emitter namespace
  store.py             -- flat store (stdlib + yaml only)
  server.py            -- FastMCP server wrapping the store
  hooks.py             -- SESSION_START lifecycle hook (session_brief)
  skills/remembering.md   -- when/how to write memories
  commands/brief.md    -- /brief command
  README.md            -- this file
```

## Store layout

Entries are stored as `<root>/<subject>/<YYYY-MM-DD>-<name>.md` with YAML
frontmatter (`name`, `description`, `type`, `subject`). The store is
append-only: name+entry_type collisions raise `ValueError`.

The on-disk frontmatter KEY is `type:`. The Python/wire parameter is named
`entry_type` (it would otherwise shadow the builtin `type`); the two refer to
the same value.

Valid types: `user`, `feedback`, `project`, `reference`.

Both `name` and `subject` are validated against `[A-Za-z0-9_-]+` before they
reach the filesystem. This rejects path separators and `..`, so a write can
never escape the store root.

Writes are atomic: the entry is written to a `.tmp` sibling then renamed into
place (atomic on POSIX), so a crash mid-write never leaves a truncated entry.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HARNESS_MEMORY_DIR` | `~/.local/share/harness/memory` | Store root |
| `HARNESS_MEMORY_BRIEF` | `1` | Set to `0` to disable the SESSION_START brief |

### Brief size (v1 caveat)

The v1 SESSION_START brief includes EVERY user-level description. A store with
hundreds of entries will grow the per-session injection without bound. A future
version should cap the brief at N entries / K bytes; for now, the kill-switch
(`HARNESS_MEMORY_BRIEF=0`) is the only mitigation.

## Trust model

Installing a plugin means trusting its code. The memory plugin reads and writes
only under `HARNESS_MEMORY_DIR`; it never executes stored content.

## Sibling-import pattern (v1)

Hook modules are loaded with synthetic names
(`harness_plugin_<plugin>_<relpath>`), which breaks plain relative imports.
Instead, `hooks.py` and `server.py` self-load `store.py` by file path using
`importlib.util.spec_from_file_location`. This is intentional and isolated --
it does not pollute `sys.path`.

```python
_spec = importlib.util.spec_from_file_location(
    "harness_plugin_memory_store", Path(__file__).parent / "store.py"
)
store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(store)
```
