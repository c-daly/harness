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

Create `~/.config/harness/models.toml`:

```toml
[models.sonnet]
route = "anthropic/claude-sonnet-4-6"

[models.gpt]
route = "openai/gpt-4o"

[models.local]
route = "ollama/llama3"
```

Then:

```bash
uv run harness --model sonnet -p "..."
uv run harness --model gpt -p "..."
```

The `route` is a [LiteLLM](https://docs.litellm.ai/) model string, so any
provider LiteLLM supports works: Anthropic, OpenAI-compatible endpoints, local
Ollama, and so on. Point at a different catalog with `--catalog PATH`.

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
