# Resident task continuity

Harness now prepares configured project and memory context before a task's first
model call. The model does not have to remember to request those sources. Each
new root attempt retrieves them again, under normal tool permissions, limits and
cancellation. Memory and agent-swarm remain plugins; the core owns this workflow.

## Configure and use

Add sources to a context profile, for example `resident.toml`:

```toml
history_turns = 2
max_input_bytes = 32768
tools = ["read_file", "write_file", "mcp__memory__memory_list"]

[[sources]]
id = "project-facts"
tool = "read_file"
args = { file_path = "FACTS.json" }
required = true
max_bytes = 4096

[[sources]]
id = "normal-memory"
tool = "mcp__memory__memory_list"
args = { subject = "harness" }
required = false
max_bytes = 8192
timeout_seconds = 5
```

Use an existing catalog alias and separately configured memory MCP server:

```sh
harness --catalog models.toml --model local-small --context-profile resident.toml
```

In the interface:

```text
/task new Update the project result
/task require Review the resulting file
Use the current project facts to update the result.
/status
```

`/status` joins the selected task and unresolved requirements, context-source
results for its latest attempt, and local runtime snapshots. It does not probe
or start a runtime. `/resources check ALIAS` remains the explicit refresh action;
normal local inference starts an owned runtime only when its existing profile
permits it. A ready inventory is still not model-quality evidence.

Escape interrupts context retrieval, local loading or generation through the
existing cancellation path. An unsent draft stays in the composer and queued
follow-ups pause. On resume, the task and source profile survive. The next user
prompt fetches fresh context and includes a concise record of the previous
attempt's execution, stop reason and unresolved requirements. Prior acceptance
is historical; new work still requires fresh checks and review. An interrupted
operation is never automatically replayed during session recovery.

For inspection without a provider:

```sh
harness status SESSION_ID
```

This reads saved records without repairing or modifying the session. Local
readiness observations are explicitly stale. A source left fetching after a
crash is reported as completion unconfirmed, not as usable context.

## Boundaries

Sources are explicit, repeatable tool queries. Choose read operations appropriate
to the workspace. Core does not infer whether a plugin tool is read-only: a
configured source can have side effects if its tool does. Configuration does not
grant permission. The effective tool registry, argument validation, rewrites,
permission prompts and cumulative root tool-call budget all apply. A source
runs again for a new user attempt, so interrupted queries must be safe to repeat
or the user must inspect their recorded effects first.

There are at most four uniquely named sources. Each has at most 8 KiB of JSON
arguments, a positive timeout of at most 30 seconds (default 5), and a result cap
of at most 16 KiB (default 4 KiB). Retrieval also consumes the task deadline. The
combined pinned context, current turn and tool schemas must fit the task/profile
input limit; a source cap is not a reserved portion of that budget.

Optional sources report unavailable on denial, absence, tool error, timeout or
oversized/unreadable output. They supply an explicit notice rather than invented
facts. Required-source failure stops before inference. Oversized output is not
silently truncated, and an oversized sidecar is rejected before materialization.
Storage or journal failures remain fatal. Ready means bounded bytes were obtained;
it does not establish their correctness, relevance or freshness in the underlying
store. Source content is labeled as tool data, not trusted instructions.

Source results remain in the existing session blob sidecar. Typed
`ContextSourceObserved` facts retain source, run, call and policy provenance.
Normal tool facts retain effective arguments and enforcement decisions. Core
source calls use a distinct purpose so replay cannot create orphan conversation
tool results. Sources run once per root attempt, including external-agent entry;
the conversation bridge does not retrieve twice, and child scopes do not fetch
implicitly. Internal semantic calls and compaction do not fetch sources.

The profile follows existing override/clear/resume rules. No memory store,
provider fallback, model install, semantic task acceptance or improvement
activation is added. These observations are usable experiment evidence, not an
automatic score or permission to adopt a candidate.

## Reproduce the offline pilot

`scripts/check_resident_workflow.py` uses the existing pinned 4B weights, a
loopback-only container capped at 4 GiB RAM, no swap and four CPUs, and the normal
memory plugin/vault mounted read-only. Follow the mount and image recipe in
[local model qualification](local-model-qualification.md#reproduce-on-this-linuxwsl-cuda-setup),
replacing its final script invocation with:

```sh
-B -m scripts.check_resident_workflow --memory-root "$qualification_memory_root" \
  --output /reports/resident-workflow.json
```

It checks an exact public project artifact, real streaming cancellation, draft
preservation, task/profile continuity across a fresh kernel, fresh normal-memory
retrieval, and composed status/answer visibility. It exports only checks, timings,
byte counts and hashes. Temporary logs and private memory text are deleted.
This is a narrow automated workflow pilot, not a held-out quality evaluation or
human usability qualification.

## Observed result — September 7

The [completed pilot](handoffs/2026-09-07-resident-workflow/resident-workflow.json)
**failed overall**. Both real project reads and normal-memory index retrievals
completed before inference, including after restart. Context/status display,
real stream cancellation (123 ms), an unsent draft, unresolved task requirements
and restart continuity passed. The resumed answer contained the correct public
project facts and was visible in the final compositor (6.0 seconds).

The initial write task returned after 10.3 seconds without a valid RESULT.json.
After the two successful configured queries, its only model-selected tool call
was a failing file read. Its answer also omitted the expected facts. The
retrieved memory index was 13,802 bytes; these observations do not isolate why
the model failed. Tool selection and relevance need separate measurement before
claiming a useful write workflow. No retry or changed criterion converted this
failure to a pass.

The [initial probe report](handoffs/2026-09-07-resident-workflow/resident-workflow-initial.json)
retains the same artifact/answer failures and a probe `AttributeError` that
prevented cancellation and resume measurement. Correcting that probe method
allowed the complete journey above; it did not change model settings, prompts
or artifact checks. Neither run qualifies a model or activates a fallback.
