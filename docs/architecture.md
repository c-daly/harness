# Harness — Architecture

This document explains how the harness works internally: the event-sourced
spine, the kernel loop and dispatcher, how tools and hooks compose, and how the
plugin and importer layers sit on top. Read it before modifying the kernel.

For day-to-day contribution mechanics and the invariants you must preserve, see
[contributing.md](contributing.md). For writing plugins against these
internals, see [plugin-authoring.md](plugin-authoring.md).

The ongoing core-agency implementation adds
[bounded inference, native/external tasks, and core improvement records](core-inference-and-improvement.md).
That document identifies the implemented interfaces and remaining agent-runtime,
activation, and local-readiness boundaries.

[Local runtime readiness](local-runtime-readiness.md) adds explicit local profiles,
timestamped availability evidence, and process ownership to the shared core scope.

[Context profiles](context-profiles.md) bound conversation requests and tool
inventories while retaining canonical history and normal plugin ownership.

---

## The one big idea: the event log is the unit of truth

A session is an **append-only log of events**. The model's messages, every tool
call proposed and completed, every permission decision, every hook decision,
retries, errors — all of it is recorded as a typed event before it takes
effect. Nothing about a session's state lives only in memory: the conversation
you feed the model is a pure function of the log (the *fold*), and the log can
be replayed to reconstruct that state exactly.

This single decision drives most of the design:

- **Resume and replay are free.** Reopen a log, fold it, and you have the exact
  state to continue from. (`resume.py`, `fold.py`)
- **Telemetry is a query, not a side channel.** Reliability-per-provider,
  cost, latency — all derivable from the log. (`telemetry.py`)
- **Determinism holds across crashes.** Intent events are fsynced before the
  action they describe runs, so a crash mid-tool-call replays coherently.
- **New event types must stay backward-compatible.** The event union is closed
  and additive; old logs must always still parse. (See *The event union* below.)

---

## Module map

The source is flat under `src/harness/`. Grouped by role:

**Event spine**
- `types.py` — newtypes (`SessionId`, `ModelId`, `ToolName`, …).
- `events.py` — the closed, discriminated event union (the schema of the log).
- `blobs.py` — content-addressed blob store; large tool results spill here.
- `log.py` — append-only JSONL writer/reader with torn-line tolerance.
- `session.py` — `Session`: appends events, stamps sequence numbers, owns blobs.
- `messages.py` — message/content-block model handed to providers.
- `fold.py` — pure reduction of a log into model-ready state + projections.
- `resume.py` — rebuild a `Session` from an existing log.

**Kernel**
- `agent.py` — typed tasks, results, progress, and durable agent run boundaries.
- `agent_runtime.py` — external task binding, initially Codex, retaining dispatcher authority.
- `resources.py` — bounded local inventory checks, freshness, and owned process lifetime.
- `context.py` — explicit context profiles, complete-turn selection, and input byte accounting.
- `loop.py` — `AgentLoop`: build context → model call → dispatch → repeat.
- `dispatcher.py` — the single enforcement point for tool *and* model calls.
- `tools.py` — `Tool` protocol, `ToolRegistry`, `FilteredRegistry`.
- `hooks.py` — the two hook families (dispatch + lifecycle) and the `HookBus`.
- `interaction.py` — `Resolver` protocol for answering permission `Ask`s.
- `subagent.py` — child sessions as in-process asyncio tasks (`dispatch_agent`).

**Providers**
- `provider.py` — `ModelProvider` protocol, typed error family, `EchoProvider`.
- `provider_litellm.py` — the LiteLLM-backed provider (multi-model) and
  `CatalogProvider`, which multiplexes per call on the catalog entry's `backend`.
- `provider_claude_code.py` — subscription-CLI backend: one turn = one headless
  `claude -p` subprocess, with harness tools served to it over `mcp_serve.py`.
- `provider_codex.py` — subscription-CLI backend: one turn = one headless
  `codex exec --json` child, with harness tools served to it over
  `mcp_serve.py` and injected via a spawn-time `-c mcp_servers.harness.url`
  override (the `codex mcp-server` transport gates every tool call behind an
  elicitation no headless client can answer).
- `provider_antigravity.py` — subscription-CLI backend: one turn = one
  headless `agy -p ... --output-format stream-json` child (Google-account
  OAuth subscription auth), with harness tools served to it over
  `mcp_serve.py` and registered via a spawn-time `agy mcp add` subprocess
  (`agy` has no dotted-config-override flag the way codex does). Unlike
  codex's read-only sandbox, `agy`'s ~57 built-in tools are unconfined by
  anything the harness controls — the scratch cwd/HOME steer, they do not
  restrict reads or writes.
- `catalog.py` — model alias → route/backend resolution.

**Permissions & telemetry**
- `permissions.py` — rule model + `PermissionEngine` (the innermost hook).
- `telemetry.py` — event subscribers that compute stats.

**MCP**
- `mcp_config.py` — layered `mcp.toml` config.
- `mcp_host.py` — server lifecycle, tool namespacing, restart budget.
- `mcp_serve.py` — the inverse of `mcp_host.py`: the harness's own tool registry
  served *outward* over streamable-HTTP MCP, every call routed through the
  dispatcher (used per-turn by the claude-code backend).
- `mcp_import.py` — `.mcp.json` → `mcp.toml` importer with refusal rules.

**Native tools & workspace**
- `workspace.py` — path-confinement law + the canonicalization hook.
- `native_tools.py` — `read_file`/`write_file`/`edit_file`/`glob`/`grep`/`bash`,
  the baseline permission ruleset, and the registration helper.
- `todo.py` — the `todo` tool + its event-sourced state.
- `skills.py` — `SkillSet`, `invoke_skill` tool, the inventory hook.

**Plugins & importer**
- `frontmatter.py` — YAML-frontmatter parsing for skills/commands/agents.
- `plugins.py` — plugin discovery, manifest validation, the eight primitives.
- `cc_import.py` — the Claude Code plugin importer (converter + report).
- `parity.py` — the frozen CC→native tool-name map (the importer's contract).

**Surface**
- `cli.py` — `build_kernel`, `run_once`, and the `harness` CLI.
- `tui.py` / `tui_support.py` — the Textual UI (subscriber + decision provider).
- `redaction.py` — the day-one redaction seam (identity by default).
- `errors.py` — shared error types.

---

## The event union

`events.py` defines a closed, Pydantic-discriminated union keyed on a `type`
literal. Each event is a frozen model. There are ~26 concrete types —
`SessionStarted`, `UserMessage`, `ToolCallProposed`, `HookDecided`,
`DispatchResolved`, `ToolCallCompleted`, `PermissionRequested`,
`SubagentSpawned`, `TodoListUpdated`, `CustomEvent`, and so on — plus a fallback
`UnknownEvent`.

Two rules keep old logs readable forever:

1. **Additive-with-defaults only.** A new event type may be added to the union;
   a new field on an existing type must have a default. You may never remove or
   re-type a field.
2. **Preserve-and-skip.** When the parser meets a `type` it doesn't recognize
   (a log written by a newer harness), it decodes it as `UnknownEvent` carrying
   the raw payload, rather than crashing. The fold ignores unknown events.

`CustomEvent(namespace, name, data)` is the **plugin-visible** channel — plugins
emit and observe these without touching the closed union. Native kernel features
that need first-class events get their own type (e.g. `todo` gets
`TodoListUpdated`), keeping the plugin channel uncontaminated.

Nothing precedes `SessionStarted` in a log.

---

## The kernel loop

`AgentLoop.run_task()` is a small state machine (`run_turn()` remains a
text-returning compatibility method):

```
build context (fold the log)  →  model call  →  dispatch each tool call  →  repeat
```

It runs until the model returns no tool calls (a final answer) or hits the
iteration cap. Sibling tool calls in one model turn are dispatched concurrently
on the event loop. The turn supports **interrupt at any await point**: when you
press Esc, in-flight tool tasks are cancelled, each pending call gets a recorded
`ToolCallCancelled` plus a synthetic result so the log stays well-paired, and
exactly one `UserInterrupt` is recorded — then `repair_turn()` reconstructs a
coherent state to continue from. This pairing discipline (every
`ToolCallProposed` ends with a completion or cancellation) is what keeps fold
and resume correct.

The native task boundary records `AgentRunStarted` before execution and
`AgentRunFinished` after cleanup. A task result distinguishes completion from
iteration exhaustion, token cutoff, failure, cancellation, and interrupted-run
recovery. Task/run identities connect model calls and nested agent runs;
verified output blobs retain full answers. Acceptance criteria remain unverified
by execution itself. On resume, open task runs become `aborted` after their
model/tool intents are repaired, without repeating uncertain side effects.

Blocking I/O (file reads, glob/grep walks, bash) is offloaded to threads via
`asyncio.to_thread` so a slow tool doesn't stall the loop or its siblings.

---

## The dispatcher: one enforcement point

`dispatcher.py` is the **single** place tool and model calls are enforced.
Native tools and MCP tools dispatch identically. For each tool call it:

1. Appends `ToolCallProposed`.
2. Runs the **dispatch hook chain** (`HookBus.run_dispatch`), recording one
   `HookDecided` per decision. A hook may `Allow`, `Block` (→ a denial result),
   `Rewrite` (change the args), or `Ask` (→ a `PermissionRequested`, resolved by
   the `Resolver`, recorded as `PermissionResolved`).
3. Appends `DispatchResolved` with the effective post-rewrite args.
4. Executes the tool: `await registry.get(name)(args)`.
5. Appends `ToolCallCompleted` with the result (or a typed error).

Model proposals also require a terminal fact: `ModelCallCompleted`,
`ModelCallFailed`, `ModelCallCancelled`, or a resume-time `ModelCallAborted`.
The fold tracks open model and tool intents separately; resume repairs both
in their original proposal order without replaying actions. An interrupted
external-agent call may have performed work, so an aborted fact preserves
uncertainty about those side effects.

Internal inference uses the same dispatch path. Its `purpose` (for example,
`compaction`) is recorded and its usage is indexed, but only `conversation`
completions add assistant messages to the transcript. `/compact` consequently
passes through policy and accounting without polluting its own input history.

The telemetry database is a versioned, rebuildable projection. It records
pending and terminal model-call states, including failures, and applies each
session sequence once. An incompatible database is rejected with instructions
to rebuild through `harness stats`; it is never queried using a mismatched schema.
CLI adapter MCP startup and shutdown share a context manager so cancellation
during startup still closes the partially started server.

Key invariants:

- **Errors are values, never crashes.** A tool that raises is caught; the
  message becomes the tool result with `is_error=True` (capped, never spilled
  to a blob). Only infrastructure failures (a failed log write) escape to the
  loop. This is what lets the model self-correct from a bad call.
- **Blob spill.** A *successful* result larger than 16 KiB is written to the
  content-addressed blob store and the event carries a reference instead of the
  inline text. Reads verify the digest, byte size, and regular-file identity;
  corrupt existing objects are rejected. Dispatch materializes results for
  inference and the external-agent transcript bridge; outward MCP resolves
  them through `ToolOutcome.read_text()`. This bounds log lines, not model
  context. Explicit context budgets and retrievable excerpts remain roadmap work.
- **There is no per-tool timeout in the dispatcher.** A tool that can hang (e.g.
  `bash`) owns its own timeout.

Session recovery holds a permanent POSIX advisory guard from read/repair through
sequence selection and writer construction. A PID marker excludes legacy
writers; dead markers are recovered only under the guard. Torn logs are scanned
as bytes, with exact damaged bytes durably quarantined before atomic replacement
of the valid prefix. A JSON value without its final newline is an uncommitted
record. Identity or sequence inconsistencies fail loudly rather than being
silently repaired. Clean readers can observe live logs without taking ownership.

`ExecutionScope` carries the active session, cumulative registry view, and one
shared `ExecutionBudget` through native delegation and coordination tools.
Agent definitions narrow the inherited view. The live session tree defaults to
1,024 model attempts (including retries), 4,096 tool dispatches, 128 child
reservations, depth 4, and 16 active children. Exhaustion is an explicit error;
waiting ancestors cannot deadlock a semaphore needed by descendants. These are
live execution limits, not yet durable token or monetary budgets. Callers can
provide `ExecutionLimits` to `build_kernel`.

Final rewritten tool arguments are checked against their registered JSON schema
before execution. External schema references are rejected. Tool cancellation is
recorded by dispatch, with uncertain side effects stated explicitly; transcript
repair reuses that result instead of adding a duplicate terminal event.

Outward MCP URLs contain a random per-server bearer capability. Requests without
it and requests carrying browser origins are rejected before MCP processing.
The URL is a credential and must not be published in diagnostics. This protects
the served tool surface; it does not certify an external CLI's native tools or
filesystem containment.

`InteractionController` owns accepted prompts for both TUI and headless clients.
The first queue is bounded and in memory; it cannot survive a crash. Pending
prompts are not conversation events until their turn starts. The TUI keeps drafts
editable, shows queued work, supports `/queue` list/edit/remove/pause/resume/clear,
and pauses pending work on incomplete results, failure, or cancellation. Mention reads are part of
the active turn's preparation. Model switches apply between logical turns,
including all tool iterations, and shutdown waits for cancelled workers.

The status, queue, and composer share one bottom container so their final
terminal regions do not overlap. Catalog pricing/context lookup reads the
installed LiteLLM metadata snapshot without importing the inference runtime or
fetching remote prices. Catalog overrides remain authoritative; that snapshot
is not a claim of current vendor pricing.

---

## Hooks: two families

`hooks.py` distinguishes two kinds of hook, with opposite failure semantics:

| | Dispatch hooks | Lifecycle hooks |
|---|---|---|
| Return | `Allow` / `Block` / `Rewrite` / `Ask` | `Inject` / `Annotate` / `Emit` |
| Fire on | every tool/model call | session-lifecycle points |
| On failure/timeout | **fail-closed** → `Block` | **fail-open** → skip + warn |
| Order | priority ascending, then insertion order | — |

Dispatch hooks fail closed because they are the enforcement layer — a hung or
broken security hook must deny, not leak. Lifecycle hooks fail open because they
are advisory — a broken "inject git status" hook must not break session start.

The **permission engine registers as the innermost dispatch hook** (priority
1000). The workspace path-confinement guard runs earlier (priority 900) so the
engine matches rules against already-canonicalized paths; the compound-bash
guard sits at 950. Because the bus sorts ascending, 900 → 950 → 1000 means
"canonicalize, then ask-on-compound, then apply policy."

Only `SESSION_START` and `SESSION_END` lifecycle points currently fire; others
(`PROMPT_SUBMIT`, `POST_TOOL`, `PRE_COMPACTION`) are defined but reserved.

---

## Permissions

`permissions.py` is a rules store plus a matcher. A `PermissionRule` is
`(action, tool-glob, arg-match)`; the engine evaluates layered rule sets with
**deny-wins-absolutely** precedence, then first-match in layer order, then layer
default, then a global `ask`. Argument matching is supported today (e.g.
`bash` + `command = "git *"`), which is what makes scoped grants like
`bash(git *)` work. The engine is strictly opt-in by the presence of config:
with no rule files and no baseline, it's allow-all (the legacy library path);
with native tools enabled, `build_kernel` installs the baseline as the
outermost (lowest-precedence) layer.

---

## Providers and multi-model

`ModelProvider` is a small protocol: take messages + tool specs, stream typed
chunks back. `provider_litellm.py` implements it over LiteLLM, so any provider
LiteLLM supports is reachable by a catalog alias; `CatalogProvider` dispatches
per call on the entry's `backend` field, so an alias can instead resolve to
`provider_claude_code.py` — a subscription-CLI backend where one `complete()`
call spawns one headless `claude -p` turn, with the harness's tools mounted
into it via `mcp_serve.py` and every tool call still flowing through the
dispatcher. Provider errors normalize to a typed family (rate-limit,
overloaded, context-overflow, auth, network) at the boundary; retry/backoff
lives in the kernel and is itself recorded as `RetryAttempted` events.
`EchoProvider` is a deterministic fake for tests.

Multi-model is not a feature bolted on — it's the default posture. A subagent
can run a different model; a plugin can request one; adversarial review across
two models is just two dispatches. Model choice is a catalog alias (data),
resolved through `catalog.py`, never a hard-coded route.

---

## Subagents

`subagent.py` runs child sessions as **in-process asyncio tasks**. The
`dispatch_agent` tool launches one; N children run concurrently with defined
cancellation (cancelling a parent cancels its children, recorded as facts) and
partial-failure semantics (a failed child returns a typed error to the parent,
which decides what to do). A child's tool inventory can be narrowed to an
allow-list via `FilteredRegistry` — a read-only view over the parent registry,
which is how plugin-defined agents restrict their tools. The reduce step (what
to do with N child results) belongs to the parent, not the kernel.

---

## MCP

`mcp_host.py` manages MCP server processes: a single-use start latch,
task-bound lifecycle obeying anyio's cancel-scope rules, and a per-episode
restart budget (three consecutive failures trips it). Tools are namespaced
`mcp__<server>__<tool>` and flow through the *same* dispatcher and permission
engine as everything else. `mcp_config.py` provides the layered `mcp.toml`;
values are env-var names, never literals, so configs are commit-safe.

---

## Plugins (in one paragraph)

`plugins.py` loads a plugin directory into eight primitives: **skills**,
**commands**, **agents** (markdown + YAML frontmatter, auto-discovered);
**dispatch hooks**, **lifecycle hooks**, **subscribers** (Python callables
loaded via `importlib` under synthetic module names); **MCP servers** (merged at
lowest precedence); and **emitter namespaces**. The cardinal law: manifest,
dependency, and callable errors surface at **load time**, never at runtime — a
broken dispatch hook that registered would turn fail-closed enforcement into a
runtime denial-of-service, so it must never register. Loading is all-or-nothing.
Full details in [plugin-authoring.md](plugin-authoring.md).

---

## The TUI

`tui.py` is a Textual app that is, architecturally, just two things: a
**subscriber** to the event stream (it renders what it sees) and a **decision
provider** (it resolves `PermissionRequested` futures via a `Resolver`). It owns
no session state — the same `run_once` ordering contract drives both it and
headless mode. This is deliberate: the UI being a pure subscriber is what keeps
the headless path a faithful equal rather than a degraded sibling.

---

## Reading order for newcomers

1. `events.py` — learn the vocabulary of the log.
2. `fold.py` — see how a log becomes model state.
3. `loop.py` + `dispatcher.py` — the beating heart.
4. `hooks.py` + `permissions.py` — how enforcement composes.
5. `plugins.py` — how third-party code plugs in.
6. A real plugin: `plugins/memory/`.
