# Task requirements and completion evidence

Harness can retain an explicit objective and its requirements across prompts,
model changes, compaction and session resume. An agent returning an answer leaves
those requirements unresolved. Recorded checks and explicit user review supply
separate evidence; user acceptance is a separate event.

This is core functionality. It works with all plugins disabled. The model's
`todo` list, `TaskOutcome` scores and semantic observations cannot change it.
Memory and agent-swarm retain their own implementations and state.

## Using it

In the TUI:

```text
/task new Update the parser and verify the behavior
/task require The quoted-field regression is covered
/task require Error messages are useful
Implement the change and run the relevant tests.
/task
/task confirm r1 I inspected the regression and its test output
/task confirm r2 I reviewed the error messages
/task accept Reviewed the change and its evidence
```

`/task new` selects the task but does not submit work. Subsequent ordinary prompts
belong to that task; the composer and prompt queue keep their existing behavior.
The compact task indicator shows unresolved counts and execution state. `/task`
shows every requirement, its evidence and the acceptance note. `/task help`
lists commands.

Use `/task list` to inspect saved tasks, `/task use ID` to select one (a unique
prefix works), and `/task off` to detach it without erasing its obligations.
Task changes are refused while work or queued prompts could be affected. Listing
and inspection remain available during work. A new session has its own tasks;
`/resume` restores the selected task of the reopened session.

Requirements are explicit and additive in this slice. Existing requirements
cannot be overwritten, silently removed or waived. Correcting the objective or
its criteria currently means creating a new task and retaining the old record.
There is no automatic task creation or language-based approval interpretation.

## Deterministic checks

`/task require TEXT` adds a requirement for user review. Advanced callers can
instead supply a typed requirement with `/task require-json JSON` or the core
API. `/task check` evaluates those definitions against recorded evidence.

| Check kind | What it establishes |
|---|---|
| `review` (default) | A user explicitly confirmed this requirement, with a note, for the current attempt. It is an attestation, not a machine check. |
| `output` | The latest root attempt's stored output artifact is intact and its bytes match a predeclared SHA-256 digest. |
| `tool_result` | The latest matching call in that attempt was dispatched with the exact effective tool name and JSON arguments, completed without a Harness error flag, and its recorded result bytes match a predeclared SHA-256 digest. |

For example, this requirement expects a `verify_fixture` tool to return exactly
the bytes `PASS`, with no newline:

```python
import hashlib

kernel.tasks.add_requirement({
    "id": "fixture",
    "description": "The fixed fixture returns its expected result",
    "check": {
        "kind": "tool_result",
        "tool": "verify_fixture",
        "args": {"target": "fixture"},
        "sha256": hashlib.sha256(b"PASS").hexdigest(),
    },
})
```

The tool must already exist and execute through the usual dispatcher, permissions
and budgets. Defining a check grants no tools and executes nothing. Expected
arguments are the effective arguments after hooks, including canonicalized
paths; JSON types and extra keys matter. Expected result bytes are the stored,
post-redaction tool output. A tool returning text such as “tests passed” only
establishes that exact recorded response. In particular, native `bash` represents
nonzero exit codes in its text, so a missing `is_error` flag alone is insufficient.
The expected result digest is mandatory.

Every automatic requirement must be declared before the checked root attempt.
Checks inspect that attempt and its same-session runtime descendants only.
Other tasks, previous attempts and child-session delegation results do not
implicitly satisfy it. Among matching calls, the latest proposal wins; an older
success cannot hide its later failed or interrupted retry.
Reused call IDs, duplicate resolutions/results and evidence outside a run's
start/finish boundary are unverified rather than attributed to a guessed source.

Evidence cites the terminal event sequence, call ID where applicable, artifact
reference where applicable, and observed digest. Missing, corrupt or oversized
artifacts remain unverified. Checks read at most 1 MiB per requirement, with at
most 32 requirements per task and at most 8 KiB of JSON arguments per tool check.
Limit failures remain visible. No model inference or network access is needed.

These checks describe **recorded evidence**, not the current workspace. They do
not inspect arbitrary files, rerun shell commands, prove test adequacy, or validate
the truth of a tool's output. File-content assertions, structured exit-status
evidence, richer predicates and cross-session evidence imports need separate
contracts. Manual review remains available for criteria that cannot be expressed
by these initial exact checks.

## Acceptance, freshness and recovery

A new attempt invalidates earlier checks, user confirmations and acceptance.
Adding a requirement also invalidates them; automatic checks cannot be declared
after observing an attempt and then used to certify that same attempt. The full
history stays in the log. Compaction changes conversational context, not task
records. Crash repair marks an interrupted run aborted and retains the unresolved
requirements without replaying side effects.

`/task confirm ID NOTE` applies only to a user-review requirement after a completed
execution. It cannot override a failed automatic check. `/task accept NOTE`
requires a completed, settled execution, at least one requirement, and no
unresolved requirements. It rereads checked artifacts before recording acceptance.
Neither all-passing checks nor “thanks” from the user implies acceptance.

Acceptance is a historical user decision about the recorded attempt. Editing a
file outside that attempt does not rewrite the event or certify the changed file.
Further work in the task creates another attempt and requires fresh evidence.
`AgentResult.acceptance` remains `unverified`: an execution result never upgrades
its own historical meaning because a user later accepted the task.

## Core and headless interfaces

`Kernel.tasks` exposes `TaskService`: `create`, `select`, `add_requirement`,
`prepare`, `check`, `confirm`, `accept`, `selected` and `state`. Start the session
before mutating it. `prepare(prompt, context=...)` returns an `AgentTask` using
the selected durable ID and complete current criteria. Run it through the native
loop or a bound external runtime as usual:

```python
kernel.tasks.create("Produce the expected result")
kernel.tasks.add_requirement({"id": "review", "description": "Review the result"})
result = await kernel.loop.run_task(kernel.tasks.prepare("Do the work"))
state = kernel.tasks.check()
assert result.acceptance == "unverified"
assert state.unresolved == ("review",)
```

The execution boundary rejects a tracked task with mismatched criteria or an
already-running attempt. Untracked `AgentTask` callers keep their existing
semantics. A headless `harness --resume SESSION -p 'continue'` uses the selected
task through the same preparation service. For read-only inspection:

```bash
harness tasks SESSION --base-dir /path/to/harness-data
harness tasks SESSION --task TASK_ID
```

Six additive event types own this state: `TaskCreated`, `TaskSelected`,
`TaskRequirementAdded`, `TaskChecked`, `TaskRequirementConfirmed` and
`TaskAccepted`. `fold(...).tasks` reconstructs it. These events do not add model
messages or change plugin records. Core improvement `Evidence` can cite their
session/sequence IDs as `task_outcome` evidence using the existing journal;
they cannot activate an improvement candidate or change its evaluation gates.

The live task view is seeded from replay once, then updated after each successful
log append. Status and prompt preparation use independent snapshots of that view;
they do not reread the entire history on every input or bus event. Checks still
read the log and verify referenced artifact bytes. A failed log write cannot
publish a task-state change.

The service is an explicit caller API, not an agent-visible tool. As with other
core event APIs, trusted Python callers can append events; these records are not
a security boundary against arbitrary code running inside Harness.

## Validation scope

Deterministic tests cover evidence scope and integrity, rewrites, later failed
calls, invalidation, input bounds, terminal outcomes, replay, compaction and
headless continuation. TUI journeys inspect the final compositor for unresolved
counts, review/acceptance, restart, cancellation, queued work and preserved drafts.
This qualifies the tested contract, not local-model quality or the complete
resident workflow. Broader usability, task revisions and automatic requirement
proposals remain future work; proposals must not become their own approval.

The [saved synthetic probe](handoffs/2026-09-07-task-evidence/probe.json) records
source hashes, state checkpoints and five warm preparation samples after 5,000
additional events. The samples were **0.030–0.159 ms** on this run; this small
probe is not a p95 or whole-interface latency qualification. Before the live view
was introduced, a development probe rereading 5,004 events took 49.00–65.40 ms
per preparation. The regression test also directly forbids repeated history reads.

Rendered checkpoints show [pending review](handoffs/2026-09-07-task-evidence/unresolved.svg),
[user acceptance](handoffs/2026-09-07-task-evidence/accepted.svg), and
[changed work with preserved draft](handoffs/2026-09-07-task-evidence/changed.svg).
The inputs, provider and review actions in this probe are scripted public fixtures;
they are not a human dogfood result or a real model-quality assessment. Reproduce
the journey and export the final compositor with:

```bash
uv run python -m scripts.check_task_workflow --output /tmp/task-probe-new
```

The output directory must not already exist. The script uses temporary sessions
and preserves the report and rendered screens in the specified output directory.
