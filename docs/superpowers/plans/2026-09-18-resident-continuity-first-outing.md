# First outing: a resident that carries the working relationship forward

Date: 2026-09-18

Status: implementation and evaluation plan. An initial C1a startup correction is
implemented: native resident instructions, configured default model,
explicit no-model state and optional default context profile. The existing MCP
connection to agent-swarm was also repaired. These changes do not complete C1a or
the remaining integration/capture work. See the
[startup validation receipt](../../handoffs/2026-09-18-resident-startup/README.md).

## Outcome

Harness starts into Saoirse, its resident agent. This is the default entry point
for every user-facing conversational interface, not an optional resident mode,
plugin command or persona that the user must select. Choosing a model changes the
resident's inference resource; starting a conversation creates a scoped session
with that resident. Neither operation creates a new resident identity.

After a restart or a day away, the user can say “continue with Harness” and Saoirse
can establish what was agreed, why it was agreed, what actually happened, what is
still open, and the next appropriate action. A correction made yesterday changes
today's behaviour. Switching projects does not mix their state. Changing the
underlying model preserves the relationship's recorded history and commitments.

This should feel like continuing work with the same collaborator. Repeating a
personal profile, producing a convincing recap, or successfully retrieving bytes
does not by itself meet that objective. The resident must use the information in
subsequent decisions and complete useful work.

The system must remain comfortable to use alongside the user's normal desktop
applications. GPU/RAM split inference is allowed. Session Desk's input-budget
repair is a separate effort; this plan covers the resident and plugin integration
that its interface consumes.

## Existing foundation and findings checked for this plan

Use the [core-agency roadmap](2026-09-06-core-agency-roadmap.md),
[resident workflow](../../resident-workflow.md), and
[Saoirse ownership direction](../../saoirse.md). Preserve the existing constellation
reader/writer contracts, provider selection and optional presence-registered
integrations. Do not create bespoke memory bridges where those contracts exist.

| Component | Existing foundation | First-outing integration question |
|---|---|---|
| Harness | Task/run records, session replay, explicit context acquisition before root attempts, dispatch enforcement, cancellation, handoffs and improvement records | Which native resident responsibilities are already implemented, and which need extending so all interfaces share the same resident lifecycle? |
| Memory | User/feedback/project/reference observations, provider-backed storage, locking, scoped retrieval, bounded search/read and a session recorder | Can Harness use the existing writer and recorder with an injected local inference runner and visible write outcomes? |
| Continuity | Resume briefs, observations, relevance ranking, decisions, narratives, insight writes and capture triggers | Are the right records surfaced, are stale claims identified, and are outcomes actually captured? |
| Project management | Existing constellation design assigns project/task lifecycle to `pm`; Harness already owns execution records | Locate and verify the actual current PM implementation and interfaces; map work-item IDs to Harness task/run IDs without a second task authority. |
| Experiment | Reader/writer run and observation stores plus the read/plan/work/eval/journal/decide workflow in the installed agent-swarm code; a Harness plugin design already exists | Expose and use the existing experiment facilities through Harness for this trial; verify the actual loaded implementation. |
| Session Desk | Uses a real Harness kernel today, with its own worker and session location | Make a sequential frontend handoff preserve resident/session state; shared attachment remains an explicit implementation gap. |

Specific findings that affect scope:

- The normal memory plugin under `~/.claude/plugins/memory` differs from Harness's
  bundled example flat-store plugin. The normal provider/vault is the continuity
  source to integrate; a second empty store would give a misleading result.
- Memory's README understates current capture support: its hook configuration
  includes PreCompact and SessionEnd recording. Its existing recorder accepts
  injected `runner` and `writer` functions. The shell hook currently defaults to
  Claude-backed recording and hides failures. Adapt that seam for Harness;
  do not build another recorder or quietly require a cloud/CLI agent offline.
- Continuity's capture hook currently asks the agent to record an insight and
  update the narrative. Emitting that request is not evidence of a durable write.
- `resume_brief` already ranks observations and records surfacing in a relevance
  sidecar. Its current synthesis line counts observation types; it is not proof
  of rich cross-project reasoning. Its time windows and bounded excerpts can
  omit old but still binding decisions.
- The installed Harness `projects` plugin provides discovery and current status.
  It is not evidence that the separate PM lifecycle implementation is installed.
- A design for the experiment plugin is not evidence that the running Harness
  has loaded it. Inventory source, manifest, registered tools and real calls.

These are checked integration facts and open questions, not a replacement design
for the user's existing plugins. Implementation must recheck installed revisions.

### How the actual code runs

The resident runs in Harness. Memory and continuity are independent plugins called
through their supported MCP/CLI interfaces, using their existing provider code and
stores. Their routine retrieval and capture do not require an agent-swarm workflow.
Memory's existing recorder receives a Harness-backed inference runner instead of
launching its default Claude process; its writer remains the configured writer.

Experiment is also its own plugin. The
[approved experiment design](../specs/2026-09-01-experiment-plugin-design.md)
explicitly separates that plugin from the agent-swarm repository where the earlier
implementation was parked. C0 must first locate any implementation of that design.
Use it if present; otherwise adapt the existing experiment store, evaluation and
phase-transition code as specified there. Preserve the tool contracts and record
formats, with Harness dispatch hooks enforcing the phase rules. Do not introduce
the old agent-swarm daemon/router as a dependency of resident continuity.

Workflow orchestration and workers are a separate capability from these services.
Any delegation in the outing goes through the supported Harness execution path
and records the actual worker backend. A subprocess launched outside Harness is
not evidence that Harness's resident or orchestration completed the task.

## Scope and ownership

This effort has two implementation deliverables: native resident support in
Harness, and integration of the existing plugins with that support. Exposing
plugin tools or supplying a resident prompt alone does not complete the first.
The resident must have a core lifecycle that uses those capabilities dependably.

Harness core owns resident identity, lifecycle triggers, context preparation,
task execution, capture status and recovery. Plugins own their readers/writers,
storage semantics, synthesis and project/experiment policies. Invoke them through
their supported interfaces and ordinary dispatch. Hooks remain thin triggers.

| Native Harness responsibility | What the core must provide | Plugin contribution |
|---|---|---|
| Default startup | Every conversational entry point enters the resident lifecycle, restores its identity and resolves the current session/project before the first model turn | Register available services during startup; no plugin is required to select or instantiate the resident |
| Resident identity and state | Stable resident identity across scoped conversations, versioned configuration, project/session bindings and model changes | Personal observations, project knowledge and richer continuity narratives |
| Resident control and capabilities | Discover available models, tools, sources and interfaces; decide when to retrieve, act, clarify or wait under normal policy | Advertised services and domain-specific workflows |
| Context lifecycle | Prepare context automatically on start, resume and project change; preserve provenance, binding constraints and known omissions within the request budget | Retrieval, ranking and synthesis over plugin-owned records |
| Commitments and recovery | Persist task obligations, action receipts, continuation state and pending capture; resume without duplicating acknowledged work | Link existing PM items and reconcile writes through the authoritative writer |
| Capture lifecycle | Trigger capture at useful boundaries, schedule it, track acknowledgements and recover incomplete capture | Observation extraction, record semantics and durable memory writes |
| Local operation and responsiveness | Select configured inference resources, schedule small helper calls, handle uncertainty and cancellation, and report unavailable capabilities | Optional specialised classifiers, retrieval backends and other services |
| Improvement lifecycle | Connect observed failures and user corrections to existing improvement records, evaluation, versioning and rollback | Memory preserves feedback; experiment provides richer trials and comparisons |
| Shared interface contract | Offer the same resident operations, progress and state to terminal, headless and desktop clients | Optional interface-specific actions; no separate resident per frontend |

Use the existing resident, task, event, continuation, resource and improvement
mechanisms first. Identify missing behaviour with evidence before adding a new
abstraction. Core owns when these operations happen and their observable status;
each plugin retains ownership of its data and specialised behaviour.

The minimum core remains useful without memory, continuity, experiment or
agent-swarm installed: resident configuration, local session/task history and
explicit continuation remain available through native mechanisms. Rich memory
retrieval and synthesis require their configured plugins and must be reported
unavailable when absent. A required missing capability blocks the dependent
operation clearly. Do not create a competing semantic-memory store as a fallback.

Offline operation uses a configured, available local model. Small helper models
can assist with request classification, context selection and capture, while core
policy retains control over permissions and task completion. A classifier's guess
that the user is done cannot by itself mark the work complete. If no suitable
model is available, preserve the state and explain the limitation.

### Startup contract

All user-facing conversational entry points use the same core startup contract:
load or initialise resident identity/configuration; resume or create the explicit
session and project scope; restore task and pending-capture state; establish
available models, tools and plugin services; prepare the appropriate context;
then accept work as that resident. Show startup progress where acquisition takes
time. Missing optional services produce a clear degraded state; they do not
silently open a generic model chat instead. A missing required service blocks the
operation that needs it, while the resident can still expose status and recovery
actions. If no inference model is available, the interface reports that state
without pretending that the resident can answer.

A fresh conversation retains resident identity and access to scoped durable
knowledge; it need not replay every old conversation. Session selection, project
selection and model selection remain distinct operations. Session Desk connects
through this same contract, with its own presentation and optional UI actions.
Background worker roles remain explicit children of resident work rather than
accidentally starting independent copies of the user's resident.

Keep a small, versioned resident configuration for name, working style and
capability bindings. Keep evolving personal observations in normal memory and
project decisions in their existing authoritative records. Reuse core task/run
events for execution truth and link them to PM work items. A model/provider change
is a resource change, not a new resident identity or an implicit permission grant.

One resident and one active frontend at a time are sufficient for this outing.
Concurrent desktop/terminal attachment, a new daemon, a general plugin rewrite,
and weight training are not prerequisites. Preserve session IDs, logs, blobs,
project paths, provider identity and existing records during sequential handoff.

## Implementation sequence

Each slice should leave a reviewable change and a short validation receipt.
Commit and publish slices when authorised; preserve unrelated dirty work.

| ID | Work | Depends on | Completion evidence |
|---|---|---|---|
| C0 | Audit native resident responsibilities alongside actual plugin revisions, stores, contracts, capture hooks, PM state and experiment entry points. Bind a Harness checkout and model/runtime profile. | — | A map of implemented and missing core behaviour, real plugin read/write/read-back controls in isolated stores, and explicit unavailable capabilities. |
| C1a | Make the resident the default conversational startup path and establish its native lifecycle/capability contracts using existing identity/configuration, task/event, context, resource and continuation mechanisms. Define capture status and shared client operations. | C0 | Launching Harness enters the resident without an opt-in command. A Harness-only resident retains identity, task state and action receipts across restart with optional plugins absent; missing services are reported accurately. |
| C1b | Bind normal memory, continuity and experiment to that lifecycle through Harness's existing plugin/MCP facilities. Connect the actual PM provider if present, preserving ID ownership. | C0, C1a | Real dispatched calls, correct provider/store attribution, offline reads and plugin-absence behaviour. |
| C2 | Connect automatic opening context to one resident configuration: relevant personal feedback, project resume brief, active task/requirements and last verified outcome. Refresh on project change and resume. | C1a–C1b | A new process chooses the appropriate next action without a manually supplied recap. Context provenance and omissions are inspectable. |
| C3 | Connect native capture scheduling and recovery to existing plugin writers/recorder and Harness inference. Capture decisions, corrections and task outcomes at meaningful boundaries, before compaction, and at orderly shutdown. | C1a–C2 | Read-back proves persistence; a crash between writing and acknowledging does not duplicate a record or lose a pending write. |
| C4 | Handle conflicting/stale records, project ambiguity, missing providers and failed capture. Add a concise way to inspect and correct what the resident is carrying forward. | C2–C3 | Controlled cases retain user decisions, refresh mutable facts and visibly report missing context without inventing it. |
| C5 | Exercise restart, model change, local-only operation and sequential frontend handoff using the same resident records. Coordinate the Session Desk seam with its separate work. | C2–C4 | Identity, commitments and exact project targeting survive; no competing session writer or duplicate action. |
| C6 | Run the controlled comparisons and five-session real-work outing through Harness, recording them with experiment and linking failures/corrections to existing improvement records. | C1a–C5 | Case-level results, useful work artifacts, user experience notes, resource measurements and evidence-linked improvement candidates. |

C0 must name the actual PM and experiment implementations before assigning their
integration work. If PM is unavailable, existing Harness tasks permit a clearly
labelled partial pilot; do not build a substitute PM engine or report full PM
integration. If the experiment interface needs adapting, reuse its existing
stores/workflow rather than replacing it with an unrelated benchmark service.

### Opening context and retrieval

1. Resolve the selected project, workspace, task and session explicitly. Ask only
   when the destination is genuinely ambiguous; a transient viewer selection is
   not automatically a durable user decision.
2. Load a compact personal/feedback brief and a targeted continuity brief, linked
   to original records. Include binding decisions even if they are old; recency
   ranking alone must not determine whether a constraint still applies.
3. Supply active obligations and verified run/artifact evidence. Keep proposed,
   attempted, observed and accepted outcomes distinct.
4. Retrieve further source pages when needed through existing bounded readers.
   Report missing coverage. Respect both request-byte limits and the model's
   actual token window, reserving reply space; bytes are not a token estimate.
5. Verify mutable checkout/process facts when they affect an action. Historical
   conversation text cannot establish that a process is running or a file changed.

Show a small source/capture status on demand using existing Harness status and
event surfaces. Normal conversation should not start with a machinery dump or
make the user manage retrieval manually.

### Capture and correction

Persist explicit user decisions and corrections with project/personal scope,
source session/turn references and dates. Record model interpretations separately
from user statements. Store actual results with tool/artifact references and keep
unfinished commitments open. Do not convert a single frustrated interaction into
a durable psychological label about the user.

Use the existing writer's metadata/versioning/supersession facilities. Where a
facility is missing, add the smallest compatible extension in its owner. Avoid
copying entire transcripts into several competing stores.

Harness should record capture intent and its receipt. A stable source identifier
allows reconciliation after a crash; a retry must not create duplicate insights.
Use an atomic writer-side uniqueness check where supported. If the writer cannot
provide idempotency, reconcile uncertain writes through its reader under a single
capture writer; keep unresolved outcomes pending instead of blindly retrying.
Failed writes stay visibly pending or failed. “Remembered”
must mean the appropriate store acknowledged a record that can be read back.

Use the existing recorder's injectable runner to call the configured Harness
inference path, with normal scheduling and cancellation. Preserve an evidence
checkpoint if summarisation cannot finish; shutdown capture is not the only
opportunity to remember important work. Low-confidence interpretations can remain
proposals without making the user approve every ordinary observation.

## The first real outing

Use one existing Harness reliability issue as the primary work item and a second
real project for a scoped, initially read-only visit. Choose the issue from the
current backlog at C0 and freeze its acceptance criteria before the trial.
Initially hold the resident model and instructions constant. The current 9B
profile is a runnable candidate, not a fully qualified agent; include its required
template-compatibility patch in the version record if selected.

Five sessions over roughly two or three days is a useful initial target. Session
boundaries follow real work and interruptions, rather than an arbitrary execution
allowance. The user should interact normally and should not have to construct
evaluation prompts or repeatedly reconstruct yesterday's context.

| Episode | User experience | Evidence we need |
|---|---|---|
| 1. Establish and work | Discuss the real issue, make a consequential decision, complete part of the work and leave a next step. | Decision/rationale, task state and actual artifact change recorded; no planned work reported as completed. |
| 2. Return after restart | Say “continue with Harness” without supplying a recap. | Correct workspace, unresolved obligation and next action recovered; useful work advances without repeating settled questions. |
| 3. Correct and switch projects | Correct an approach or preference, visit the second project, then return. | Correction affects the next relevant action; project-specific facts and global preferences keep the right scope. |
| 4. Cross a resource boundary | Resume with another configured model; separately exercise local-only operation. | Shared history remains usable, source gaps are visible, and familiar working conventions persist despite model differences. |
| 5. Recover and finish | Interrupt work, resume through the available frontend path, and finish or accurately report the remaining blocker. | No duplicated action or fabricated completion; the final artifact and task record agree; the user can inspect the continuing state. |

The normal user-facing path should include Session Desk once its independent
budget repair and required resident handoff seam are ready. Run the same knowledge
checks through Harness alone as well. A Harness-only pass remains explicitly
partial if desktop continuity has not been exercised.

Include one bounded delegated task if delegation is part of the selected real
issue. The resident must retain ownership of the commitment and distinguish a
worker's claim from verified completion. It is not necessary to construct a swarm
merely to increase the pilot's complexity.

## Experiment design and acceptance

Use the existing experiment project/workflow to register the question, frozen
cases, source/model versions, observations and results. Resident decisions and
plugin calls must execute through Harness. External scripts may drive inputs and
inspect results; they must not perform the resident's reasoning or task work on
its behalf. Scripted providers are suitable for protocol tests only.

Compare the current resident behaviour with the integrated lifecycle using the
same model, instructions except for the declared change, context limits, plugin
availability and starting project/history snapshots. Separate copies of memory,
vault, task state and continuity's relevance sidecar prevent one arm's retrievals
or writes from improving the other arm. Controlled failures use isolated copies;
ordinary real-work sessions use the configured normal stores under normal policy.

Freeze the controlled checks before execution. Keep the developer-visible smoke
cases separate from fresh continuation cases; do not train on the acceptance set.
For each failure, trace: record existed → source was retrieved → content reached
the request → model used it correctly → resulting action was verified.

| Check | First-outing acceptance |
|---|---|
| Default startup | Each supported conversational entry point starts or reconnects to the resident without a resident-mode flag. Fresh conversations and model changes preserve resident identity while respecting session/project scope. Optional-service failures do not fall back to a separate generic assistant. |
| Native resident support | With optional plugins and Session Desk absent, Harness retains resident identity, local task/session state and action receipts across restart. Missing plugin capabilities remain explicit; plugin integration then adds the same services across supported interfaces. |
| Continuation | Every declared restart case recovers the correct project, open commitment and next action without a supplied recap. |
| Consequential memory | At least one earlier decision and one user correction demonstrably change a later action, supported by the stored source and resulting behaviour. |
| Freshness | A changed project fact supersedes an old observation with provenance; a later proposal does not supersede an executed fact. |
| Attribution and ambiguity | Sources support the claims attached to them; ambiguous project targets trigger clarification before action. |
| Capture/recovery | Capture acknowledgements survive process restart; interruption does not silently lose a recorded commitment or duplicate a write/action. |
| Scope and authority | No cross-project write, invented user approval, false completed-work claim or authority gained from recalled text in the controlled cases. |
| Useful work | The chosen real issue advances through a verifiable artifact and meets its original acceptance criteria, or the outing remains incomplete with its blocker preserved. |
| User experience | The user reports whether context had to be re-explained, corrections repeated, or the assistant felt unfamiliar after each boundary; at least one resumed session must be judged a natural continuation. |
| Responsiveness | Normal desktop interaction remains acceptable during reading, capture and local inference; resource measurements support, rather than replace, the user's judgement. |
| Improvement continuity | An observed failure or correction links to a durable improvement record and a later behavioural check; proposed model/configuration changes retain the existing evaluation and rollback requirements. |

Report all case outcomes; a favourable average cannot erase a wrong-project action
or false completion. Subjective familiarity and objective task continuity are
separate results. This small outing can justify further daily use, not a general
reliability claim or an automatic model/prompt promotion.

Measure memory-retrieval and capture latency, first-response and completion time,
available RAM, CPU/GPU load, page-in/swap activity and desktop interaction against
an idle/normal-work baseline. With the user's usual apps open, identify numerical
resource/headroom limits before heavy trials and record them in the protocol.
Existing swap occupancy alone is not proof of active thrashing. Do not raise the
WSL memory allocation or occupy all GPU memory simply to make a candidate fit.
CPU/GPU placement and lower-priority capture are implementation choices to test.

## Deliverables and next decision

The outing produces an integration map; small core/plugin adapter changes with
regression checks; a working resident configuration; real cross-session work
artifacts; an experiment record with failures intact; and a short report separating
storage, retrieval, context selection, model behaviour and interface defects.

Use those results to choose the next change. Missing records call for capture
repair; omitted records call for retrieval/context repair; correct supplied
evidence followed by repeated behavioural errors makes prompting or fine-tuning
a plausible next experiment. Curate verified corrections and successful traces
as potential training examples, preserving provenance and a separate evaluation
set. Any later tuned model is compared against the same plugin-backed baseline,
with retention checks and rollback through existing improvement mechanisms.

Do not make fine-tuning a prerequisite for experiencing continuity. The first
outing should establish whether the existing constellation, reliably used by
Harness, already supplies the continuing collaborator the user wants.
