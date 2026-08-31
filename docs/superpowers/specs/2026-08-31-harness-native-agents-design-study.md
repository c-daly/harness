# What harness needs to run agents natively

**Date:** 2026-08-31
**Status:** design study; small gap list follows from it

## What this is

This began as a plan to port agent-swarm onto harness. It is not that any more.

Investigating the port produced a more useful result than the port would have:
**~4.1k LOC of agent-swarm is workaround structure for constraints Claude Code
imposed on a plugin that did not own the shell** — and harness, which does own
the shell, already supplies natively almost everything that structure was
constructing.

So the deliverable is the analysis plus a short list of genuine gaps, not an
implementation plan for moving code.

## agent-swarm is not touched

**No change of any kind lands in the agent-swarm repository.** Not a refactor,
not an adapter, not a shared core, not a conditional. agent-swarm continues to
run under Claude Code exactly as it does today; this work is invisible to it.

There is no shared shell-agnostic core with a CC adapter and a harness adapter —
that would couple the two and require restructuring agent-swarm to expose the
seam. Nothing is deleted anywhere. Where this document says a component is *not
needed*, it means **harness does not need to build it**, never that agent-swarm
should remove it.

The ~13k LOC of workflows, orchestration, experiments, and stores stays in
agent-swarm serving CC. harness gets nothing there from this work, and nothing
here proposes it should.

## The finding: six workarounds, one constraint

Claude Code gave a plugin no way to own tool dispatch, and gave subagents no way
to hold MCP connections or answer permission prompts. Six agent-swarm subsystems
exist to work around that, and they are the same workaround six times over:

| # | agent-swarm | LOC | why it exists |
|---|---|---|---|
| 1 | `lib/permissions.py` | 539 | CC gave a plugin no way to gate its own tool calls |
| 2 | `hooks/native-tool-blocking.py` | 255 | blocks CC's natives to force calls through the router, where they *could* be gated |
| 3 | `lib/native_tools.py` | 513 | reimplements Read/Write/Bash because (2) blocked the originals |
| 4 | `agents/*.md` → `tools: Bash(mcp*)` | — | Bash as **transport**: *"You have exactly one tool: `Bash`. Every real action runs through `mcp-call`"* |
| 5 | `hooks/subagent-mcp-bypass.py` | 69 | *"Auto-approves MCP tool calls from subagents to bypass the 'prompts unavailable' permission denial"* |
| 6 | router + daemon + backends | ~2,350 | a TCP broker at `:7523` holding the MCP connections subagents could not hold themselves |
| — | `lib/jsonl_extractor.py` | 461 | scrapes `~/.claude/projects/*.jsonl` because CC exposed no first-party usage stream |

The full path for a CC subagent to reach one tool:

```
subagent → Bash (its only tool) → mcp_call → TCP :7523 → daemon
  → Router → Controller → backend (Popen'd MCP server)
```

In harness a subagent is an in-process child session sharing the parent's
registry (`subagent.py`: *"child sessions, concurrent in-process, parent's
enforcement applies"*). It reaches a tool by `FilteredRegistry.get(name)`. One
hop, no broker, no prompt, no bypass, no transport.

**This is why the hard CC coupling is only 23 references across 8 files.** The
coupling is concentrated entirely in the workaround layer.

## What harness already has

Stated explicitly, because the port framing obscured it:

| capability | harness | notes |
|---|---|---|
| Agent dispatch | `dispatch_agent` native tool (`subagent.py:125`) | default permission rule (`native_tools.py:667`); routing pin (`routing.py:4`); telemetry follows child sessions (`telemetry.py:315`); TUI panel per dispatch (`tui_panel.py:88`) |
| Agent definitions | `AgentDef` — tools, model, strategy, experts | plus mixture-of-models fan-out (ensemble / panel / draft_refine / escalate) — no agent-swarm equivalent |
| Per-agent enforcement | `FilteredRegistry` | *"restricts advertisement (specs) AND execution (get)"*; `dispatcher.py:147` executes through it |
| MCP connectivity | `mcp_host.py` — *"Owns all connections"* | verified: 90 tools over stdio |
| Permissions | `PermissionEngine` | layered, deny-absolute across layers, priority 1000 behind plugin hooks, `model:<route>` addressable |
| Interception | dispatch hooks — `Allow \| Block \| Rewrite \| Ask` | priority chain, fail-closed, covers model calls too |
| Usage / cost | `ModelCallCompleted` + `telemetry.py` | usage, pricing **stamped at call time**, duration; derived SQLite store; run = session + descendants via `parent_session_id` |

Dispatch is *more* native in harness than in agent-swarm, where it rides on CC's
`Task` plus a gating hook. And harness emits first-party usage, so the transcript
scrape (7) has no harness analogue to build.

## The gap list

Everything above is present. What is actually missing:

1. **`max_output_chars` on `AgentDef`.** agent-swarm's agents bound their output
   (2000–5000 chars); harness has no such field and `SubagentRunner` returns
   `loop.run_turn()` unbounded. Add the field; enforce at the `SubagentRunner`
   boundary.

2. **`SubagentSpawned.agent` is never populated.** The field exists on the event
   (`events.py:172`), and `SubagentRunner.dispatch` knows the agent — it is a
   parameter, and `definition` is resolved from it — but `subagent.py:63` appends
   `SubagentSpawned(child_session_id=child_id, model=chosen)` and drops it. Every
   dispatch in the log records a model and not which agent ran.

   Consequences today: `tui_panel.py:127` recovers the name by scraping
   `ToolCallProposed.args`, and `telemetry.py` has no agent attribution at all,
   so per-agent cost cannot be computed from the log. One-line fix; delete the
   TUI workaround after.

3. **`SubagentSpawned` carries no proposing `call_id`.** Genuinely missing, and a
   correctness problem rather than a gap. `tui_panel.py`'s docstring: *"every
   `SubagentSpawned` seen while that stack is non-empty is attributed to its top
   ... does not attempt to disambiguate two coordination calls genuinely running
   concurrently in the same turn — `SubagentSpawned` does not carry its proposing
   `call_id`, so there is no way to recover that in general."*

   So dispatch attribution is a sequential-assumption heuristic that goes
   **silently wrong** when two coordination calls overlap — precisely the mixture
   fan-out case harness is built for. The event is declared `is_intent = True`
   because *"the spawn record (causal link) must survive a crash"*, yet carries no
   link to what caused it.

**No new event types are needed.** Both of these are fields on an existing event,
and both are additive-optional — the same shape as `ModelCallCompleted.stop_reason`
(*"additive, default keeps old logs valid"*), so old logs stay readable.

That is the list.

## Not needed, and why

- **Arg-scoped tool permissions.** `Bash(mcp*)` is a transport shim, not a safety
  rule. In harness an agent is handed its real tools, so `reviewer` gets
  `read_file`/`glob`/`grep`/serena and no bash at all. Nothing to scope. The 8
  `[degraded]` entries in the import report are the importer correctly reporting
  that it discarded something which should not survive.
- **`can_write_files`.** Dissolves into the tool list — omit `write_file`/
  `edit_file`. It is a separate field only because Bash-as-transport made the
  tool list unable to express it.
- **A principal in the dispatch vocabulary.** Not required by anything here.
- **A daemon, router, or broker.** harness talks to MCP servers directly.
- **New lifecycle hook points.** The port needs none. Whether harness wants a
  post-tool interception point is a **harness** question, out of scope here (see
  below).

## Harness-level questions this study is not entitled to settle

harness is a Claude-Code-class shell in its own right. Its own design questions
must be answered on harness's terms, not derived from a plugin's requirements —
a plugin port is evidence about what a shell needs, never the requirements
source. Especially this plugin, so much of whose shape is Claude Code workaround
structure that deriving harness's design from it would re-import the very
constraints the analysis identified.

Named and left open:

- **MCP connection lifecycle.** agent-swarm's daemon kept backends warm across
  sessions; `McpHost` starts them per session. Whether harness wants cross-session
  reuse is a harness question. Noted only because the analysis made it visible.
- **Concurrency / threading model.** Subagents are *"concurrent in-process"* on a
  shared `HookBus` with per-child registries. That is a harness design choice with
  consequences (a per-agent dispatch hook would leak onto siblings) and deserves
  deliberate treatment.
- **The unfired lifecycle points.** `POST_TOOL`, `PROMPT_SUBMIT`, and
  `PRE_COMPACTION` exist in `LifecyclePoint`, never fire, and warn at
  `plugins.py:186`. `Inject` is only effective at `SESSION_START` (`loop.py:79`).
  Wire, delete, or keep — harness's call, on harness's grounds.
- **Denial observability.** `FilteredRegistry` narrowing emits no event, so "never
  needed bash" and "was refused bash" are indistinguishable. Costs more if the
  event log becomes a telemetry substrate of record. Unlike gaps 2 and 3 this
  would need a *new* event type, which is why it stays a harness question rather
  than joining the gap list.
- **Finer-grained permissions.** Visibility plus the engine is sufficient for what
  is here, not in general. Directions: arg-scoped rules, a principal, dynamic
  phase layers. The engine's bones already anticipate them.

## Non-goals

- Porting workflows, orchestration, the experiment system, or stores.
- Changing agent-swarm in any way.
- Making `harness import` handle agent-swarm. The importer did its job; what it
  flagged as degraded was mostly workaround structure that should not survive.
- Deciding harness's own architecture from this analysis.

## Verification

1. `max_output_chars` is honoured — a subagent exceeding it is truncated at the
   `SubagentRunner` boundary, with the truncation legible.
2. Per-agent cost is computable from the event log alone, with no reference to
   `ToolCallProposed.args`.
3. Two coordination calls fanning out concurrently in one turn attribute their
   experts correctly, and `tui_panel`'s sequential-assumption heuristic is gone.
4. `git -C <agent-swarm> status` is clean and its history unchanged. Any commit
   to that repository is a failure, whatever else was achieved.
