# harness

A personal, event-sourced, **multi-model** agent harness. You give it a task; it
runs a model in a loop, calls tools on your behalf, asks permission before
anything dangerous, and records every step as an append-only event log that can
be replayed, resumed, and audited.

Working name. Python 3.12+, [uv](https://docs.astral.sh/uv/)-managed.

```bash
uv sync
uv run harness                              # interactive TUI
uv run harness -p "summarize the README"    # one-shot, headless
```

## What it is

- **Event-sourced kernel.** The session log is the unit of truth; model state is
  a pure fold of it. Resume, replay, and telemetry fall out for free.
- **Multi-model by default.** Models are catalog aliases over LiteLLM — switch
  providers, run different models in subagents, do adversarial cross-model
  review. Not getting locked in is the point.
- **One enforcement path.** Native tools and MCP tools dispatch identically,
  through one dispatcher, behind one permission engine.
- **Permission engine.** Allow / deny / ask rules over tool name *and* arguments,
  layered user → project, with a safe baseline (reads allowed, writes and shell
  prompt).
- **Native tools.** `read_file`, `write_file`, `edit_file`, `glob`, `grep`,
  `bash`, `todo` — workspace-confined, with teaching error messages.
- **Plugins.** Eight primitives (skills, commands, agents, dispatch/lifecycle
  hooks, subscribers, MCP servers, emitters) validated at load time.
- **Claude Code importer.** `harness import` converts a Claude Code plugin to a
  native one with a full conversion report — a converter, not a compat layer.

## Documentation

| Doc | For |
|---|---|
| **[docs/user-guide.md](docs/user-guide.md)** | Running and configuring: models, permissions, MCP, plugins, importing, telemetry |
| **[docs/architecture.md](docs/architecture.md)** | How it works: the event spine, kernel loop, dispatcher, hooks, the module map |
| **[docs/plugin-authoring.md](docs/plugin-authoring.md)** | Writing a plugin: the eight primitives, the manifest, worked examples |
| **[docs/contributing.md](docs/contributing.md)** | Modifying the harness: the invariants, extension recipes, testing discipline |

The complete working reference plugin is [`plugins/memory/`](plugins/memory/).
The authoritative design record (design doc + per-phase completion notes) lives
in the project vault at `vault/10-projects/harness/`.

## Status

The core is built: event spine and kernel, provider layer, permissions,
telemetry, MCP, the Textual TUI, the plugin loader, the native tool inventory,
and the Claude Code importer. The suite runs ~650 tests.

> Secondary docs drift from code. Where this README or anything under `docs/`
> disagrees with the source, the source is right — please fix the doc.
