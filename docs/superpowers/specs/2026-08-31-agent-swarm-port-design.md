# agent-swarm → harness: the port as subtraction

**Date:** 2026-08-31
**Status:** design approved; no open questions

## Problem

agent-swarm runs today as a Claude Code plugin: 8 agents, 19 skills, 2 commands,
6 hooks, and ~17k LOC of `lib/` fronted by an MCP router exposing 90 tools. The
goal is to make it run natively on harness, with Claude Code out of the picture.

The first attempt was `harness import` — the CC plugin converter. It produced a
loadable plugin whose prose converted cleanly but whose behaviour did not: all 8
agents came in `[degraded] Bash(mcp*) arg-scope dropped -> bash`, all 6 hooks
were flagged for hand-port, and the MCP command still carried an unexpanded
`${AGENT_SWARM_ROOT}`. That outcome is correct — the importer is a converter,
not a compatibility layer — but it means the port is a design problem, not a
conversion problem.

## Scope, and what this spec may not decide

This is a **port** spec: it answers "how does agent-swarm run natively on
harness." That framing entitles it to say what the port does not need. It does
not entitle it to reshape harness.

- **In scope:** declining to build something because the port does not require
  it. That leaves harness unchanged.
- **Out of scope:** removing, constraining, or setting policy on harness's own
  existing surface because a plugin does not exercise it. harness is a
  Claude-Code-class shell in its own right; its concurrency model, MCP connection
  lifecycle, performance characteristics, and hook/event surface are harness
  questions, answered on harness's terms.

Where this spec brushes against one of those, it names it and stops. A plugin
port is evidence about what a shell needs. It is never the requirements source —
especially this plugin, so much of whose shape is Claude Code workaround
structure that deriving harness's design from it would re-import the very
constraints the port exists to shed.

## agent-swarm is not touched

**No change of any kind lands in the agent-swarm repository.** Not a refactor,
not an adapter layer, not a shared core, not a conditional. agent-swarm continues
to run under Claude Code exactly as it does today, and this work is invisible to
it.

That rules out the obvious-looking architecture. There is **no shared
shell-agnostic core with a CC adapter and a harness adapter** — that would couple
the two and require restructuring agent-swarm to expose the seam. What gets built
is a separate thing that happens to be informed by agent-swarm's design.

Consequences, stated plainly because they change the size of the work:

- The ~13k LOC of workflows, orchestration, experiments, and stores does **not**
  come along for free. It stays in agent-swarm, serving CC. harness has nothing
  there until something is built.
- "Retire" and "delete" in this document mean **never built on the harness side**
  — describing code that does not need to exist, not code to be removed. Nothing
  is deleted anywhere.
- The analysis of agent-swarm's ~4.1k LOC of Claude Code workaround structure
  keeps its full value: it is what tells us which capabilities are real and which
  are artefacts of a shell agent-swarm did not own. That is design intelligence,
  not a migration inventory.

## The organizing principle

**A large fraction of agent-swarm is not capability. It is workaround structure
for constraints Claude Code imposed on a plugin that did not own the shell.**

Now that harness is the shell, those subsystems should be **retired, not
ported**. The question to ask of every agent-swarm subsystem is not "how do we
move this" but "is this a capability, or is this a fight with Claude Code?"

Three subsystems already answer *fight*:

1. **Permissions.** `lib/permissions.py` (539 LOC) implements global→role→agent→
   phase gating because CC gave a plugin no way to gate its own tool calls.
2. **Native tools.** `lib/native_tools.py` (513 LOC) reimplements Read/Write/
   Bash because `hooks/native-tool-blocking.py` (255 LOC) blocks CC's originals
   — which it does so that everything is forced through the router where
   agent-swarm *can* gate it. The whole chain exists to buy an interception
   point CC would not give.
3. **Telemetry ingest.** `lib/jsonl_extractor.py` (461 LOC) scrapes
   `~/.claude/projects/*.jsonl` for token usage because CC exposed no
   first-party usage stream to a plugin.

With the daemon and its transport chain (Decision 6), that is **~4.1k LOC** of
capability harness never needs to build, because it does not have the constraint
that produced it.

## The interface is three points, not seventeen thousand lines

agent-swarm **never spawns agents.** `hooks/agent-dispatch.py` is a `PreToolUse`
hook that intercepts `Task`/`Agent` calls, calls `prepare_dispatch()` on the
router, and returns `allow`/`deny`/`ask` plus `additionalContext`. The
`subprocess` calls in `orchestrator.py` and `controller.py` are git operations —
branch checks and worktrees — not process spawning. **The host shell spawns;
agent-swarm intercepts, registers, gates, and annotates.**

So the entire agent-swarm↔shell coupling is:

1. **A tool-call interception point** — allow/deny/ask, plus context injection
2. **An MCP tool surface** — `prepare_dispatch`, `register_agent`,
   `complete_dispatch`, `update_agent_phase`
3. **Session lifecycle signals** — start, end, and a before-compaction ordering
   guarantee

This is why hard CC-contract coupling (`permissionDecision`, `hookSpecificOutput`,
`subagent_type`, `transcript_path`, `.claude/projects`) totals only **23
references across 8 files** — 14 in `hooks/`, 5 in `lib/native_tools.py`, 2 in
`controller.py`, 2 stragglers. The ~4k-LOC workflow engine, the orchestrator, the
experiment system, and the stores are already shell-agnostic.

harness covers all three points today, two of them outright:

| interface point | harness mechanism | state |
|---|---|---|
| interception | dispatch hooks — `Allow \| Block \| Rewrite \| Ask`, priority chain, fail-closed, also covers `ProposedModelCall` | richer than CC's |
| MCP surface | `mcp_host.py` | verified: 90 tools over stdio |
| lifecycle | `SESSION_START` / `SESSION_END` fire; `Inject` effective at start | partial — see Decisions |

## Disposition

| subsystem | LOC | disposition |
|---|---|---|
| `lib/permissions.py` | 539 | **retire** — harness owns permissions |
| `lib/native_tools.py` | 513 | **retire** — harness has native tools (747) |
| `hooks/native-tool-blocking.py` | 255 | **delete** — bought an interception point harness gives natively |
| `lib/jsonl_extractor.py` | 461 | **retire** — harness emits usage first-party |
| `hooks/agent-dispatch.py` | 182 | **rewrite** → one dispatch hook |
| `hooks/session-start.py` | 571 | **rewrite** → one `SESSION_START` lifecycle hook returning `Inject` |
| `hooks/session-end.py` | 314 | **rewrite** → subscriber on `SessionEnded` |
| `hooks/pre-compacting.py` | 277 | **rewrite** → see Decision 3 |
| router / MCP surface / daemon | ~2,350 | **retire** — see Decision 6 |
| workflows, orchestration, experiments, stores | ~13,000 | **stays in agent-swarm** — harness gets nothing here from this work |
| 19 skills / 2 commands | prose | **already converted** by `harness import` |
| 8 agent definitions | prose | **rewrite frontmatter** — name real tools; drop the Bash transport |

## Decisions

### 1. Agent tool access is visibility, and nothing more

`FilteredRegistry` is not a display filter. Its docstring: *"restricts
advertisement (specs) AND execution (get)"*, and `dispatcher.py:147` executes
through it. It is already a per-child enforcement point in the call path.

All 8 agent-swarm agents declare `tools: Bash(mcp*)` — a **single** tool.
`implementer.md` states it plainly: *"You have exactly one tool: `Bash`. Every
real action runs through `mcp-call`."* Bash is a **transport**, not a capability:
CC subagents could not be handed MCP tools directly, so every action was funnelled
through a shell shim and `Bash(mcp*)` scoped that shim.

**Decision:** no arg-scoping. Rewrite each agent's frontmatter to name the real
tools it needs; `FilteredRegistry` hands them over directly. `reviewer` gets
`read_file`, `glob`, `grep`, and the serena tools — and **no bash at all**. There
is nothing left to arg-scope.

`can_write_files: false` dissolves the same way: omit `write_file`/`edit_file`
from `tools`. It exists as a separate field only because Bash-as-transport made
the tool list unable to express it.

**Rejected:** extending `FilteredRegistry` with arg patterns; threading a
principal (`agent_id`/`role`/`phase`) through `ProposedToolCall`. Both solve a
problem that exists only inside Claude Code. Partial tool permission is a
coherent capability in general — nothing in agent-swarm needs it.

**Consequence:** the 8 `[degraded] Bash(mcp*) arg-scope dropped` entries in the
import report are not capability gaps. They are the importer correctly reporting
that it discarded something which should not survive the port.

**The one genuine gap:** `max_output_chars`. agent-swarm's agents bound their
output (2000–5000 chars); harness's `AgentDef` has no such field and
`SubagentRunner` returns `loop.run_turn()` unbounded. Add the field and enforce
it at the `SubagentRunner` boundary. **This is the only agent-fidelity change
harness actually needs.**

`PermissionEngine` keeps main-loop `Ask`/grants and model-call gating
(`model:<route>`). It is unaffected by this work.

**Deferred, explicitly: finer-grained permission control.** Visibility plus the
existing engine is sufficient *for this port*, not sufficient in general. Known
directions, none of them in scope here:

- **Arg-scoped rules** — `write_file` only under `src/`, network tools only to
  allowlisted domains. Rejected above because agent-swarm does not need it, not
  because it lacks value.
- **A principal in the dispatch vocabulary** — `agent_id` / `role` / `phase` on
  `ProposedToolCall`, enabling per-caller rules the flat `allowed` set cannot
  express. agent-swarm's CC-era design gated through a caller-id registry with
  three distinct caller paths (main, dispatched subagents, SDK subagents); that
  is prior art for the shape, not a plan to reinstate.
- **Dynamic layers** — permissions that change with workflow phase mid-session.
  `RuleSet.load(path)` is file-only today; `FilteredRegistry` is fixed at spawn.
- **Denial observability** — see Decision 5.

The engine's bones already anticipate most of this: it is layered, deny-is-
absolute spans layers, and it sits at priority 1000 behind plugin hooks. That is
the extension point when finer control is wanted. Nothing in this spec forecloses
it; the sequencing simply does not pay for it yet.

### 2. Do not add lifecycle hook points

Checked against what agent-swarm's hooks actually return, rather than against
CC's event taxonomy: two need interception (`native-tool-blocking`,
`agent-dispatch`), one needs `SESSION_START` injection, two are observation with
side effects. **Nothing needs `POST_TOOL` or `PROMPT_SUBMIT`.**

The distinction that earns its keep is not hooks-vs-events but two families:

- **Events / subscribers** — `_pump` over `bus.subscribe(maxsize=1024)`, async,
  out-of-band, lossy under backpressure. Cannot block, rewrite, or inject before
  an action. `fold.py`: *"Never executes side effects; never runs hooks."*
- **Dispatch hooks** — synchronous, in the critical path, fail-closed.

"Deny this dispatch" is not expressible as an append-only fact. That is the whole
case for hooks, and harness already has that family.

**Decision:** the port wires neither `POST_TOOL` nor `PROMPT_SUBMIT`, and adds no
new lifecycle points.

Whether harness should *keep* those enum members is **out of scope** (see Scope).
An earlier draft of this spec deleted them on the grounds that agent-swarm does
not need them — that is a harness architecture decision derived from one plugin's
requirements, which this spec is not entitled to make. A shell may well want a
post-tool interception point for reasons no port would reveal. The observation to
carry forward is only that they are currently unfired and warn at
`plugins.py:186`; the disposition is harness's to decide.

### 3. Compaction ordering

`hooks/pre-compacting.py` saves approval flags before compaction discards
context. It does not steer the summary — its output is a `systemMessage` status
string. So this is an **ordering** requirement, not an interception one, and a
subscriber cannot satisfy it (async, out-of-band, may run after `loop.history` is
replaced).

Compaction lives in the TUI (`tui.py:_run_compact`), not the loop; `fold.py` is
explicit that folding never runs hooks. So `PRE_COMPACTION` cannot be a kernel
guarantee the way the other points are.

**Decision:** keep `PRE_COMPACTION` as the single surviving lifecycle point
beyond start/end, fired from `_run_compact` before the summarize call. Accept
that it is TUI-scoped; headless `-p` does not compact, so nothing is lost today.
Document the asymmetry rather than paper over it.

**Deferred alternative:** move compaction into `AgentLoop` so the point becomes
kernel-wide. Larger change; not required by this port.

### 4. harness's event log replaces the transcript scrape

Current chain:

```
~/.claude/projects/*.jsonl → jsonl_extractor.py → telemetry v2
  → controller.run_dashboard_import() → dashboard/import.py
  → dashboard.db → logos-otel exporter → OTEL
```

The source is CC's own transcripts. A harness session driving the router would
exercise router tools — that state lands in agent-swarm's stores — and produce
**zero** token telemetry, silently. Nothing mis-attributes it; nothing sees it.

harness already emits what the scrape reconstructs. `ModelCallCompleted` carries
`usage` (input/output/cache read/write), `pricing` **stamped at call time**,
`duration_ms`, and `stop_reason`. `telemetry.py` is *"a derived SQLite store
folded from session logs"* with `rebuild_index()`, where a run is *"a top-level
session plus its descendant subagent sessions, followed recursively through
`SessionStarted.parent_session_id`."*

**Decision:** the exporter reads harness's derived store; `jsonl_extractor.py`
retires. The dashboard import → exporter path stays — token metrics flow only
through it — but its *source* changes from scraped transcripts to harness's
first-party event log.

### 5. Denials stay unobserved, for now

`FilteredRegistry` narrowing emits no event, so "the agent never needed bash" and
"the agent was refused bash" are indistinguishable. Nothing downstream consumes
denial records today, and token telemetry does not need them.

**Decision:** accept silent narrowing. Revisit if and when harness's event log
becomes the telemetry substrate of record (Decision 4), because gaps in a
load-bearing log cost more than gaps in an incidental one.

## Decision 6: the daemon dissolves

`router.py` (501), `mcp_native.py` (766), `backends.py` (300), `daemon.py` (268),
`daemon_client.py` (312) — ~2.35k LOC — exist to solve **one** Claude Code
constraint: subagents that can hold no MCP connections of their own and cannot
answer permission prompts.

`daemon.py`: *"Single long-lived process. Owns the Router, which owns the
Controller, which owns all services. Started once, stays alive across sessions."*
`subagent-mcp-bypass.py`: *"Auto-approves MCP tool calls from subagents to bypass
the 'prompts unavailable' permission denial."*

The path for a CC subagent to reach one tool:

```
subagent → Bash (its only tool) → mcp_call → TCP :7523 → daemon
  → Router → Controller → backend (Popen'd MCP server)
```

In harness a subagent is an in-process child session sharing the parent's
registry (`subagent.py`: *"child sessions, concurrent in-process, parent's
enforcement applies"*). It reaches a tool by `FilteredRegistry.get(name)`. One
hop, no broker, no prompt, no bypass.

**Decision:** no daemon. Bash-as-transport (Decision 1), `mcp_call`,
`subagent-mcp-bypass.py`, the TCP broker, and the connection multiplexer are a
single workaround for a constraint harness does not have. They retire together.
harness's `McpHost` — *"Owns all connections"* — is the whole replacement.

This raises the retired total from ~1.8k to **~4.1k LOC**, and makes "the port is
a subtraction" the literal shape of the work rather than a framing device.

### Two residuals, neither requiring a daemon

- **Backend startup cost.** The daemon kept serena and other MCP backends warm
  across sessions; `McpHost` starts them per session. Removing the daemon means
  the port no longer exercises cross-session connection reuse — it does **not**
  mean harness has decided it does not want it. MCP connection lifecycle is a
  harness-level question and is deliberately left open here. Noted only because
  the daemon's removal makes it observable.
- **Cross-session workflow state.** Whether iterate/orchestrate state must
  outlive a top-level session is unresolved. harness has session resume
  (`resume.py`, `sessions.py`) and plugins may hold stores. If continuity is
  needed it wants a store, not a long-lived service.

### Supersedes a prior decision

The 2026-07-13 direction had thinktank seats become regular agent-swarm agents by
registering and routing **through the router TCP daemon** with a source-stamped
caller-id — the backend framing having been rejected because regular-agent
semantics were required. Removing the daemon supersedes that *mechanism*. It does
not contradict the *requirement*: in harness a seat is an `AgentDef` dispatched
through `SubagentRunner`, which is more straightforwardly a regular agent than
anything reached over a TCP broker. The intent survives; the transport dies.

## Non-goals

- Porting the workflow engine, orchestrator, experiment system, or stores. They
  are shell-agnostic and move nowhere.
- A permissions rewrite. `PermissionEngine` is unchanged by this work.
- Making `harness import` handle agent-swarm. The importer did its job; the
  remainder is design, not conversion.
- Preserving CC compatibility anywhere. CC is leaving.

## Verification

The port is done when, on harness with no Claude Code in the loop:

1. A `reviewer` agent's tool list contains no `bash` at all, and it reaches the
   read and serena tools directly rather than through an `mcp-call` shim.
2. A dispatched agent is registered and gated through the router by a harness
   dispatch hook.
3. The session-start briefing appears via a `SESSION_START` `Inject`.
4. A harness run's token usage and cost reach the OTEL exporter without any
   `~/.claude/projects` file existing.
5. Nothing marked **retire**/**delete** was built on the harness side — not
   reimplemented, wrapped, or shimmed.
6. `git -C <agent-swarm> status` is clean and its history unchanged. Any commit
   to that repository is a failure of this spec, whatever else was achieved.

## Sequencing

1. **Agent definitions + `max_output_chars`** (Decision 1) — rewrite the 8
   frontmatters onto real tools; add the one field harness lacks. No dependencies.
2. **Hook rewrite** (Decisions 2, 3) — 2 dispatch hooks, 1 session-start
   contribution, 2 subscribers, `PRE_COMPACTION` wired, dead points deleted.
3. **Telemetry source swap** (Decision 4) — exporter reads harness's store.
4. **Daemon removal** (Decision 6) — last, because phases 1–3 must demonstrably
   work without it before it is deleted.

Each phase is independently useful and independently revertible.
