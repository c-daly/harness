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

For the provisioned CUDA setup, the [local assistant](docs/local-assistant.md)
provides an offline launcher and a measured 8B profile with project context,
optional normal memory, checked file work, cancellation and task continuation.

## What it is

- **Event-sourced kernel.** The session log is the unit of truth; model state is
  a pure fold of it. Resume, replay, and telemetry fall out for free.
- **Multi-model by default.** Models are catalog aliases — over LiteLLM for API
  and local endpoints, or over a subscription CLI (Claude Code, Codex, or
  Antigravity/Gemini) with the harness's own tools served to it. Switch
  providers, run different models in subagents, do adversarial cross-model
  review. Not getting locked in is the point.
- **One enforcement path.** Native tools and MCP tools dispatch identically,
  through one dispatcher, behind one permission engine.
- **Resident continuity.** Explicit task requirements and configured project/memory
  queries survive session resume. `/status` joins task, context and local readiness;
  normal memory remains a plugin. See [the workflow and its limits](docs/resident-workflow.md).
- **Permission engine.** Allow / deny / ask rules over tool name *and* arguments,
  layered user → project, with a safe baseline (reads allowed, writes and shell
  prompt).
- **Native tools.** `read_file`, `write_file`, `edit_file`, `glob`, `grep`,
  `bash`, `todo` — workspace-confined, with teaching error messages.
- **A TUI built for long sessions.** Streamed thinking (`/thoughts`), markdown
  replies with transparent typeset LaTeX math (`/markdown`), in-place `/clear`
  and `/compact`, `/resume` to
  reopen a prior session, a persistent status bar (model · context % · cost ·
  tools), `@file` mentions with Tab completion — a mention reads through the
  same dispatcher and permission gate a model-issued read does, but only the
  literal text you typed is ever logged as your message — and a toggleable
  activity panel (`/panel` or F2) with Files / Agents / Workflows tabs.
- **Plugins.** Eight primitives (skills, commands, agents, dispatch/lifecycle
  hooks, subscribers, MCP servers, emitters) validated at load time.
- **Claude Code importer.** `harness import` converts a Claude Code plugin to a
  native one with a full conversion report — a converter, not a compat layer.

## Documentation

| Doc | For |
|---|---|
| **[docs/user-guide.md](docs/user-guide.md)** | Running and configuring: models, permissions, MCP, plugins, importing, telemetry |
| **[docs/model-management.md](docs/model-management.md)** | Inspect public Hugging Face GGUFs and register installed local models with on-demand startup |
| **[docs/architecture.md](docs/architecture.md)** | How it works: the event spine, kernel loop, dispatcher, hooks, the module map |
| **[docs/plugin-authoring.md](docs/plugin-authoring.md)** | Writing a plugin: the eight primitives, the manifest, worked examples |
| **[docs/contributing.md](docs/contributing.md)** | Modifying the harness: the invariants, extension recipes, testing discipline |
| **[docs/resident-workflow.md](docs/resident-workflow.md)** | Configured context, task continuation, status, interruption and offline evidence |
| **[docs/local-fallback.md](docs/local-fallback.md)** | Opt-in local fallback that preserves task context, authority and budgets |
| **[docs/external-handoff.md](docs/external-handoff.md)** | Operator reconciliation and bounded continuation of interrupted external work |
| **[docs/portable-continuation.md](docs/portable-continuation.md)** | Export a tracked task, context and evidence for another interface with `/export` or `harness export` |
| **[docs/coordination-outcomes.md](docs/coordination-outcomes.md)** | Inspect delegated outcomes, disagreement and provenance with `/coordination` or `harness coordination` |
| **[docs/usage-budgets.md](docs/usage-budgets.md)** | Shared usage stop limits, durable accounting, and `/budget` inspection |
| **[docs/execution-controls.md](docs/execution-controls.md)** | Core time/concurrency limits, `/execution`, and resume/delegation behavior |
| **[docs/local-scheduling.md](docs/local-scheduling.md)** | Local device groups, visible priority queues and owned runtime replacement |
| **[docs/supervised-improvement.md](docs/supervised-improvement.md)** | Core prompt proposals, fixed experiments, explicit adoption and exact rollback |
| **[docs/assessment-evaluation.md](docs/assessment-evaluation.md)** | Paired context/progress prompt comparison, frozen oracles and retained failed trials |
| **[docs/assessment-improvement.md](docs/assessment-improvement.md)** | Supervised context/progress prompt proposals, shadow adoption and rollback |

The complete working reference plugin is [`plugins/memory/`](plugins/memory/).
The authoritative design record (design doc + per-phase completion notes) lives
in the project vault at `vault/10-projects/harness/`.

## Status

The core is built: event spine and kernel, provider layer, permissions,
telemetry, MCP, the Textual TUI, the plugin loader, the native tool inventory,
and the Claude Code importer. The [core agency implementation record](docs/superpowers/plans/2026-09-06-core-agency-progress.md)
tracks current validation and remaining work. The bounded offline 8B project
workflow passes; broader model/task reliability and automatic semantic decisions
remain unqualified.

> Secondary docs drift from code. Where this README or anything under `docs/`
> disagrees with the source, the source is right — please fix the doc.
