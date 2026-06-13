# Harness — Writing a Plugin

A plugin is a directory that extends the harness with any of **eight
primitives**: skills, commands, agents, dispatch hooks, lifecycle hooks,
subscribers, MCP servers, and emitter namespaces. This guide shows how to build
one. The complete working reference is `plugins/memory/` in this repo — read it
alongside this document.

For where plugins are discovered and how users enable them, see
[user-guide.md](user-guide.md#plugins). For the kernel internals these
primitives hook into, see [architecture.md](architecture.md).

---

## Anatomy

```
my-plugin/
├── plugin.toml            # the manifest (required)
├── skills/
│   └── remembering.md     # auto-discovered skill
├── commands/
│   └── brief.md           # auto-discovered slash command
├── agents/
│   └── curator.md         # auto-discovered agent definition
├── hooks.py               # Python: dispatch hooks, lifecycle hooks, subscribers
└── server.py              # an MCP server the manifest launches
```

Skills, commands, and agents are **auto-discovered** from their directories —
they don't need to be listed in the manifest. Everything that runs Python code
or launches a process **is** declared in the manifest, because the harness must
validate it at load time.

---

## The manifest: `plugin.toml`

Here is the real manifest from the memory plugin, annotated:

```toml
[plugin]
name = "memory"                          # [A-Za-z0-9_-]+, no "__", required
version = "0.1.0"
description = "Durable observation memory (golden reference plugin)"

[hooks]
module = "hooks.py"                      # the Python module holding callables

[[hooks.lifecycle]]
name = "brief"                           # unique name
function = "session_brief"               # callable in hooks.py
point = "session_start"                  # session_start | session_end

[mcp.servers.memory]
command = "python3"
args = ["${PLUGIN_ROOT}/server.py"]      # ${PLUGIN_ROOT} = this plugin's dir

[emitters]
namespaces = ["memory"]                  # CustomEvent namespaces this plugin owns
```

Other manifest tables:

```toml
[[hooks.dispatch]]
name = "guard"
function = "guard_writes"
priority = 100                           # must be < 1000 (permissions is 1000)

[[subscribers]]
name = "audit"
module = "hooks.py"
function = "audit"                       # must be an async function
```

`[depends]` declares inter-plugin dependencies, validated at load.

### Manifest laws

These are enforced at load time and will refuse a plugin loudly:

- **Names** match `[A-Za-z0-9_-]+`, contain no `__`, and are unique within their
  kind.
- **Reserved namespaces** `harness`, `mcp`, `annotation`, and `plugin` cannot be
  claimed by `[emitters]`.
- **Dispatch hook priority** must be `< 1000`. The permission engine is the
  innermost hook at exactly 1000; nothing may sit inside it.
- **MCP env values are env-var names, never literals.** `${PLUGIN_ROOT}` is
  substituted in `command`/`args`/`cwd` (not in env values).
- **Loading is all-or-nothing.** If any primitive is invalid, the whole plugin
  fails to load and nothing it declares registers.

---

## Skills

A skill is a named block of instructions the model can pull into context on
demand via the `invoke_skill` tool. File: `skills/<name>.md`.

```markdown
---
name: remembering
description: When and how to write memories
---

Write memories for durable preferences, corrections, and decisions that
should persist across sessions. One fact per entry.
```

`name` and `description` are the frontmatter; the body is the instruction text.
At session start, a lifecycle hook injects an inventory of available skills so
the model knows what it can invoke; calling `invoke_skill` returns the body.

---

## Commands

A command is a slash command for the TUI. File: `commands/<name>.md`.

```markdown
---
name: brief
description: Ask for the current memory brief
---

Summarize what you remember about me and my projects.
$ARGUMENTS
```

Typing `/brief some text` submits the body as a normal turn, with `$ARGUMENTS`
replaced by `some text`. Commands run through the **same turn path** as typed
input — same permission checks, same history, same single-turn guard. They are
not a backdoor around the loop.

---

## Agents

An agent definition parametrizes a subagent. File: `agents/<name>.md`.

```markdown
---
name: curator
description: Reviews and organizes notes
tools: [read_file, grep, invoke_skill]
model: sonnet
---

You are the curator. Survey existing notes before proposing changes.
```

The body becomes the child's system prompt. `tools` is an allow-list applied via
a `FilteredRegistry` — the child sees *only* those tools. `model` is a catalog
alias; if it doesn't resolve, the dispatch falls back to the default and the
choice is reported. The `dispatch_agent` tool launches the named agent.

The agents primitive is a **filtered registry view, never a dispatch hook** —
tool restriction happens at registry-construction time, because the shared hook
bus has no per-session identity to scope against.

---

## Hooks (Python)

Hooks and subscribers are Python callables in the module named by
`[hooks] module`. The harness loads that module via `importlib` under a
synthetic name, so it is isolated even if two plugins ship a `hooks.py`.

### Lifecycle hook

Fires at a session-lifecycle point; **fails open** (a raise is caught and the
hook skipped). Returns contributions — typically `Inject` to add text to the
system prompt. This is the real memory-brief hook:

```python
from harness.hooks import Inject

def session_brief(ctx):
    """SESSION_START: inject the memory brief. Fail-open, kill-switchable."""
    if os.environ.get("HARNESS_MEMORY_BRIEF", "1") == "0":
        return []
    root = _memory_dir()
    try:
        text = store.brief(root)
    except Exception:
        return []                 # fail-open, always
    return [Inject(text=text)]
```

Because lifecycle hooks are advisory, **catch your own exceptions and return an
empty list** on failure. A broken "inject project context" hook must never break
session start.

### Dispatch hook

Runs on every tool/model call; **fails closed** (a raise or timeout becomes a
`Block`). Takes the proposed action, returns one of `Allow`, `Block`,
`Rewrite`, or `Ask`:

```python
from harness.hooks import Allow, Block

def guard_writes(action):
    if action.tool == "write_file" and "secrets" in str(action.args):
        return Block(reason="writes under secrets/ are not allowed by this plugin")
    return Allow()
```

A dispatch hook is enforcement. If it can't decide safely, it must deny — which
is exactly what fail-closed gives you for free. This is also why a *broken*
dispatch hook must fail to load rather than register: a hook that raises at
runtime would turn fail-closed into a denial-of-service on every call.

### Subscriber

An **async** function that observes the event stream after the fact — for
auditing, metrics, or persistence. It must not raise; a crashing subscriber is
recorded as an error and the session continues.

```python
async def audit(envelope):
    if envelope.event.type == "tool_call_completed":
        ...   # non-blocking I/O only
```

Subscribers are fed from a bounded queue, so keep them fast and non-blocking. If
you need to react to something the model did, prefer a subscriber; if you need to
*change* what happens, you need a dispatch hook.

---

## Importing sibling modules

A subtlety: because the harness loads `hooks.py` under a synthetic module name,
a plain `import store` or `from . import store` will not find a sibling file.
Self-load siblings with `importlib`, as the memory plugin does:

```python
import importlib.util as _ilu
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location(
    "harness_plugin_myplugin_store", _Path(__file__).parent / "store.py"
)
store = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(store)
```

---

## MCP servers and emitters

A plugin can ship an MCP server (declared under `[mcp.servers.<name>]`, launched
with `${PLUGIN_ROOT}` substituted) — its tools appear as `mcp__<name>__<tool>`
and go through the same dispatcher and permissions as everything else. Plugin
MCP specs merge at the **lowest precedence**, so a user's `mcp.toml` can shadow
them.

`[emitters] namespaces` declares the `CustomEvent` namespaces your plugin
writes. `CustomEvent(namespace, name, data)` is the plugin-visible event channel
— emit them to record plugin-specific facts in the log without touching the
kernel's closed event union.

---

## The trust model

Loading a plugin **executes its code** — `hooks.py` runs at load time, MCP
servers are launched as processes. There is no sandbox. This is deliberate and
matches the permission baseline: installing a plugin is itself an act of
consent. Author plugins you'd run, and audit plugins before you install them.

---

## Checklist

- [ ] `plugin.toml` has `[plugin]` name/version/description; the name is regex-
      valid with no `__`.
- [ ] Every dispatch hook priority is `< 1000`.
- [ ] Lifecycle hooks catch their own exceptions and return `[]` on failure.
- [ ] Subscribers are `async` and non-blocking.
- [ ] Sibling modules are self-loaded via `importlib`.
- [ ] No literal secrets anywhere (MCP env values are variable names).
- [ ] `[emitters]` namespaces avoid `harness`/`mcp`/`annotation`/`plugin`.
- [ ] The plugin loads: drop it in a `--plugin-dir` and run `harness -p "hi"`;
      a manifest error surfaces immediately and loudly.
