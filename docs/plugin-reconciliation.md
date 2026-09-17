# Plugin workflow reconciliation

Core needs to refer to two kinds of plugin-managed state without importing
either plugin: an agent-swarm workflow (phase, status) and a memory-plugin
contribution (a context read, an accepted written record). This document is
the contract for how core does that, and what it explicitly refuses to do.
Implementation: `src/harness/plugin_reconciliation.py`. Tests:
`tests/test_plugin_reconciliation.py`, exercised against the reference
`plugins/memory` plugin and a stub agent-swarm server
(`tests/fixtures/agent_swarm_stub.py`, in-memory only, never installed for
real use).

## The contract

Core already appends ToolCallProposed, DispatchResolved, and
ToolCallCompleted facts for every MCP tool dispatch, whether the tool
belongs to agent-swarm, memory, or any other plugin. `plugin_reconciliation.py`
never imports a plugin. It only reads those facts back out of the session
log and its verified blob sidecar. Calls whose results spill past the dispatcher's
inline threshold must produce the same report as inline calls. The projection
helpers accept a `blobs=BlobStore(...)` argument for these results; live checks
and both CLI consumers supply the session's store automatically. Missing or
corrupt result blobs refuse the report rather than inventing successful writes
or workflow references. Inspection never creates missing blob directories.

- `project_plugin_workflows(envelopes)` folds successful completed calls to
  `mcp__SERVER__workflow__workflow_*` into a `PluginWorkflowRef` per
  (server, workflow_id): the last known phase, and the log position (seq
  and call id) that produced it. Dispatcher errors and plugin JSON `error`
  results neither create refs nor update existing ones. For successful calls,
  an unexpected or unparsable result still yields a ref, with `last_phase=None` the first
  time and left unchanged on a later call that carries no phase. It never
  invents a phase. Blob-integrity failures remain explicit errors.
  The effective tool and arguments from `DispatchResolved` take
  precedence over the original proposal, including after hook rewrites.
- `count_memory_contributions(envelopes)` counts completed
  `mcp__memory__memory_get`, `memory_list`, and `memory_brief` calls
  dispatched with `purpose=context` -- reads the agent made to gather
  context, not reads made for any other reason.
- `count_accepted_records(envelopes)` counts completed
  `mcp__memory__memory_write` calls whose result text is not an
  `error: ...` value. The reference memory plugin reports rejections as
  values, never as an MCP tool error, so counting `is_error` alone would
  overcount.
  Both memory counters use the effective tool from `DispatchResolved`, with
  the original dispatch purpose retained for context reads. Older logs without
  a resolution fact fall back to the proposed tool.
- `reconcile_from_log(envelopes)` builds a `ReconciliationReport` from the
  log alone. Every ref is classified `unknown`: with no live check, core
  never invents a status.
- `reconcile(kernel)` is the live version. For every ref, it dispatches
  `workflow__workflow_get_state` through `kernel.loop.dispatcher` -- the
  same dispatcher, the same permission chain, the same event log as any
  agent tool call. A tool the registry does not know (the server was never
  connected, was disabled, or has since been removed) or a call that
  completes as an error is classified `unavailable` and listed in
  `non_resumable`. A hook redirecting the check to another tool or workflow
  leaves the original ref's status `unknown`. The returned report includes
  the phases and counters projected after the live dispatches, with live
  check attempts distinguished from log-only observations.
  The tool calls remain durable, auditable log facts, including successful
  phase observations. A later log-only report still classifies every status
  as `unknown`; current status requires another live reconciliation.
- `harness plugins reconcile SESSION_ID --base-dir DIR` prints a
  `reconcile_from_log` report. It never starts, connects to, or otherwise
  contacts an MCP server. `harness status` joins one summary line built
  the same way.

## The law

An in-memory plugin workflow lost across a plugin restart is not resumable
by core. If the agent-swarm plugin process restarts, its in-memory
workflow state is gone; core cannot recover it, guess at it, or persist a
copy for the plugin. Core can only report what the plugin says right now,
through an ordinary dispatch, or report `unavailable` when that dispatch
fails. This is a deliberate boundary, not a gap: the plugin owns its state
format and its persistence strategy, and core enforcing or repairing that
state would mean core importing plugin internals, exactly what this
contract exists to avoid.

## Independence properties

Verified in `tests/test_plugin_reconciliation.py`:

1. **A workflow started through the plugin surface, with memory
   contributing context and receiving an accepted record, reconciles as
   active after a core restart.** The stub keeps running; core rebuilds
   its session and dispatcher from the log, reconnects to the still-live
   stub, and the live `workflow__workflow_get_state` dispatch reports
   `active`.
2. **Restart reconciliation reports the truth, not the last known good
   state.** Both a removed stub and a restarted, reachable stub with empty
   in-memory state yield `unavailable` and a non-resumable workflow. In the
   latter case, the new stub returns a domain-level not-found result while
   the core task and its requirements, tracked by `TaskService` and never
   touched by this module, remain fully inspectable and continuable.
3. **Removing either plugin leaves the core task inspectable and
   continuable.** With the memory plugin absent, core continues without
   error and reports zero memory contributions; with the agent-swarm stub
   absent entirely, core reports zero workflow refs and raises nothing.
4. **Installing either plugin alone creates no dependency on the other.**
   Reconciliation logic for workflow refs and for memory counters reads
   disjoint tool-name prefixes (`mcp__SERVER__workflow__workflow_*` versus
   `mcp__memory__*`) from the same log; neither counter references the
   other plugin surface, so either plugin can be installed, exercised, and
   reconciled with the other completely absent.

Old logs with no MCP calls at all fold to an empty report:
`ReconciliationReport(refs=(), statuses={}, non_resumable=(),
memory_contributions=0, accepted_records=0)`.
