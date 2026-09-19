# Resident startup and existing MCP connection repair

Date: 2026-09-18 (America/New_York)

This receipt records the initial implementation and local runtime checks. The
resident startup and project-overview changes were subsequently prepared on
`feat/resident-startup-project-overview`, based on current `main`. Prior
workspace/session edits remain in the original checkout and are outside this
repair. The machine configuration changes below are not installed by merging
the source changes.

## Reproduced problems

- CLI startup without an explicit model or routing default constructed EchoProvider
  in the TUI and FakeProvider echo turns headlessly. The continuity plan had changed
  documentation, not that behaviour.
- User MCP configuration pointed agent-swarm at a nonexistent cached
  `agent-swarm/1.1.0/bin/mcp-router` executable. Only cache version `1.0.0` existed.
- The stable installed agent-swarm bridge then failed to start its daemon because
  the plugin environment lacked all three declared runtime dependencies: PyYAML,
  requests and duckdb. Its bridge suppressed the startup diagnostic.
- Memory's configured server connected and answered a bounded search when tested
  with host subprocess access. Its path is inside the Claude plugin installation,
  but this connection launches Python directly; it does not invoke Claude.

## Changes

- Core `build_kernel` defaults to Saoirse's resident instructions. This is distinct
  from model selection and remains available without plugins.
- CLI reads optional `~/.config/harness/resident.toml` (or `--resident-config`).
  An explicit model or saved session selection wins, then a routing default, then
  the resident's configured default. Fresh sessions may also load a configured
  context profile; resumed context and explicit context overrides remain intact.
- Without a configured model, the TUI exposes model selection and an explicit
  unavailable message. Headless inference fails with an actionable provider error.
  Echo is now an explicit `--demo` facility only.
- TUI identifies Saoirse and the selected model at startup.
- Updated CLI tests to opt into demo behaviour and isolate user configuration.

Machine configuration changes, outside the repository:

- Created `~/.config/harness/resident.toml` with `model = "local"`, selecting the
  existing qwen3-8b endpoint/profile. No model catalog or existing context limit was
  changed. The user was asked for an optional preference; local was the stated
  default assumption while awaiting any contrary preference.
- Replaced the stale cached executable with the stable installed
  `~/.claude/plugins/agent-swarm/bin/mcp-router` path. Existing tool filters and
  `default_enabled = false` were preserved.
- Original MCP configuration is at
  `~/.config/harness/mcp.toml.before-resident-startup-20260919T010950Z.bak`.
- Installed the daemon's declared runtime requirements into its own virtualenv:
  PyYAML 6.0.3, requests 2.34.2, duckdb 1.5.5, plus their dependencies. The legacy
  daemon is now available. No workflow was started or task delegated.

## Live evidence

All inference used the actual Harness entrypoint and configured local provider.
No provider override supplied the model choice or answer.

| Probe | Result |
|---|---|
| `harness --base-dir /tmp/harness-resident-startup-live --no-mcp --no-plugins -p ...` | Selected `local`, unpinned; actual response identifies Saoirse. Model call completed: 1,773 input and 21 output tokens. |
| `harness --base-dir /tmp/harness-resident-startup-full -p ...` | Default MCP/plugin configuration: projects (2 tools), Serena (30), memory (2 permitted tools) all started. Selected `local`; actual response identifies Saoirse. Model call completed: 10,717 input and 22 output tokens. |
| Repaired agent-swarm MCP connection | Handshake discovered 91 server tools before Harness allowlist filtering; `router__ping` succeeded. |
| Agent-swarm workflow reader | `workflow__workflow_get_state` for `experiment` returned null with `isError=false`; no active experiment was claimed. |
| Configured memory reader | Bounded `memory_search` completed with `isError=false`, returning 493 text bytes. Private record contents are not copied here. |

Local inference reused the existing external runtime after inventory confirmed
`qwen3-8b`; no additional GPU model was loaded and no existing runtime was stopped.
The normal tool set's 10,717 input tokens on this short probe are a material context
cost for the local profile. This repair does not qualify its long-session quality.

Evidence sessions:

- `/tmp/harness-resident-startup-live/sessions/0e3ac070afb74122b425c96892dc32dc.jsonl`
- `/tmp/harness-resident-startup-full/sessions/8925e679d2774550bc6324e0141a8510.jsonl`

## Initial automated validation

- Resident startup, CLI and model-selection regressions: **71 passed**.
- Broader interface/context/MCP/budget/lifecycle regressions: **204 passed**.
- Resident startup including the additional rendered no-model/model-switch check:
  **14 passed** (overlaps the prior startup checks).
- Compact project overview controls: **7 passed**.
- Ruff on changed Python files and `git diff --check`: passed at initial check.

Initial sandbox-only runs could not complete local MCP handshakes and were
interrupted. The affected tests and live MCP probes were rerun with local host
access. One broader test command named a nonexistent test module and collected
no tests; the corrected test list was then run.

## Scope still open

This is a native startup repair and restoration of an optional legacy MCP bridge.
The working memory connection exposes reads; automatic observation capture and
writer integration remain plan work. The memory recorder's separate Claude-backed
default has not been migrated. Agent-swarm still uses its existing daemon/router;
this is not a native plugin port or a completed workflow qualification. Experiment
still requires the independent integration described in the approved design.
Cross-session identity configuration, richer continuity, shared frontend attachment
and the five-session outing remain governed by the plan's acceptance criteria.

## Follow-up: invented project name on a real user turn

The user's session `25e16f003e804a0b9306c191a086c82f` recorded the actual prompt
`Tell me where the projects stand`. The model called
`projects_status(project="serena")` without prior discovery, received found=false,
and asked the user about the project name it had invented. The request used
12,952 input tokens. No configured context profile supplied the project inventory.

The first attempted correction supplied inventory and a smaller tool set. It
queried real paths but opened 16 separate project reports and failed with
ContextOverflow. This failed run is retained at
`/tmp/harness-resident-project-status/sessions/1a83419d81084172a8b64d0705affbed.jsonl`.
Discovery alone was insufficient.

Added `plugins/projects_overview.py`, a compact read-only adapter registered in
the existing installed projects server. The adapter uses that server's inventory,
root resolution, Git reader and JSON encoder; it adds no store or task authority.
It returns bounded pages of exact paths, latest commit, explicit working-tree
state, dirty-entry counts and status-document paths. Renames count as one entry;
failed Git reads stay errors, rather than becoming clean status. Full document
excerpts remain available through projects_status.

The installed server's additive registration patch is saved beside this receipt
as `projects-overview.patch`. Its backup is
`~/.config/harness/plugins/projects/server.py.before-resident-overview-20260918.bak`.
The helper is source-loaded from this Harness checkout, so moving the checkout
requires updating that registration. This is a local integration, not a published
plugin package.

The tested profile is `docs/examples/local-resident/saoirse.toml`, installed as
`~/.config/harness/profiles/saoirse.toml` and selected by resident.toml. It exposes
16 tools: core work/delegation, memory reads and project discovery/status/overview.
Other configured MCP tools are excluded from this profile, including in children;
their servers can still connect. This is explicit context selection, not evidence
that every loaded plugin is available to the local resident. The profile retains
16 history turns with a 65,536-byte request cap and a 1,536-token response limit.
Existing sessions keep their saved policy unless explicitly overridden.

Two intermediate overview replies completed but added unsupported "Active" labels.
The adapter now supplies working_tree labels derived from Git, and the profile
uses those as the checkout-status column. The final exact-prompt replay at
`/tmp/harness-resident-project-overview-qualified` completed using one overview
call and 5,597 then 6,639 input tokens. Its checkout labels and reported values
matched the returned records. It covered a subset; this is not proof of complete
portfolio coverage, milestone assessment, or general local-model reliability.

A fresh request with only installed defaults, `Give me an overview of my
projects.`, also completed. Session
`/tmp/harness-resident-overview-default/sessions/491b8a06d690452c9d4fd63241bdb8ce.jsonl`
records all three context sources ready, one projects_overview call, and two model
calls with 5,599 and 6,641 input tokens. No --model, --context-profile or --allow
override was used. The answer reports observed checkout labels and notes that
more projects are available. This is a second wording check, not a broad eval.

The original source windows and memory records were not migrated or modified.
The resident config before selecting the profile is backed up at
`~/.config/harness/resident.toml.before-project-overview-20260918.bak`.

For a fresh session, restart from this checkout with `uv run harness`. To preserve
an existing conversation while explicitly adopting the new profile, exit the old
client and use `uv run harness --resume SESSION_ID --context-profile
~/.config/harness/profiles/saoirse.toml`. A plain resume deliberately keeps its
saved context policy; changing user defaults does not rewrite saved sessions.
