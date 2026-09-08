# Harness — User Guide

Harness is an event-sourced, multi-model agent harness. You give it a task; it
runs a model in a loop, calls tools on your behalf (reading files, running
shell commands, talking to MCP servers), and asks your permission before doing
anything dangerous. Every step is recorded as an append-only event log, so any
session can be replayed, resumed, or audited after the fact.

This guide is for running and configuring the harness. If you want to write a
plugin, see [plugin-authoring.md](plugin-authoring.md). If you want to
understand or modify the internals, see [architecture.md](architecture.md) and
[contributing.md](contributing.md).

---

## Install

Requires Python 3.12+. The project uses [uv](https://docs.astral.sh/uv/).

```bash
cd harness
uv sync                 # install dependencies into a local venv
uv run harness --help   # run the CLI through uv
```

`uv run harness` is the entry point throughout this guide. If you install the
package (`uv pip install -e .`), the `harness` command is available directly.

---

## Running a session

### Interactive (TUI)

```bash
uv run harness
```

This opens a terminal UI: type a prompt, watch the model stream its reply and
tool calls, and answer permission prompts inline. Key bindings:

- **Enter** — submit your prompt.
- **Esc** — interrupt the turn in flight (the model stops, in-flight tool
  calls are cancelled cleanly, and you get the prompt back).
- **Up / Down** — walk your input history.
- **Tab** — with an `@token` under the cursor, complete it against workspace
  files (repeated Tab cycles through matches); otherwise Tab behaves as
  normal (moves focus).
- **F2** — toggle the activity panel (same as `/panel`; see below).
- `@path/to/file` — mention a file. Tab-complete it (see above), or just
  type it out; a bare relative path like `@alpha.py` works, no `./` needed.
  The mention itself is read through the same dispatcher path — and the
  same permission gate — a model-issued `read_file` call takes, so it shows
  up in the event log and an `ask`/`deny` rule on `read_file` applies to it
  too. The file's content is handed to the model as extra context for that
  turn only; what gets logged as *your* message, and what a later turn or
  `/resume` sees, is always the literal text you typed, `@token` included.
  A path that doesn't resolve to a real file is left as plain text, silently
  — no error, nothing sent. A file over 16 KiB is truncated (with a note)
  before it's attached; use offset/limit on `read_file` yourself (or ask
  the model to) for more.
- `/help` — list slash commands, including any your plugins add.
- `/models` — list configured models and agents; `/models hub OWNER/REPO`
  inspects public GGUF metadata and `/models add` registers installed weights
  with on-demand startup. See [model setup](model-management.md).
- `/task` — inspect the selected objective and its unresolved requirements.
  `/task new OBJECTIVE` selects a durable task; `/task require TEXT` adds a
  requirement for user review. Submit ordinary prompts to work on it. Use
  `/task check` for configured evidence checks, `/task confirm ID NOTE` to
  record your review, and `/task accept NOTE` to accept the checked attempt.
  Further work invalidates the earlier checks and acceptance. `/task list`,
  `/task use ID` and `/task off` switch task context without erasing records.
- `/status` — inspect task requirements, configured context results and local
  runtime snapshots together, without starting a probe or model. See
  [resident continuity](resident-workflow.md) for context sources and restart behavior.
  See [task evidence](task-evidence.md) for exact checks and headless inspection.
- `/thoughts [collapse|full|off]` — control how a reasoning model's thinking
  is shown while it streams. `collapse` (the default) streams the live
  thought, then replaces it with a `(thought for Ns · N chars)` summary once
  the answer starts -- the raw thought never lands in the transcript or
  session history. `full` keeps streaming the raw thought alongside the
  answer and retains it, dimmed, in the transcript above the reply. `off`
  shows only a `(thinking…)` suffix while thinking is in progress, with no
  thought text anywhere. Run `/thoughts` with no argument to see the current
  mode. The mode is session-local and not persisted across restarts. This is
  what makes a reasoning-heavy local model (e.g. a local Qwen3.6 quant) show
  visible progress instead of appearing hung during a long thinking phase.
- `/markdown [on|off]` — render each completed assistant reply as markdown
  (headings, lists, fenced code, tables) once the turn finishes. On by
  default; `/markdown off` reverts to plain text if a reply's formatting
  ever looks worse rendered than raw (e.g. heavy use of literal `#`/`*`/`_`
  outside of prose). Run `/markdown` with no argument to see the current
  mode. Session-local, not persisted across restarts. Streaming stays plain
  by design while a reply is still in progress — the live tail can't reflow
  as markdown mid-stream without flicker, so only the completed reply in the
  transcript renders formatted. With Markdown on, `$...$` / `\\(...\\)` inline
  math and `$$...$$` / `\\[...\\]` display math are typeset using Matplotlib's
  portable TeX-compatible MathText engine. Simple one-line expressions use
  crisp terminal glyphs; fractions, roots, sums, integrals, scripts, and other
  two-dimensional layouts use native transparent Sixel images when the
  terminal advertises support. Other terminals receive a high-density Unicode
  projection of the same transparent raster. All paths follow the active light
  or dark theme. Set `HARNESS_MATH_SIXEL=off` to disable native images. Dollar
  delimiters inside inline/fenced code, escaped dollars, and ordinary currency
  are left alone. Unsupported or malformed LaTeX falls back to its original
  source instead of breaking the reply.
- `/clear` — end the current session cleanly and start a fresh one: new
  session id, empty history, but the same provider instance, permission
  engine, and resolver wiring the app started with (kernel rebuild-in-place,
  not a process restart). Any MCP servers enabled at startup are restarted
  (fresh connections, same enabled set — the checklist is not re-prompted).
  Refused with a message while a turn is running.
- `/compact` — fold the whole transcript into one summary. Issues a single
  completion through the CURRENT model asking for a handoff-quality summary,
  then replaces `loop.history` with that summary as a system message and
  records a `CompactionApplied` event in the session log. It's event-sourced
  and resume-safe: reading the session back later (including via `--resume`)
  reconstructs the same collapsed state, because the fold applies the exact
  same replacement on replay. On failure (the summarize call errors) history
  is left untouched, nothing is logged, and the error is shown. Refused while
  a turn (or another `/compact`) is running. Esc cancels an in-flight
  `/compact` cleanly -- history untouched, nothing logged -- without writing
  a `UserInterrupt` event: unlike interrupting a real turn, a `/compact` is
  an internal admin call, not a user turn, so no interrupt fact belongs in
  the log for it.
- `/resume` — pick a prior session and reopen it in place (same kernel
  rebuild `/clear` uses, but reopening instead of starting fresh). Shows a
  picker listing every other session under `--base-dir`, newest first, each
  row an age (`3m ago`, `2h ago`, ...) and that session's first prompt; the
  current session is left out of the list, and a torn or otherwise unreadable
  log still shows up, marked `[unreadable: ...]`, rather than being hidden.
  Enter on the (default-highlighted, newest) row resumes it: same provider,
  permission engine, and resolver wiring as `/clear`, but `loop.history` and
  the transcript are rebuilt from that session's log instead of starting
  empty. Escape cancels and leaves the current session untouched. Refused
  while a turn is running. If there is nothing else to resume, says so and
  never opens the picker.
- `/panel` (or **F2**) — toggle the activity panel, a sidebar hidden by
  default with three tabs: **Files** (every path read/written/edited this
  session, newest-touched first, with `R`/`W`/`E` markers), **Agents**
  (every `dispatch_agent` call and every expert an `ensemble` /
  `consult_panel` / `escalate` call fanned out to, with a running/done/error
  status), and **Workflows** (the same coordination calls grouped by run,
  plus an agent-swarm section). Opening or closing the panel is purely
  local — it reads only what this session has already logged, writes
  nothing itself, and never interrupts a turn in flight. The agent-swarm
  section exists only when an MCP server named `agent-swarm` is enabled for
  the session (see [Session-start server checklist](#session-start-server-checklist));
  with it disabled the Workflows tab still shows the coordination-run
  section, just not that one. When present, it fetches the server's
  workflow state through the same dispatcher and permission gate a
  model-issued tool call takes — on first opening the Workflows tab and on
  pressing **r** while the panel has focus, never on a timer — and shows a
  fetch failure inline instead of crashing the panel.

The bottom line shows live token counts and stats for the session. Just above
the prompt, a persistent status bar tracks the running session at a glance:

- **model** — the current alias or model id (`loop.model`); updates the
  instant `/model` switches it, no turn required.
- **ctx N%** — estimated context-window fill: the tool schemas' token
  footprint plus the conversation history so far, divided by the model's
  `max_input_tokens` (a `local`-tagged model with no declared limit falls
  back to 16384). Shrinks after `/compact`. Shown only when the current
  model resolves against `--catalog`; omitted for a bare `--model` run or
  the echo provider, since there's no limit to measure against.
- **$N.NNNN** — running cost from the session's telemetry rollup. Shown
  alongside `ctx`, under the same catalog-alias condition.
- **tools N** — completed tool calls so far, from the same rollup the
  bottom stats line uses.

`/clear` and `/resume` reset it for the new/reopened session (a fresh
session starts at `tools 0`); it otherwise refreshes at each turn's end. A
resumed session's `tools`/`$` segments stay blank (its live telemetry index
never captures the session's own start), but `ctx N%` still tracks
`loop.history` as the conversation grows. `--catalog`'s file is re-read only
when it changes on disk; if it's briefly malformed, `ctx`/`$` just drop from
the bar for that refresh instead of the app erroring.

### Headless (one-shot)

```bash
uv run harness -p "summarize the README and list the open TODOs"
```

`-p/--prompt` runs a single turn non-interactively and prints the final reply.
In headless mode there is no one to answer permission prompts, so any tool that
would *ask* is **denied** by default. Grant what the run needs up front with
`--allow` (see [Permissions](#permissions)).

Headless mode is the same interaction channel as the TUI with a non-interactive
resolver swapped in — there is no separate code path, which keeps the two
honest about being interchangeable.

---

## Choosing a model

Harness is multi-model by design. Models are referenced by **alias**, resolved
through a catalog file so you never hard-code a provider string into a command.

Create `~/.config/harness/models.toml`. Each alias is a `[models.<alias>]`
table; only `route` is required:

```toml
[models.sonnet]
route = "anthropic/claude-sonnet-4-6"

[models.gpt]
route = "openai/gpt-4o"

[models.local]
route = "openai/llama3"               # an OpenAI-compatible local server
api_base = "http://localhost:11434/v1"
input_cost_per_token = 0.0            # this model isn't in LiteLLM's cost map,
output_cost_per_token = 0.0           #   so state its pricing here
max_input_tokens = 8192
tags = ["local", "cheap"]
verified = false                     # configuration alone is not conformance
```

Then:

```bash
uv run harness --model sonnet -p "..."
uv run harness --model gpt -p "..."
```

The `route` is a [LiteLLM](https://docs.litellm.ai/) model string. It selects
Anthropic, OpenAI-compatible endpoints, local servers, or another supported
adapter; actual compatibility still requires a working endpoint and suitable
model. Point at a different catalog with `--catalog PATH`.

### Running local models

For installed native llama.cpp and single-file GGUF weights, register a new
alias through the terminal:

```text
/models add my-local --file /path/to/model.gguf --runtime /path/to/llama-server --port 8082
/model my-local
```

Registration verifies the file and writes an on-demand startup profile; the
next turn starts its server. See [model setup](model-management.md) for library
paths, context, CPU/GPU settings and optional verification against cached Hub
metadata. The [local assistant](local-assistant.md) guide describes the measured
8B CUDA task profile. Weight size, model loading and useful task quality require
separate checks.

An alias containing only `route` and `api_base`, like the example above, requires
a separately running server. The route must identify its served model; changing
the alias does not load different weights. Owned startup profiles additionally
check server identity before inference.

`scripts/serve-local.sh` remains an optional externally managed Docker launcher.
It can download weights on first start. Its 35B default is not the measured M3
profile; earlier configurations were too slow for resident decisions. Larger
models and quantization comparisons remain on the
[candidate list](local-model-candidates.md).

### Catalog fields

| Field | Meaning |
|---|---|
| `route` (required) | The LiteLLM model string the alias maps to. |
| `api_base` | Custom endpoint base URL — for OpenAI-compatible or local servers. |
| `api_key_env` | Name of the env var holding the API key (a *name*, never the key itself). |
| `backend` | Selects a non-LiteLLM provider implementation: `"claude-code"` runs turns through the local, logged-in Claude Code CLI on subscription auth (see [Claude on your Claude Code subscription](#claude-on-your-claude-code-subscription)); `"codex"` runs turns through the local, logged-in Codex CLI on subscription auth (see [Codex on your ChatGPT subscription](#codex-on-your-chatgpt-subscription)); `"antigravity"` runs turns through the local, logged-in Antigravity (`agy`) CLI on Google-account subscription auth (see [Antigravity (Gemini) backend](#antigravity-gemini-backend)). Either way `route` becomes `<backend>/<model>` instead of a LiteLLM string. Absent → the LiteLLM route above. |
| `tags` | Free-form capability labels you can use to organize aliases. |
| `input_cost_per_token` / `output_cost_per_token` | Pricing overrides. |
| `max_input_tokens` | Context-window override. |
| `verified` | Whether this model has passed a conformance run (default `false`). |
| `local` | Explicit local readiness and optional on-demand startup profile; see [runtime controls](local-runtime-readiness.md). |
| `artifact` | Registration-time file digest, size and optional pinned Hub provenance; recorded by `/models add`. |

**Pricing and context windows are not your job to maintain.** When you give only
`route`, the harness looks the model up in **LiteLLM's maintained cost map** and
pulls per-token cost and context size from there automatically. You only restate
those fields for models LiteLLM doesn't know — typically local ones. Resolved
pricing flows into the event log, which is what makes cost a telemetry query
(`harness stats`).

**API keys come from the environment, not the catalog.** LiteLLM reads provider
credentials from its conventional variables (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, …). The catalog records routes, endpoints, and metadata — never
secrets — consistent with the harness's never-store-literals rule.

**`verified` is an honesty flag, not a gate.** It defaults to `false` because
"LiteLLM routes there" is not "the harness works there" — a model stays
unverified until a conformance suite has passed against recorded real streams.
You can run an unverified model; the flag just records what's been checked.

If you don't pass `--model`, the harness runs a built-in **echo provider** (a
deterministic stand-in for demos and tests), not a real model — so a real
session always needs `--model <alias>`. An unknown alias or a missing catalog
file fails with a message telling you how to fix it.

> Switching providers is a first-class operation, not a workaround. The same
> applies inside plugins and subagents — model choice is data, not code.

---

## Permissions

Every tool call passes through a permission engine before it runs. The engine
matches **allow / deny / ask** rules against the tool name and its arguments,
with a strict precedence: a `deny` anywhere wins absolutely, otherwise the first
matching rule in layer order decides, otherwise the layer default applies.

### The baseline

When native tools are enabled, the harness ships a baseline policy so a fresh
checkout is safe without any config:

| Tool | Default |
|---|---|
| `read_file`, `glob`, `grep` | allow |
| `write_file`, `edit_file`, `bash` | **ask** |
| `dispatch_agent`, `todo`, `invoke_skill`, `mcp__*`, model calls | allow |
| anything else | ask |

The reasoning: reading and searching are safe; **writing and running shell are
the dangerous core and always prompt**; configuring an MCP server or installing
a plugin is itself the act of consent (the trust model is "loading a plugin
means trusting its code"), so those are allowed. Your own rules always shadow
the baseline.

### Granting at session scope

```bash
uv run harness --allow 'bash(git *)' --allow 'write_file' -p "..."
```

`--allow` takes a tool glob, optionally with an argument pattern, and grants it
for this session only. Repeat the flag to grant several. This is the usual way
to make a headless run non-interactive.

### Persistent rules

Rules live in TOML and layer in this order (innermost wins on a tie; deny always
wins):

1. `~/.config/harness/grants.toml` — session "always allow" grants the TUI saves
2. `<project>/.harness/permissions.toml` — project rules
3. `~/.config/harness/permissions.toml` — your personal rules

A rule file looks like:

```toml
default = "ask"

[[rules]]
action = "allow"
tool = "bash"
match = { command = "git *" }

[[rules]]
action = "deny"
tool = "write_file"
match = { file_path = "/etc/*" }
```

`match` patterns are globs over the stringified argument value. In the TUI,
answering **"always"** to a prompt writes a scoped grant for you (bash grants
stay session-only unless you set `HARNESS_PERSIST_GRANTS=1`, so one keystroke
never writes a permanent allow-all-shell rule).

---

## Native tools

The CLI registers a built-in tool inventory for every run. `--workspace DIR`
sets the root that file tools are confined to; it defaults to the current
directory.

| Tool | What it does |
|---|---|
| `read_file` | Read a file, `cat -n` style, with `offset`/`limit` windowing |
| `write_file` | Create or overwrite a file (overwrite requires a prior read) |
| `edit_file` | Replace an exact string in a file (must be unique unless `replace_all`) |
| `glob` | Find files by glob pattern, newest first |
| `grep` | Search file contents with a Python regex |
| `bash` | Run a shell command with a timeout and output cap |
| `todo` | Maintain a task list (reconstructed from the event log) |

All file tools are **confined to the workspace root**: a path that resolves
outside it is rejected, and symlinks are never followed out of the workspace.
`bash` is not path-confined — its guardrails are the permission engine plus a
prompt on compound commands.

---

## MCP servers

Harness speaks the [Model Context Protocol](https://modelcontextprotocol.io/).
MCP tools are dispatched and permission-checked exactly like native tools, under
the name `mcp__<server>__<tool>`.

Configure servers in `mcp.toml`, layered user → project:

- `~/.config/harness/mcp.toml`
- `<project>/.harness/mcp.toml` (shadows the user file per server name)

Manage them from the CLI:

```bash
uv run harness mcp add memory --command python3 --arg /path/to/server.py
uv run harness mcp list
uv run harness mcp remove memory
uv run harness mcp import .mcp.json     # import a Claude Code .mcp.json
```

**Secrets are never stored as literals.** Environment values in `mcp.toml` are
env-var *names*, dereferenced at launch — so a config file is safe to commit.
The importer refuses any `.mcp.json` entry that embeds a literal secret and
tells you which variable to set instead.

**Narrowing tool exposure per server.** A server can register far more tools
than you want in scope. `tools_allow` is a list of fnmatch globs on the
server's own tool names (before the `mcp__<server>__` prefix is added);
tools that match none of the globs are never registered — they do not exist
in the tool registry at all, so permission rules and the model can never see
or call them. An empty (or omitted) `tools_allow` exposes every tool the
server advertises. `default_enabled` controls whether the server starts
pre-checked in the session-start checklist; set it to `false` for a server
you want present but dormant until explicitly opted into for a given session.

```toml
[servers.agent-swarm]
command = "/home/fearsidhe/.claude/plugins/cache/fearsidhe-plugins/agent-swarm/1.1.0/bin/mcp-router"
tools_allow = ["workflow__*", "experiment__*", "router__*"]  # its unique families only
default_enabled = false  # present in the checklist, dormant unless opted in
```

### Session-start server checklist

Before any MCP server is dispatchable, the TUI shows a checkbox per configured
server, pre-checked from `default_enabled`. Enter accepts the selection (Space
toggles a box). A server you leave unchecked never starts for that session --
its transport is never launched, it holds no connection, and it registers no
tools, so it cannot be reached even by a permission rule that would otherwise
match it. Headless (`-p`) runs skip the checklist -- there is no one to ask --
and start whatever `default_enabled` says for every configured server.

Once the checklist's selections are live and every started server has
registered its tools, the TUI checks the final tool registry against the
active model: for a constrained model (tagged `local`, or with
`max_input_tokens` under 32768) whose tool schemas alone would eat more
than 10% of its context window, it shows a warning toast naming the tool
count and the estimated token cost -- `/tools` lists what's registered so
you can trim the checklist next time. The same check runs again after any
`/model` switch. Unconstrained models never trigger it.

Skip MCP entirely for a run with `--no-mcp`, or point at one explicit file with
`--mcp-config PATH`.

---

## Plugins

A plugin is a directory that bundles skills, slash commands, agent definitions,
hooks, MCP servers, and event emitters. Harness discovers plugins from:

- `~/.config/harness/plugins/`
- `<cwd>/.harness/plugins/`
- any directory you add with `--plugin-dir DIR` (repeatable)

Disable discovery with `--no-plugins`. To write your own, see
[plugin-authoring.md](plugin-authoring.md). The `plugins/memory/` directory in
this repo is a complete, working reference plugin.

### Importing a Claude Code plugin

If you already have a Claude Code–format plugin, convert it:

```bash
uv run harness import /path/to/cc-plugin-root
```

This emits a native plugin tree plus an `IMPORT-REPORT.md` that lists **every**
rewrite, degradation, drop, flagged hook, MCP refusal, and skipped file. The
importer is a converter, not a compatibility shim:

- Skills, commands, agents, and `.mcp.json` servers are converted directly.
- Tool names in prose are rewritten to native names (`Read` → `read_file`,
  `TodoWrite` → `todo`, …), and each rewrite is reported.
- A skill referencing a capability with no native equivalent (e.g. `WebFetch`)
  is flagged **degraded** rather than silently "succeeding."
- **Hooks are never converted** — a Claude Code hook is a shell command
  speaking Claude Code's protocol, so each one is flagged for hand-porting with
  guidance instead of being shimmed.
- Secrets are never echoed; output is byte-identical across runs.

Imported plugins are **regenerable artifacts**: don't hand-edit them, just
re-import when the upstream updates. To take ownership of one:

```bash
uv run harness import <output-dir> --eject   # convert to owned source
```

After `--eject`, re-import is refused (so you can't clobber your edits).
`--force` overrides a refusal; `--out DIR` chooses the output location.

---

## Telemetry

Every model call, tool call, permission decision, retry, and error is an event
in the session log, which makes reliability a query rather than a guess:

```bash
uv run harness stats               # token/cost/latency rollups
uv run harness compare RUN_A RUN_B # compare two runs
uv run harness outcome SESSION_ID ok --score 0.9 --note "shipped"
```

---

## Where things live

| Path | Contents |
|---|---|
| `~/.local/share/harness/sessions/<id>/` | Per-session event log + content blobs |
| `~/.config/harness/models.toml` | Model catalog (aliases → routes) |
| `~/.config/harness/mcp.toml` | User MCP servers |
| `~/.config/harness/permissions.toml` | User permission rules |
| `~/.config/harness/grants.toml` | "Always allow" grants from the TUI |
| `~/.config/harness/plugins/` | User plugins |
| `<project>/.harness/` | Project-scoped `mcp.toml`, `permissions.toml`, `plugins/` |

Override the session/data root with `--base-dir`. Resume a past session with
`--resume SESSION_ID`, or reopen the most recently active one under
`--base-dir` without looking up its id via `--continue` (errors clearly if
there are no sessions to continue; mutually exclusive with `--resume`). Tag a
run for later querying with `--tag NAME` (repeatable).

---

## Claude on your Claude Code subscription

Entries with `backend = "claude-code"` run turns through your locally
installed, logged-in Claude Code CLI (headless `claude -p`) instead of an
API. The harness serves its own tools to Claude over MCP and disables
Claude Code's built-ins, so permissions and the event log behave exactly as
with API models. The harness never handles claude.ai credentials — log in
with `claude` once and the backend uses that.

```toml
[models.claude]
backend = "claude-code"
route = "claude-code/default"   # "claude-code/<model>" passes --model <model>
input_cost_per_token = 0.0      # subscription: flat-rate, no per-token cost
output_cost_per_token = 0.0
tags = ["anthropic", "subscription", "tool-calling", "frontier"]
```

Requirements: `claude` on PATH and logged in (Pro/Max). One harness turn is
one Claude Code agent turn; Max-plan rate limits apply.

Any reasoning Claude surfaces (`thinking` content blocks) is streamed to the
UI as thought chunks and never enters the transcript.

---

## Codex on your ChatGPT subscription

Entries with `backend = "codex"` run turns through your locally installed,
logged-in Codex CLI (`codex exec --json`) instead of an API. The harness
serves its own tools to Codex over MCP, injected at spawn time as a dotted
`-c mcp_servers.harness.url=...` override, and reads the turn back off the
JSONL event stream. The `codex mcp-server` transport is not usable
headlessly: it gates every MCP tool call behind a custom `codex/event`
elicitation that no automated client can answer, while `codex exec` runs
the same turn, with the same tools, and asks nothing (verified against
codex-cli 0.147.0). Each turn also gets `-s read-only` and a fresh, empty
scratch directory as its `cwd`. The harness permission engine gates every
call to a harness tool; codex's own sandbox is a separate mechanism that
blocks its built-in shell from writing to disk or reaching the network (see
"Trust model" below for what it does *not* block).

Every turn also runs under its own scratch `CODEX_HOME`, seeded fresh and
torn down when the turn ends, so a turn never picks up your real Codex
profile or its configured MCP servers. Its `config.toml` sets
`approvals_reviewer = "auto_review"` — without that key `codex exec`
auto-declines every MCP tool call headlessly; with it, codex-side approval
prompts never fire, and the harness permission engine is the only approval
surface a harness tool call goes through. The harness never reads, stores,
or transmits your ChatGPT credential contents: each turn mechanically
copies `auth.json` from `$CODEX_HOME` (or `~/.codex` if unset) into that
per-turn scratch `CODEX_HOME` so the codex CLI can authenticate itself, and
the copy is removed with the rest of the scratch home when the turn ends.
Log in once with `codex login` and the backend uses that.

```toml
[models.codex]
backend = "codex"
route = "codex/default"         # "codex/<model>" passes a --model override
input_cost_per_token = 0.0      # subscription: flat-rate, no per-token cost
output_cost_per_token = 0.0
tags = ["openai", "subscription", "tool-calling"]
```

**Additive, not exclusive.** Unlike the claude-code backend, which disables
Claude Code's own built-in tools so the harness registry is the only tool
surface, the codex backend does *not* disable Codex's built-in shell. A
codex-backed turn gets the harness's tools in addition to whatever Codex can
already do on its own — tool parity here is additive, not a drop-in match
for the claude-code backend's exclusivity.

**Trust model.** Writes go through harness tools and the harness permission
engine — a deny-rule there binds. Reads by codex's own built-in shell do
not: live verification (codex-cli 0.147.0, 2026-08-16) showed that under
`-s read-only` codex's shell can `cat` an absolute path anywhere on disk and
get the content back. The sandbox blocks writes and network, not reads, and
codex has no read-root confinement setting to turn that off. The empty,
per-turn scratch `cwd` is steering plus defense-in-depth, not a hard
boundary: relative paths resolve to nothing there, and a prompt prefix
tells the model its cwd is empty and to check through harness tools before
concluding a file is missing — but a codex-backed turn that chooses to read
an absolute path outside that scratch dir will succeed, unaudited by the
harness. Stated plainly: a harness deny-rule on a read path does not bind
codex's own shell.

Requirements: `codex` on PATH and logged in. One harness turn is one Codex
agent turn.

Any reasoning Codex surfaces (`reasoning` items) is streamed to the UI as
thought chunks and never enters the transcript.

---

## Antigravity (Gemini) backend

Entries with `backend = "antigravity"` run turns through your locally
installed, logged-in Antigravity CLI (`agy`, Google's successor to
gemini-cli) instead of an API. Auth is your Google account's OAuth
subscription grant, not a per-token API key — log in once through `agy`'s
own login flow and the backend uses that. The harness serves its own tools
to `agy` over MCP: a per-turn `McpToolServer` is registered with a separate
`agy mcp add -t http harness <url>/` subprocess ahead of the turn (`agy` has
no dotted-config-override flag the way codex does; the CLI is the only
stable registration surface), and `--dangerously-skip-permissions` stands in
for the config-seeding codex needs for headless MCP tool approval.

`agy` has no config-dir override env var, so every turn runs under a whole
scratch `HOME` (a fresh, `0700` tempdir), seeded fresh and torn down when the
turn ends, so a turn never picks up your real Antigravity profile or its
configured MCP servers. The harness never reads, stores, or transmits your
Google account credential contents: each turn mechanically copies the auth
files `agy` needs (`oauth_creds.json`, `google_accounts.json`,
`installation_id`, `settings.json`, `projects.json`, `state.json` under
`~/.gemini`, and `antigravity-oauth-token`, `installation_id`,
`settings.json` under `~/.gemini/antigravity-cli`) into that per-turn
scratch `HOME` so the `agy` CLI can authenticate itself, and the copies are
removed with the rest of the scratch home when the turn ends. Log in once
through `agy`'s own Google-account login flow and the backend uses that.

```toml
[models.gemini]
backend = "antigravity"
route = "antigravity/gemini-3.7-flash-high"  # "antigravity/<model>" passes a --model override
input_cost_per_token = 0.0      # subscription: flat-rate, no per-token cost
output_cost_per_token = 0.0
tags = ["google", "subscription", "tool-calling"]
```

**Additive, not exclusive.** Like the codex backend and unlike the
claude-code backend, the antigravity backend does *not* disable `agy`'s own
built-in tools. An antigravity-backed turn gets the harness's tools in
addition to whatever `agy` can already do on its own — tool parity here is
additive, not a drop-in match for the claude-code backend's exclusivity.

**Trust model (read this before relying on it).** `agy`'s ~57 built-in tools
(`run_command`, `write_to_file`, `browser_*`, `view_file`, …) are not
disabled — the harness permission engine gates every harness-tool call, and
a deny-rule there binds, but it does not reach `agy`'s own built-ins. The
per-turn scratch `cwd` and scratch `HOME` steer the model; they do not
confine it. This is a step *less* contained than the codex backend, not the
same: codex's own sandbox at least blocks its built-in shell from writing to
disk or reaching the network, even though it cannot stop reads outside the
scratch dir. Antigravity ships with no equivalent restriction in v1 — unlike
codex's read-only sandbox, `agy`'s built-ins can potentially both **read
and write** outside the scratch directories, unaudited by the harness. `agy`
exposes `--sandbox` and `--mode plan` flags, but their interaction with MCP
tool calls is unprobed; investigating and adopting them is a named
fast-follow, not a v1 guarantee. The orientation prompt prefix (the same
pattern used for codex) directs the model to do its file, directory, and
system work through harness tools, but that is steering, not enforcement.

Requirements: `agy` on PATH and logged in. One harness turn is one `agy`
headless print-mode turn.

`agy` exposes thinking-token *counts* but never thought text, so the
antigravity backend never emits thinking output in v1 — there is nothing to
stream to the UI as thought chunks, and (as with every other backend)
nothing enters the transcript either way.
