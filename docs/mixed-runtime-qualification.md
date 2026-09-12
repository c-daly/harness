# Live mixed-runtime workflow

`scripts/qualify_mixed_workflow.py` exercises a configured local inference model,
a remote inference model and the typed Codex agent adapter in one core task
tree. It uses a public retry-budget fixture and an independent exact artifact
check. Execution completion alone cannot pass the check.

The operator driver is deterministic. Each participant is a real model/agent;
there is no model making the orchestration decisions. This is an opt-in
development journey, not a capability certification or a semantic benchmark.
Memory and agent-swarm are not installed into the test kernel.

## Run

Use an existing catalog with three distinct aliases: a local profile, a remote
inference route and a `backend = "codex"` route. The selected profiles and
installed CLI must already work. The command uses their normal credentials and
may start the local runtime if its catalog profile enables automatic startup.
It makes live remote calls. It does not edit the catalog, install runtimes or
download weights.

```sh
PYTHONPATH=src:. uv run python -m scripts.qualify_mixed_workflow \
  --catalog "$HOME/.config/harness/models.toml" \
  --local local --remote gpt --external codex \
  --seconds 180 --output /path/to/new/mixed-workflow-run
```

The output path must not exist. Set `TMPDIR` to a directory with free space if
the system temporary filesystem is full. `--codex-binary` can select an installed
CLI. Per-phase time is configurable from 30 to 600 seconds. The fixed shared
limits are 24 model admissions, 32 tool admissions, eight descendants, depth
three, three active agents and one active coordinator. A coordinator consumes
one descendant admission. Native request time is at most 120 seconds and
conversation output is bounded to 4,096 tokens per response. These elapsed-time
caps are not hang detection. Reported token/cost accounting is observed usage,
not a prepaid reservation or a guarantee about provider-internal work.

## What it does

1. Creates a new project containing only `CASE.json`. Three independently scoped
   participants read it and write separate `local.json`, `remote.json` and
   `external.json` assessments through the native file tools. The rule excludes
   successful jobs and failed jobs with no attempts remaining.
2. Pauses the external agent's first actual `read_file` invocation after core
   dispatch/permission checks, exposing a named core wait. The operator requests
   cancellation of that typed runtime. This is a controlled tool wait, not an
   inferred stall or proof of cancellation at every possible provider phase.
3. Lets the native siblings settle and retains their files and child output
   blobs. Records the incomplete coordination, runtime lineage, targeted stop,
   partial usage and admission counts. It measures target settlement separately
   from completion of the whole ensemble.
4. Closes the source kernel and its owned local runtime. Reopens the same root
   session and checks exact task, policy, limits and accounting restoration,
   with no restored live work. Inspects fixture effects and starts a fresh
   external attempt in a new child session. It does not resume Codex's private
   conversation state or rerun the completed native participants.
5. Checks exact file contents, successful reads/writes tied to the child records,
   ancestor lineage, settled runtime state, preservation of native outputs and
   the unchanged fixture. Exports both coordinations and the stop record using
   the ordinary portable task exporter. Operator review remains unresolved.

Current core ensembles enforce the task's declared requirements. This journey
declares a human-review requirement, so even the fully executed external retry
now leaves the aggregate and root attempt incomplete. The driver records the
unverified child check and requires that review hold. Its independent fixture
oracle still requires correct files and complete participant executions; the
hold does not excuse an incorrect or missing artifact. This updated behavior is
covered by scripted integration tests, without another live-model measurement.

`report.json` contains metadata, explicit booleans, source hashes and per-phase
results. Exit zero requires every obligation, including correct native results.
Exit one also covers a fully executed journey whose model answer is incorrect.
Failures preserve the report, sessions and partial project files under the output
directory. `continuation.zip` is produced when the journey reaches export. The
command refuses to overwrite an earlier run.

Reports omit endpoint URLs, startup commands, credentials, raw provider errors
and response text. Session logs, artifacts and provider scratch can contain
additional data; they are local diagnostic material and are not automatically
published. The checked-in evidence below contains metadata and public fixture
observations only.

## Explicit ownership in continuation packages

This journey exposed an export gap: direct calls to core coordination have no
dispatch tool-call ID, so their reports were missing from the continuation
package. New `coordination_started`, `coordination_finished` and
`subagent_spawned` records include an optional `agent_run_id`. The exporter uses
that recorded owner, or a known owning tool call, to select a task's operations.
Exported coordination and child-reference rows expose it as `run_id`.

Temporal overlap does not establish ownership. Unowned work that happens during
another task is excluded. Conflicting tool/run ownership, operations outside
their recorded run, duplicate terminals and changed coordination ownership are
refused. A blocked coordination without a start retains its direct outcome;
an owned start without a terminal remains unconfirmed.

Old tool-owned records still export without the new field. Old direct records
with neither an owning tool call nor an explicit run ID remain inspectable in
the session log but are omitted from task export. The exporter does not guess
their ownership from timing. Child session contents remain references, not a
recursive database copy.

## September 11 measurement

The [final report](handoffs/2026-09-11-mixed-runtime/report.json) measures the
source committed as `48f4453`, identified by per-file hashes. It predates PR55's
export-ownership review correction and checked ensemble selection. Those changes
have automated regression coverage, without a repeat live-model measurement.
The [profile](handoffs/2026-09-11-mixed-runtime/profile.json)
records the installed Qwen3-8B Q4_K_M weights, llama.cpp 9603, RTX 5070,
`openai/gpt-5` route and Codex CLI 0.154.0.

**The workflow did not qualify: 23 of 24 checks passed.** The local model wrote
`{"retryable":["harbor","mesa","ridge"],"remaining_attempts":6}`. The correct
answer excludes `ridge`, whose three attempts exhaust its limit of three, and
has four remaining attempts. Its native read returned the complete fixture.
The remote and fresh external attempts wrote the correct answer.

The targeted external stop settled in **0.178 seconds** at the controlled tool
wait. The incomplete ensemble retained both native artifacts. Restart restored
the task, context, limits and accounting exactly, and only the external member
ran again. Both coordination reports and the stop record survived portable
export. All live work settled; operator review remained unresolved. The test's
owned local server was stopped afterward.

Final cumulative admissions were **nine model calls, nine tool calls and six
descendants**. One cancelled usage attempt remained explicitly unknown across
restart. The fresh Codex attempt reported **64,401 input tokens** for this small
fixture: external-agent accounting includes work beyond the Harness prompt.
The report's observed amounts are not a complete token total or bill.

[Development observations](handoffs/2026-09-11-mixed-runtime/development-attempts.json)
retain all four attempts. The local answer was wrong in all four; no prompt or
fixture tuning was used to get a passing result. The first driver version also
incorrectly applied an inference-only setting to Codex and called startup on an
already resumed session. Later attempts exposed and fixed the missing direct
coordination export, then tightened child-reference ownership. Only the final
attempt uses the complete initial PR55 implementation. These repeated development attempts
are not independent held-out qualification trials.

The practical implication is to keep acceptance checks outside the solver.
The installed 8B profile's earlier success copying project facts does not
establish reliable rule classification or justify resident-agent decision
authority. No model capability flag, semantic prompt or adoption policy changed.

## Limits and next work

This tests headless core contracts and one controlled interruption point. It
does not qualify the TUI, human daily use, native Codex effects outside Harness
tools, total provider cost, arbitrary cancellation phases, process-crash recovery,
plugin reconciliation or isolated source improvement. Codex's built-in tools
remain provider-controlled; its empty scratch directory and read-only sandbox
do not provide arbitrary-path read confinement. The test directs it to Harness
tools and verifies those tool results, without claiming broader confinement.

The final artifact oracle belongs to this public test driver. It does not add
an automatic validator to ordinary ensemble voting. A correct remote answer
and a completed local turn cannot establish that the local answer was correct.
M5 still needs useful workflows with task-specific acceptance evidence, the
installed plugins and governed source-patch evaluation/promotion/rollback.
