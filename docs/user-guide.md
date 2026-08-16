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
- `@path/to/file` — mention a file; the path is expanded into your message.
- `/help` — list slash commands, including any your plugins add.
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
- `/clear` — end the current session cleanly and start a fresh one: new
  session id, empty history, but the same provider instance, permission
  engine, and resolver wiring the app started with (kernel rebuild-in-place,
  not a process restart). Refused with a message while a turn is running.
- `/compact` — fold the whole transcript into one summary. Issues a single
  completion through the CURRENT model asking for a handoff-quality summary,
  then replaces `loop.history` with that summary as a system message and
  records a `CompactionApplied` event in the session log. It's event-sourced
  and resume-safe: reading the session back later (including via `--resume`)
  reconstructs the same collapsed state, because the fold applies the exact
  same replacement on replay. On failure (the summarize call errors) history
  is left untouched, nothing is logged, and the error is shown. Refused while
  a turn is running.

The bottom line shows live token counts and stats for the session.

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
verified = true
```

Then:

```bash
uv run harness --model sonnet -p "..."
uv run harness --model gpt -p "..."
```

The `route` is a [LiteLLM](https://docs.litellm.ai/) model string, so any
provider LiteLLM supports works: Anthropic, OpenAI-compatible endpoints, local
servers, and so on. Point at a different catalog with `--catalog PATH`.

### Running local models

To run a local model on your hardware, use `scripts/serve-local.sh` to launch a containerized llama.cpp server with GPU acceleration. The script defaults to **Qwen3.6-35B-A3B** (Mixture of Experts), a 35B-parameter model where only ~3.5B params activate per token — fast and VRAM-efficient on a 12 GB GPU:

```bash
bash scripts/serve-local.sh   # launches on http://localhost:8080
```

Then configure your `~/.config/harness/models.toml` to route through it (same `[models.local]` section above, but with `route = "openai/qwen"` and `api_base = "http://localhost:8080/v1"` to match llama.cpp's OpenAI-compatible endpoint).

**Quantization options** for Qwen3.6-35B-A3B (all from `unsloth/Qwen3.6-35B-A3B-GGUF`):

| Quantization | Size | Notes |
|---|---|---|
| `UD-IQ4_XS` | 17.7 GB | Default; balanced quality and speed. |
| `UD-Q4_K_M` | 22.1 GB | Higher quality, slower; use if you have VRAM headroom. |
| `UD-Q3_K_XL` | 16.8 GB | Tighter fit for 12 GB cards; quality trade-off for speed. |

Set a different model with `HARNESS_LOCAL_MODEL` (e.g., `HARNESS_LOCAL_MODEL=unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M bash scripts/serve-local.sh`). The old Qwen3-Coder-30B-A3B is still available the same way: `HARNESS_LOCAL_MODEL=unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:IQ4_XS`.

**Key point:** The `[models.local]` catalog entry defines the *route* your harness uses to call the server; the actual model running inside is whatever `scripts/serve-local.sh` launched (controlled by `HARNESS_LOCAL_MODEL`). The route name is advisory to llama.cpp — the server responds correctly to any OpenAI-compatible request, regardless of whether you named it `llama3` or `qwen`.

### Catalog fields

| Field | Meaning |
|---|---|
| `route` (required) | The LiteLLM model string the alias maps to. |
| `api_base` | Custom endpoint base URL — for OpenAI-compatible or local servers. |
| `api_key_env` | Name of the env var holding the API key (a *name*, never the key itself). |
| `backend` | Selects a non-LiteLLM provider implementation: `"claude-code"` runs turns through the local, logged-in Claude Code CLI on subscription auth (see [Claude on your Claude Code subscription](#claude-on-your-claude-code-subscription)); `"codex"` runs turns through the local, logged-in Codex CLI on subscription auth (see [Codex on your ChatGPT subscription](#codex-on-your-chatgpt-subscription)). Either way `route` becomes `<backend>/<model>` instead of a LiteLLM string. Absent → the LiteLLM route above. |
| `tags` | Free-form capability labels you can use to organize aliases. |
| `input_cost_per_token` / `output_cost_per_token` | Pricing overrides. |
| `max_input_tokens` | Context-window override. |
| `verified` | Whether this model has passed a conformance run (default `false`). |

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
`--resume SESSION_ID`. Tag a run for later querying with `--tag NAME`
(repeatable).

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