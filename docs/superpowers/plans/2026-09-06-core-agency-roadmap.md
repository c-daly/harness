# Harness roadmap: a persistent interface with core agency

**Date:** 2026-09-06

**Status:** Accepted for implementation on 2026-09-06. See the
[implementation record](2026-09-06-core-agency-progress.md) for actual progress
and validation; the roadmap's gates are not claims of completed behavior.

**Baseline:** `main` at `ce722b4`.

**Scope:** A local, single-user Harness, initially on Linux/WSL2. Preserve the
existing provider, frontend, and plugin extension points while changing what
the core owns.

> **September 7 resident-workflow addition:** core now fetches explicitly
> configured context through normal tool dispatch before each root attempt,
> retains a previous-attempt brief, and joins task/context/local status. See the
> [resident workflow](../../resident-workflow.md). Offline retrieval, interruption
> and restart continuity passed their narrow real-model checks. The write-artifact
> pilot failed; local tool choice and context relevance remain qualification gaps.
> This does not finish M3, qualify automatic fallback or authorize improvement adoption.

## Direction and review map

Harness should be the consistent assistant and work surface across changing
models, agent runtimes, connectivity, and interfaces. Project information,
normal memory, and durable task records supply continuity. Local inference
provides a useful baseline; remote resources extend what the assistant can do.

**The basic agency belongs in core.** Core owns interaction and task state,
local inference integration, capability awareness, bounded decisions, routing,
delegation, interruption, and recovery. `memory` and `agent-swarm` remain
plugins. They implement memory services and richer workflow policies through
general core contracts; neither is required to make Harness function as an
assistant. Core must not import their Python internals or depend on their
private state formats.

The existing architecture already aims at continuity, provider independence,
and heterogeneous agent work. This roadmap develops that intent. The missing
piece is a coherent core product contract, backed by working integration and
daily-use evidence.

| Revision | What changes from the September 3 hardening plan |
|---|---|
| **NEW — core agency** | A persistent core coordinator can use inference directly, do local work, or delegate a bounded task. Its existence is independent of plugins. |
| **REWORK — models and agents** | Replace the proposed universal completion contract with separate model-inference and agent-run contracts. External CLI agents are agent runtimes. |
| **NEW — local baseline** | Core manages local readiness, resource limits, and inference scheduling, including startup without a network after provisioning. |
| **NEW — semantic functions** | Small, typed model calls support request interpretation, memory relevance, and progress assessment; evaluate them before granting decision authority. |
| **NEW — first-class self-improvement (September 6 steering)** | Core owns evidence collection, improvement candidates, isolated experiments, evaluation, adoption policy, and rollback. This runs through the implementation rather than waiting until after M6. |
| **PULL FORWARD — usability** | Preserve drafts, queue follow-ups, expose real work state, and make interruption useful in the first implementation tranche. |
| **EXTEND — continuity and recovery** | Preserve tasks and artifacts across resource changes; export them without requiring Harness or a vendor transcript to interpret them. |
| **CLARIFY — plugin boundary** | Test core without plugins, then test actual memory and agent-swarm integrations. Their existing feature sets are not evidence that core integration works. |
| **CARRY FORWARD — hardening** | Keep integrity, enforcement, containment evidence, budgets, conformance, accessibility, and measured dogfood gates. Sequence release machinery after a useful product slice. |

This is a proposed replacement for the *ordering and abstraction choices* of
the [production-beta hardening plan](2026-09-03-production-beta-hardening.md).
That document remains the detailed hardening backlog. Its instruction to run
every task in order should not govern implementation under this roadmap;
the crosswalk below identifies retained and revised work. Neither document's
unchecked tasks describe shipped behavior.

## Starting point: substantial machinery, an incomplete operating model

The current core has event-sourced sessions, dispatch hooks, permissions,
native tools, MCP, plugins, an owned agent loop, subagents, mixture strategies,
routing, telemetry, and a substantial TUI. The August native-agent gaps in
agent attribution, spawn call IDs, and output limits have landed. Avoid
rebuilding these facilities under new names.

The important structural problems are:

- `CatalogProvider.complete()` treats raw inference and complete external CLI
  agent executions as the same operation. A text transcript passed to a new
  CLI process is a weak substitute for a task and agent lifecycle contract.
- UI, model selection, context preparation, and execution state remain too
  entangled. The composer clears a submitted draft before refusing it while
  busy; the prompt remains in history, but it has not been queued.
- Local serving exists as a script and an endpoint option. It does not yet
  constitute a managed, measured, always-available core capability.
- Static routing and mixture strategies do not establish that a selected
  resource can complete the task, or that a cheap answer met its acceptance
  criteria.
- Several correctness defects undermine the continuity and delegation
  guarantees the next architecture would need.

The assessment reproduced nested delegation regaining tools excluded from
its parent inventory, incorrect grandchild parent attribution, unreadable
large tool results at model/MCP boundaries, incomplete model-call terminal
events, cancellation cleanup gaps during MCP startup, unchecked blob
corruption, and failure to repair a partial UTF-8 log tail. The nested-tool
finding concerns inherited inventory restrictions: the global permission
engine still applies. `/compact` also bypasses model dispatch and its normal
enforcement/accounting path.

**Evidence boundary:** At this same HEAD, the September 5 assessment reported
885 passed, 7 skipped, and 4 warnings, built the wheel and sdist, and observed
two Ruff E741 failures. Missing Anthropic/Ollama fixtures and an opt-in live
Antigravity test account for the skips. Those results are historical assessment
evidence, not a new test run for this roadmap. The isolated defect probes used
fake providers and temporary sessions. Live provider parity, local model
performance, and sustained terminal usability remain unqualified.

## Core contracts and plugin boundaries

**NEW — make these responsibilities explicit before expanding autonomy.**
Names below are proposed contracts, not a requirement to create a separate
framework or service for each row.

| Contract | Core responsibility | Extension boundary |
|---|---|---|
| Interaction controller | Own submit/queue/edit/interrupt/resume commands, task identity, and observable activity. Both TUI and plain/headless clients use it. | Frontends render state and resolve interaction requests. They do not call providers directly. |
| Model inference | One bounded inference operation: messages, output limits, optional schema/tools, deadline, usage, typed result/error, and purpose. It does not execute tools. | Local and remote inference adapters implement the same owned contract and declare unsupported features. |
| Agent execution | Accept a task, context/artifact references, acceptance criteria, scope, budget, and cancellation token; expose progress and a terminal result. Resume support is explicit. | The native `AgentLoop` and external CLI agents implement this contract. A role definition selects a runtime and, where supported, a model. |
| Core coordinator | Interpret requests, assemble relevant context, answer directly or assign work, track unresolved obligations, and explain resource changes. | Plugins may contribute context, policies, roles, or workflows through existing hook/dispatch surfaces. |
| Capability registry | Track configured resources, supported features, current availability, authority, capacity, and versioned evidence. | Adapters and plugins report capabilities; core probes and records observations. |
| Local runtime service | Discover or start configured local inference, report readiness, schedule bounded calls, and manage core-owned processes. | A replaceable runner/server supplies inference. Core does not depend on one weight family, inference library, or GPU vendor. |
| Context and memory access | Assemble bounded project/session context and query configured memory capabilities with provenance. Apply normal dispatch and write policy. | The memory plugin supplies its reader/writer semantics and stores. Other providers can implement the contract. |
| Delegation and task state | Own task/run IDs, causal parentage, cumulative authority, shared budgets, cancellation, artifacts, and partial results. | Agent-swarm supplies richer workflow definitions, role transitions, experiments, and its own state services. |
| Self-improvement | Own a durable improvement lifecycle, outcome evidence, versioned candidates, bounded experiments, adoption records, and rollback. | Models/agents propose or implement candidates; plugins may contribute evidence or experiment strategies without owning the core loop. |

Keep three concepts distinct: a **model** supplies inference, an **agent**
owns a bounded execution loop, and the **core assistant** owns the continuing
interaction with the user. A runtime may hide or constrain its underlying
model choice; represent that honestly. Do not infer that a model alias exposes
all the capabilities of a named external agent.

The core coordinator should reuse the native execution machinery with a
narrow coordination role. Deterministic code owns state transitions and
enforcement. Model calls propose interpretations or next actions through
that machinery. This does not require an extra model round trip before every
request, nor a second model rewriting every token streamed by a delegate.

The core's durable session/task records remain useful with every plugin
disabled. When memory is configured, the coordinator uses that **normal
memory**, including its existing scope and write rules. An unavailable memory
provider is reported as unavailable; core does not silently create a private
replacement memory store. Basic project-file and session access still works.
Record observations separately from accepted decisions and retain the sources
supporting each memory contribution.

Agent-swarm workflow state remains plugin-owned. Core records its own dispatch
facts and references plugin workflow IDs; the plugin owns workflow transitions.
Integration must specify restart reconciliation. An in-memory workflow server
does not become durable merely because another plugin component uses SQLite.

Core owns local model *integration and operation*. Provide a provisioned default
local profile with pinned runtime/model metadata, readiness checks, and clear
resource requirements. Keep weights outside the Python package, support a
user-managed endpoint, and avoid imposing Docker or CUDA as the core API.
The existing serving script is a starting adapter, not a portable installation
or a verified hardware recommendation.

### First-class self-improvement

**Scope update:** The user explicitly requested self-improvement as a first-class
part of this effort. Core must provide the complete improvement lifecycle, not
only a suggestion to remember failures or an extension point for a later plugin.

Observe task outcomes, explicit corrections, recurring failures, latency, and
resource use. Link each improvement candidate to its evidence, a hypothesis,
the version it changes, a measurable expected benefit, and an evaluation plan.
Candidates can improve prompts, routing/context policy, or source code through
isolated patches. Weight training remains a separate future capability.

Run experiments through the same bounded inference/agent contracts and resource
controls as ordinary work. Compare a candidate against the incumbent on the
same versioned cases; include regression and held-out cases. Candidate code
cannot change its own adoption criteria, evaluation authority, or permissions.
Keep experiments off the active installation and preserve the original version.

Adoption is an explicit, recorded transition governed by configured policy.
Automatic adoption, where allowed, must satisfy predeclared quality/regression
and resource gates. Rejected and inconclusive experiments remain visible.
Activation occurs at a safe boundary and records the exact candidate and
incumbent versions; rollback restores the prior version without replaying task
side effects. Code changes use a separate checkout and patch artifact before
promotion. Never edit a running source tree opportunistically.

Provide an improvements surface for evidence, hypotheses, experiments, pending
reviews, adopted changes, and rollbacks. Core's event/artifact records preserve
this lifecycle without plugins; configured normal memory can retain accepted
lessons through its existing write contract. Avoid a private competing memory.

Introduce evidence capture in M0/M1, lifecycle contracts in M2, local execution
in M3, and the operational propose/evaluate/adopt/rollback loop in M4/M5.
M6 must exercise both a successful improvement and a rejected regression, with
replay and rollback. The specific automatic-adoption policy is being clarified
with the user; independent implementation work does not require assuming it.

## Milestone sequence

**Implementation update, September 7:** [Task evidence](../../task-evidence.md)
now persists explicit user requirements, checks recorded execution artifacts,
and shows unresolved work across prompts and resume. Acceptance is a separate
user decision; models and semantic observations cannot supply it. The initial
checks concern exact recorded bytes, with richer workspace checks and natural
requirement proposals still outstanding. This advances M1/M2 without completing
the useful resident workflow or the M3/M6 qualification gates.

Use exit gates rather than calendar estimates. The largest uncertainties are
local quality/latency on the actual machine and external-agent continuation,
tool visibility, and containment. Measure those early instead of accumulating
features on assumptions.

### M0 — Establish a short, executable baseline

**CARRY FORWARD; keep this small.** Record the current tests, lint, packaging,
supported environments, and known defect cases in reproducible reports. Fix
the existing lint failures and establish CI. Classify current catalog entries
as inference endpoints or agent runtimes, with known versus unverified
capabilities. Capture a baseline real task journey and UI latency trace.

Run an early local feasibility experiment alongside the initial fixes: one
small semantic request and one short conversational/task request on the actual
machine, recording cold/warm latency, memory use, and contention. This informs
M2/M3 without blocking basic correctness work or selecting a permanent model.

**Gate:** Reproducible baseline and regression cases exist; missing live
coverage is explicit. No broad provider-support or offline-readiness claim.

### M1 — Make execution trustworthy and the current interface usable

**CARRY FORWARD + PULL UI FORWARD.** Fix the failures that would otherwise make
a persistent assistant lose information or misrepresent work:

- Preserve complete tool-result semantics across inline/blob storage,
  inference adapters, and outward MCP. Render bounded content explicitly and
  retain retrievable artifact references; never replace a needed result with
  an unusable placeholder. Verify blob hash/size and repair torn UTF-8 tails
  under a real single-writer lock without sacrificing the intact prefix.
- Give model and agent attempts exactly one terminal disposition, including
  blocked, failed, cancelled, and interrupted-with-unknown-outcome cases.
  Put startup inside cleanup scopes and settle child processes on cancel.
  Audit compaction through dispatch with a non-conversational call purpose.
- Propagate actual parent identity and intersect tool scope through every
  delegation level. Establish root-shared limits before increasing fan-out;
  descendants cannot reset their budget or restore excluded authority.
- Introduce the shared interaction controller incrementally. Keep the
  composer editable, acknowledge a queued prompt before clearing it, provide
  queue edit/remove/resume, and show concrete work/wait/recovery states.
  Resolve model/runtime switches at explicit task or turn boundaries.
- Pull forward central argument validation, required environment/redaction
  controls, and MCP authentication for the paths being exercised. Record
  honest execution boundaries before using external agents on real work.

Start with a bounded in-memory prompt queue; disclose that it cannot survive a crash and
preserve drafts when submission fails. Do not insert queued text into the
conversation until it starts. Pause the queue after failure/cancellation.
Any later durable draft feature should use separate recoverable UI storage,
with the same privacy controls, rather than fictional user-message events.

**Gate:** Regression journeys cover large results, nested restrictions,
startup cancellation, storage damage, and busy submission. Every accepted
prompt is visible as queued or running. Cancellation and retry preserve the
record of completed side effects. Final rendered UI, not only widget state,
shows the correct draft, queue, work phase, and recovery action.

**Likely seams:** `dispatcher.py`, `blobs.py`, `log.py`, `resume.py`,
`subagent.py`, `mcp_serve.py`, external provider adapters, `interaction.py`,
`tui.py`, and `tui_panel.py`.

### M2 — Separate inference from agent execution in core

**REWORK the old Task 3.** Introduce the two execution contracts above, using
migration adapters so ordinary API and native-agent behavior continue to work.
Migrate one external CLI first, then the others as their capabilities are
verified. Legacy catalog aliases remain readable, with an explicit execution
kind; avoid a flag-day configuration rewrite.

Inference requests need purpose, bounded input/output, sampling parameters,
deadline/cancellation, optional structured response schema, and typed usage
that preserves unknown values. Simple semantic calls have no tools and no
agent loop. Inference tool proposals execute only when a native agent dispatches
them. Agent runs have separate progress, artifact, completion, cancellation,
and resumability semantics. A provider's internal tool execution remains
subject to its actual verified boundary, not an invented dispatch guarantee.

Move context preparation, compaction, routing, and task control behind the
shared controller/dispatcher APIs. Specify task, agent, runtime, and model
identities independently in events and UI projections. Keep old logs readable
and replay free of new inference, hooks, or external actions.

**Gate:** A small task over supplied context can use direct inference, a native
agent around a raw model, or one supported external agent, with truthful
lifecycle and capability reporting. A separate tool-using task exercises both
agent paths. Ordinary chat and tool use still pass their existing tests.
One frontend command contract drives both interactive and headless
execution; no frontend bypasses dispatch for internal model calls.

**Likely seams:** `provider.py`, `provider_litellm.py`, `catalog.py`, `loop.py`,
`dispatcher.py`, `events.py`, `fold.py`, `frontmatter.py`, `cli.py`, and the
controller introduced in M1. New modules should follow responsibility, not
mirror every row of the contract table.

### M3 — Deliver the first useful local core assistant

**Candidate list update:** retain [larger models and tooling options](../../local-model-candidates.md)
for later capacity experiments, including Qwen3-14B, Unsloth quantizations and
Hugging Face sourcing. This does not change the current cleanup/task-evidence
priority or qualify a fallback.

**NEW — the first product milestone.** Implement core-managed local readiness
and capability snapshots. Distinguish missing configuration, loading, ready,
busy, authentication failure, unreachable resource, denied capability, and
unknown status. Include observation time and recheck stale evidence before
dispatch. A failed cloud endpoint is not proof that all networking is down.

Support both an existing local server and a configured core-owned process.
Provision dependencies and weights separately from offline use. Record model
and runtime versions, real context limits, structured-output/tool support,
resource ceilings, and readiness. Start the UI independently of model loading.
Only stop processes owned by this Harness instance.

Connect project/session context and the general memory-access contract. Test
with no plugins, then with the actual local memory plugin and its normal vault.
Its environment and dependencies must also be provisioned for offline startup.
Do not assume the smaller bundled reference memory plugin proves this works.

Provide useful local conversation, project inspection, session continuation,
and at least one bounded native-agent task whose results can be checked.
Use task-sized context and tool inventories; sending full history and every
tool schema to a small model defeats the latency and reliability objective.

**Gate:** With external networking unavailable, start Harness and its configured
local runtime from stopped processes using preinstalled assets. Open a project,
resume its task, answer from accessible project records, perform the bounded
local task, cancel it, and resume without changing interface. Repeat with all
plugins disabled and with normal memory enabled. Report unavailable resources
clearly. A missing or failed local runtime still leaves the UI, records, queue,
and recovery controls operable; it cannot promise model-generated answers.

Select the initial local model/profile by measured task quality and user-visible
latency on supported hardware. Record CPU-only capability separately if tested.
Local removes API charges; RAM, energy, contention, startup, and inference time
remain costs. One model may fill multiple roles initially; do not require two
resident models before proving one useful configuration.

### M4 — Add bounded semantic agency and graceful resource changes

**NEW — decisions belong in core.** Add a small semantic-function service over
the inference contract. Start in shadow mode with three functions:

1. Interpret a submitted message: new request, continuation, correction,
   question, acknowledgement, stop request, or uncertain. Keep the distinction
   between message completion, a conversational pause, and accepted task
   completion explicit.
2. Select relevant context/memory from an already scoped candidate set.
3. Assess progress from evidence: completed artifacts/checks, remaining
   criteria, blockers, and a suggested next action. A narrative self-score
   does not establish that a task succeeded.

Each function has a versioned prompt/schema, bounded inputs/output/time,
explicit abstention, validation, and a recorded result. Raw model confidence
is not calibrated probability. Compare against deterministic rules and a
simple baseline on a frozen set of representative interactions; keep a held-out
set for promotion. Include ambiguous thanks, temporary pauses, redirects,
failed checks, stale memory, and interruptions. Weight premature completion
and misinterpreted cancellation more heavily than extra clarification. Report
quality, abstention, latency, and sample sizes together.

Set each function's error and latency thresholds before scoring held-out
results, and require the fixed critical ambiguity/stop cases to pass. If a
function fails its gate, retain deterministic behavior or keep it advisory;
do not relax the gate just to make the local model part of the control loop.

Enable only decisions that meet the declared evaluation gate. Start with
reversible routing/context suggestions, then automatic low-risk choices.
Explicit controls bypass inference. A semantic interpretation cannot grant
permissions, increase budgets, accept incomplete work, erase pending tasks,
or override explicit stop/model/runtime pins. Ambiguity preserves the task.
Keep basic agency if an individual function is disabled or times out.

Use capability snapshots plus task requirements to route work. Give short
interactive calls scheduling priority, bound background assessment, and avoid
GPU contention that makes typing or control actions wait. If the selected
runtime cannot preempt, reserve capacity or queue visibly; do not assume
priority labels can interrupt an already-running generation.

Implement fallback as a recorded task transition. Preserve artifacts and
unfinished acceptance criteria. Before moving an interrupted external-agent
task, reconcile completed and uncertain side effects; never replay the whole
assignment blindly. A new attempt re-enters dispatch and inherits authority
and budgets. Respect pins, required capabilities, data-routing restrictions,
and trust profiles. If no eligible resource exists, retain the blocked part
and continue only independent eligible work. Recovery of a resource does not
automatically reassign an active task or oscillate between providers.

**Gate:** Held-out decision evidence justifies each enabled function; ambiguous
user messages do not falsely complete tasks. Fault-injection journeys cover
network loss, expired credentials, a busy local device, local runtime failure,
and unavailable memory/MCP. The same UI retains task identity, shows where
work is happening, and never duplicates a completed action during handoff.
Replay reconstructs decisions without consulting a model again.

**Self-improvement gate:** A repeated, evidenced failure produces a versioned
candidate. Core evaluates it against the incumbent, records the result, and
applies or holds it according to adoption policy. A failing/inconclusive
candidate cannot become active. The UI exposes the evidence and the active
version; replay does not rerun experiments.

### M5 — Prove heterogeneous work and plugin interoperability

**EXTEND existing coordination; keep workflow policy in plugins.** Use the
core task/runtime contracts to combine a local native agent, a remote native
agent, and a supported external agent. Core provides supervision, permissions,
resource scheduling, and artifacts whether or not agent-swarm is installed.
Existing ensemble/panel/draft-refine/escalation strategies consume the same
contracts. Escalation must use acceptance evidence, not merely absence of an
error string or an unvalidated judge prefix.

Bound concurrency, nesting, deadlines, output, and shared cost/token budgets.
Do not deadlock coordinators by having waiting parents occupy every worker
slot. Set ownership for edits and artifacts; use isolated worktrees or explicit
write serialization when concurrent agents could change the same files.
Represent disagreement, partial success, cancelled siblings, and unresolved
work as inspectable task results.

Then exercise an actual agent-swarm workflow through supported plugin surfaces,
with normal memory contributing context and receiving an appropriate accepted
record. Specify core/plugin state reconciliation and declare any plugin step
that cannot resume. The previous native-agent study was deliberately narrower
than a workflow port; do not treat its completed gaps as full integration.
Prefer an adapter at the existing boundary before proposing any external
plugin repository refactor.

Add a portability journey: export project context, normal memory references,
task criteria/status, artifacts, and provenance in documented Markdown/JSON;
use that package from another frontend or agent runtime. Explain which native
runtime state cannot transfer. Test core restart from its records separately
from this export: raw event-log access alone is a poor exit path for a user.

**Gate:** A mixed-runtime workflow can be inspected, interrupted, resumed where
supported, and completed with correct lineage, bounded resources, and no
authority expansion. Removing either plugin leaves the core assistant useful.
Installing either independently does not create a dependency on the other.
A consumer can continue exported work without a private Harness database or
provider-specific conversation state.

**Self-improvement gate:** An agent can produce and validate an isolated source
patch through the core improvement lifecycle. Promotion obeys the configured
policy and safe activation boundary. Rollback is demonstrated, and candidate
changes to evaluation or permission controls cannot authorize their own adoption.

### M6 — Qualify daily use and the advertised support set

**CARRY FORWARD; by this milestone, usability testing must have run since M1.** Finish
multiline editing, discoverability, first-run setup, permission review, session
inspection/recovery, plain terminal operation, accessibility, and terminal-size
coverage. The normal surface needs the task, draft/queue, useful output, and
current activity. Put detailed routes, versions, budgets, and capability evidence
one action away. Show unavailable metrics as unknown, not zero.

Carry forward the old Task 18C reference budgets, measured at the compositor:

| Interaction | Initial p95 gate |
|---|---:|
| Start to focused usable composer | 1,500 ms |
| Key event to changed cell | 50 ms |
| Accepted submit to visible work state | 100 ms |
| Received output chunk to visible cell | 100 ms |
| Open an activity/command/resource surface | 200 ms warm / 500 ms cold |
| Cancel to visible cancelling state | 100 ms |
| Cancel to settled fake process tree | 3,000 ms |

Retain the original sample counts and reference-machine reporting. Measure
local cold loading, warm inference, provider wait, and Harness overhead
separately. Select and publish a local response-latency target from M3's
measured useful profile before qualifying it; UI timing alone cannot make a
slow baseline assistant acceptable. No unexplained busy state may last over
two seconds: show the known phase and elapsed time without inventing progress.

Run all six existing dogfood journeys: clean setup, a ten-turn work loop,
API/external-agent switching, mixed delegation, failure/edit/retry, and
cancellation/queue/resume. Add offline cold startup with memory, resource-loss
handoff with partial side effects, and plugin removal/portable continuation.
Require no lost or duplicated accepted prompts, zero P0/P1 usability failures,
the existing action-count gates, and a continuous 90-minute real-work session.
Include a clean-setup user who did not implement the UI.

Finish version-specific adapter conformance and real containment probes for
the advertised support set. Native agent tools and Harness-dispatched tools
have different enforcement evidence; provider transport access and tool-driven
network access remain distinct. Verify grants, redaction, environment exposure,
outward MCP authentication, repair/locking, loss-aware projections, shared
budgets, package installation, and operational diagnostics under this final
architecture. Renew evidence after changes that affect its validity.

**Gate:** The exact candidate satisfies the published support, offline,
portability, and daily-use claims. Deterministic tests, recorded fixtures,
live probes, hardware measurements, and human journeys are separately
identified. Only then execute the old candidate qualification and artifact
promotion procedures; passing tests alone does not establish this outcome.

## First implementation tranche

Begin with these reviewable units; do not start by rewriting the entire TUI or
building a general-purpose autonomous scheduler:

1. Capture the known failing behavioral cases and fix the existing lint/CI
   baseline. Draft the inference/agent contract examples and measure the local
   feasibility spike while the early defects are repaired.
2. Repair terminal facts and startup cleanup. Give compaction an audited
   internal-inference path whose output does not become an ordinary assistant
   message in the transcript.
3. Repair large-result round trips and storage integrity/recovery. Verify both
   model consumption and MCP consumption; include damaged blobs and UTF-8 tails.
4. Repair nested parentage and cumulative tool restrictions, with root-shared
   limits and a nested cancellation case.
5. Extract the smallest interaction-controller slice needed for acknowledged
   submit/queue, preserved drafts, visible states, and scoped cancellation.
   Validate its rendered interaction before broader widget changes.
6. Implement the split execution contracts with one inference adapter, the
   native loop, and one external agent. Advance directly to the M3 offline
   product journey before expanding the provider matrix.

Use focused regression tests for these behavior and integrity changes. Run the
appropriate full checks at integration boundaries; doc-only changes need
document validation and do not establish runtime qualification. Each unit should
leave working behavior available through temporary compatibility adapters.

## Crosswalk to the existing hardening backlog

**RESEQUENCED — retained detail, revised dependencies.** Early milestones take
the controls required by their actual paths; M6 completes the advertised release
matrix. This is not permission to postpone a necessary trust or data-integrity
boundary until after relying on it.

| Existing task | Disposition under this roadmap |
|---|---|
| 1 — CI/package baseline | M0; retain. |
| 2 — terminal facts/cleanup | M1; extend to agent attempts and uncertain interrupted outcomes in M2. |
| 3 — request/status contract | M2; replace the single universal completion abstraction with inference and agent contracts. |
| 4 — execution profiles | M1 for exercised paths, M6 qualification; resource substitution never weakens the effective profile. |
| 5 — compatibility/doctor | Inventory in M0, capability/readiness observations in M3, final support evidence in M6. |
| 6A — blobs/artifacts/results | M1; retain and extend to task handoff references. |
| 6B — bounded transcripts | M1/M2; distinct context preparation for inference versus external-agent tasks. |
| 7 — tool validation/risk | M1/M2; retain as the authority boundary for core and delegated actions. |
| 8 — conformance/adversarial corpus | Begin with migrated adapters in M2; add semantic evaluation in M4 and final live evidence in M6. |
| 9A — filesystem containment | Prove boundaries before affected real-work journeys; complete the support matrix in M6. |
| 9B — external CLI containment | Recast around agent runtimes in M2; qualify each runtime before advertising it. |
| 10 — storage/locking/repair | Critical repairs in M1; full restart/fault coverage before M3/M5 continuity claims. |
| 11A — redaction/environments | Apply before new persistent decision/context records and child processes; final audit in M6. |
| 11B — authenticated outward MCP | Pull into the first external-agent path that needs it; retain. |
| 12 — grants/plugin trust | Core grant guarantees before delegation; independent plugin integration in M3/M5, qualification in M6. |
| 13 — budgets/concurrency | Cumulative limits in M1/M2; local scheduling in M4; heterogeneous fan-out in M5. |
| 14 — loss-aware projections | Start with M1 activity/controller work; recovery must be complete before M3/M5 resume claims. |
| 15 — identity/onboarding/switching | Start M1; separate model, agent, runtime, and capability identity in M2/M3. |
| 16 — composer/permission review | Preserve and queue input in M1; finish multiline/accessibility work continuously through M6. |
| 17A — discoverability | Start M1; resources and live tasks use the core controller in M2–M5. |
| 17B — inspection/export/recovery | Recovery begins M1; portable handoff becomes a product gate in M5. |
| 18A — compositor/accessibility | Use from the first M1 UI change; complete the matrix in M6. |
| 18B — plain terminal | Establish shared commands in M2; prove frontend independence and finish usability in M5/M6. |
| 18C — playability | Runs from M0 baseline through every milestone; add local/offline latency and outage journeys. |
| 18D — evidence renewal | Retain in M6 and whenever a relevant adapter/runtime change invalidates evidence. |
| 19A — release evidence | Preserve deterministic evidence outputs; assemble the final bundle in M6. |
| 19B — exact candidate | M6, after the useful core and mixed-workflow gates. |
| 19C — artifact promotion | After M6 qualification, under the existing release scope. |

## Deliberate limits

Do not make an open-ended agent society, automatic model training, a workflow
language, a resident model for every role, or a daemon
with multiple simultaneous clients prerequisites for useful basic agency.
The in-process controller can leave a future transport seam without requiring
the server architecture now.

Do not move the memory or agent-swarm implementation into core to avoid
designing an integration contract. Conversely, do not put the core assistant's
availability, task control, or basic decision loop inside either plugin.
Keep the distinction between mandatory core behavior and optional policies.

Defer wider OS support and a broad provider matrix until a small advertised
set works well. Preserve current adapters through migration, but qualify them
individually. Self-improvement is in scope through the lifecycle above; a
model's assessment of itself is not its grading authority.

## Design lineage and source map

- [June 10 architecture](../../../../vault/10-projects/harness/specs/2026-06-10-harness-design.md): anti-lock-in, event spine, per-call models, plugin-level workflows. This file lives outside this checkout; its relative link assumes the current sibling `vault` project layout.
- [August native-agent study](../specs/2026-08-31-harness-native-agents-design-study.md): intentionally limited native gaps, not a full workflow port.
- [September 3 hardening plan](2026-09-03-production-beta-hardening.md): detailed correctness, trust, UI, measurement, and qualification backlog.
- Core seams: [provider contract](../../../src/harness/provider.py), [catalog dispatch](../../../src/harness/provider_litellm.py), [agent loop](../../../src/harness/loop.py), [dispatcher](../../../src/harness/dispatcher.py), [subagents](../../../src/harness/subagent.py), [mixtures](../../../src/harness/mixture.py), [routing](../../../src/harness/routing.py), [TUI](../../../src/harness/tui.py), and [local serving script](../../../scripts/serve-local.sh).
- External integrations inspected for this proposal: `/home/fearsidhe/.claude/plugins/memory` (`README.md`, reader/writer/provider contracts) and `/home/fearsidhe/.claude/plugins/agent-swarm` (workflow engine, in-memory workflow server, and persistent datastore). They remain separate repositories and are not modified by this roadmap.
