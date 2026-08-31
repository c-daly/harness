# agent-swarm → harness: the port as subtraction

**Date:** 2026-08-31
**Status:** design approved, daemon deferred to its own spec

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

That is ~1.8k LOC that deletes rather than moves.

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
| router / MCP surface / daemon | ~2,350 | **open** — see Open Questions |
| workflows, orchestration, experiments, stores | ~13,000 | **stays put** — shell-agnostic |
| 19 skills / 8 agents / 2 commands | prose | **already converted** by `harness import` |

## Decisions

### 1. Tool visibility is the permission mechanism for agents

`FilteredRegistry` is not a display filter. Its docstring: *"restricts
advertisement (specs) AND execution (get)"*, and `dispatcher.py:147` executes
through it — `self.registry.get(effective.tool)(dict(effective.args))`. It is
already a per-child enforcement point in the call path.

It covers agent scoping almost completely. The one thing it cannot express is
**partial (arg-scoped) permission** — `Bash(mcp*)`, the exact rule the import
report degraded on all 8 agents.

**Decision:** extend visibility rather than introduce a principal into the
dispatch vocabulary.

- `AgentDef.tools` entries gain an optional arg pattern. CC's `Bash(mcp*)`
  syntax already expresses this; the importer parses it and discards it.
- `FilteredRegistry.get` returns a wrapper that validates args against the
  constraint before delegating.
- A violation raises a distinct error the model can correct against — not
  `UnknownToolError`, which presents as a phantom missing tool.

This is per-child, closes over its own constraints, and needs no change to the
shared `HookBus` or to `PermissionEngine`.

**Rejected:** threading a principal (`agent_id`/`role`/`phase`) through
`ProposedToolCall` and unifying `FilteredRegistry` with `PermissionEngine`.
Larger, and unnecessary once the registry is recognised as an enforcement point.

**Constraint that forced this:** children get `hooks=self.hooks` — the *parent's
shared* `HookBus` — while the registry is per-child. A per-agent dispatch hook
would leak onto concurrent siblings. The registry was always the right home.

`PermissionEngine` keeps main-loop `Ask`/grants and model-call gating
(`model:<route>`). It is unaffected by this work.

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

**Decision:** delete `POST_TOOL` and `PROMPT_SUBMIT` from `LifecyclePoint`. They
advertise capability nothing needs and keep a warning path alive at
`plugins.py:186`.

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

## Open questions

### The daemon

`router.py` (501), `mcp_native.py` (766), `backends.py` (300), `daemon.py`
(268), and `daemon_client.py` (312) — ~2.35k LOC — are two things bolted
together: an **MCP multiplexer** fronting other MCP servers plus native tools,
and a **cross-session state holder** for the agent registry, workflow state, and
worker pool.

harness's `mcp_host.py` already multiplexes MCP servers, so the first half is
plausibly redundant. The second half is not: harness sessions are independent and
have no cross-session home.

Three shapes, none chosen:

- **Keep it.** agent-swarm stays a separate long-lived service that harness talks
  to over MCP. Smallest change; keeps a second process and a second state store.
- **Absorb it.** Cross-session state becomes a harness plugin with its own store.
  One process; requires harness to grow a concept it does not have.
- **Dissolve it.** Make the state per-run rather than cross-session, if the
  workflows genuinely need no continuity between top-level sessions.

This needs its own spec. Nothing above depends on the answer.

## Non-goals

- Porting the workflow engine, orchestrator, experiment system, or stores. They
  are shell-agnostic and move nowhere.
- A permissions rewrite. `PermissionEngine` is unchanged by this work.
- Making `harness import` handle agent-swarm. The importer did its job; the
  remainder is design, not conversion.
- Preserving CC compatibility anywhere. CC is leaving.

## Verification

The port is done when, on harness with no Claude Code in the loop:

1. A `reviewer` agent is denied `bash` outside `mcp*` args, natively, and the
   denial is legible to the model.
2. A dispatched agent is registered and gated through the router by a harness
   dispatch hook.
3. The session-start briefing appears via a `SESSION_START` `Inject`.
4. A harness run's token usage and cost reach the OTEL exporter without any
   `~/.claude/projects` file existing.
5. The ~1.8k LOC marked **retire**/**delete** in Disposition is actually gone
   from agent-swarm — not merely bypassed. A port that adds the harness path
   while leaving the CC path in place has failed this spec's central claim.

## Sequencing

1. **Arg-scoped tool visibility** (Decision 1) — unblocks agent fidelity; the
   only change with no dependencies.
2. **Hook rewrite** (Decisions 2, 3) — 2 dispatch hooks, 1 session-start
   contribution, 2 subscribers, `PRE_COMPACTION` wired, dead points deleted.
3. **Telemetry source swap** (Decision 4) — exporter reads harness's store.
4. **Daemon** — separate spec.

Each phase is independently useful and independently revertible.
