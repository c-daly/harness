# Inspect delegated work

Coordinated agents share the root session's [usage stop limits](usage-budgets.md).
Use `/budget` to inspect their cumulative accounting, including failed attempts.

Use `/coordination` in the terminal or inspect a saved session without starting
a model or loading plugins:

```sh
harness coordination SESSION_ID --base-dir ~/.local/share/harness
```

The view lists each saved ensemble, panel, draft/refine or escalation result:
execution status, selection policy, disagreement, participant status, child
session/run references and the saved report/answer hashes. Inspection reads the
log and verifies report bytes; it neither repairs the source nor executes work.
`/panel` continues to show activity. A configured coordination agent's partial
result remains incomplete even when its tool transport returns successfully.
Direct ensemble, panel and escalation calls show a separate aggregate result row
in the Workflows group, alongside each child's own status. Completed participants
can therefore appear beside an incomplete result when review fails or the
aggregate answer is truncated. A blocked aggregate is visible even without children.
Admitted runs also record their start. If no aggregate terminal was saved,
inspection and export show completion as unconfirmed. In the activity panel,
the aggregate remains running until its tool settles or is recovered, then shows
unconfirmed if its terminal result is missing.

Core coordinates through `SubagentRunner.run_result()` and
`run_strategy_result()`. Their `DelegationResult` distinguishes `completed`,
`incomplete`, `failed`, `blocked` and `cancelled`. It carries delivered text,
reason, child session/run and original output references, truncation, and optional
coordination report provenance. `acceptance` is always `unverified`.

The existing tools and string APIs remain compatible. Their error prose is for
display; coordination does not parse it to determine execution status. An answer
that literally contains `[subagent error]` is ordinary answer data. A child that
completed but exceeded its configured delivery length is marked truncated and
cannot supply a complete vote, review, draft or verifier result. Its original
bounded task output stays in the child store for inspection.

Each settled coordination records a `coordination_finished` event and a version-1
report in the calling session's blob store. Reports preserve roles, requested
model aliases/agent definitions, child terminal status and references, aggregate
answer, selection policy, disagreement and unresolved participant outcomes. The
child log identifies its effective routed model/runtime. Child output references
belong to that child's store; the aggregate answer and report belong to the
calling session. The report loader validates its schema, hash and association
with the session/event. Existing child-terminal events gain optional provenance
fields; old logs remain readable. New reports also record whether coordination
was admitted and its overall deadline. Older reports leave those fields unknown.

Fan-out permits at most 16 experts (plus an optional ensemble judge). Draft/refine
uses at most two entries and escalation at most three. `ExecutionLimits` defaults
to 16 active workers and, separately, 16 active pure coordinators. A coordinator
waiting for its experts therefore leaves all worker slots available. Direct
strategy tools and configured coordination agents each consume one level of
depth and one cumulative descendant reservation, sharing the existing limits of
four levels and 128 descendants with workers. Admission is rejected when capacity
is exhausted; there is no waiting queue. Ordinary agents still occupy worker
slots while awaiting their own tool calls.

Each admitted coordination has a default 600-second overall deadline, spanning
all its stages, including optional judges, critics, refiners and premium fallback.
An operator can [extend a live coordinator](execution-controls.md#extend-a-live-coordinator)
through `/execution extend-coordinator`, independently of root and member timers.
The root logs each grant intent; the target's settled report records the applied
total timeout. Inspection retains grant records without treating them as live
timers or proof of completion. An unconfirmed start shows its initial deadline.
Expiry returns `incomplete` with reason `coordination deadline`. Cancellation,
deadline expiry or unexpected failure cancels and awaits outstanding siblings
before publishing the aggregate terminal fact; completed participant outputs stay
inspectable. Cancellation is cooperative, so cleanup may extend beyond the
deadline. An earlier ancestor deadline still cancels its descendants. Active
coordinators also prevent idle-only handoff and improvement operations. Handoffs
retain the stricter source limits and hold unreconciled descendant activity.

Native `write_file` and `edit_file` calls serialize mutations of the same
canonical path within the owning event loop. A cancelled or timed-out worker
retains that file lock and its enclosing tool/task/child capacity until the file
thread finishes. Repeated cancellation cannot release the lock early; a call
still waiting for the lock can cancel without starting its mutation. Different
paths remain independent. The terminal explains a wait for a started file change,
and the tool/run still records cancellation even if that change finishes.

Native reads also remember a SHA-256 digest of the exact file bytes for the
calling session. Sharing a registry does not let a parent or sibling reuse
another agent's observation. Writes and edits freeze their expected version
before waiting for the lock, then compare current bytes while holding it. A
changed file produces a tool error asking the agent to reread and reapply its
change. The rejected call leaves the file untouched. This includes changes
outside a displayed read window, or deletion before a planned overwrite.

A successful delivered write/edit supplies that caller's next version, so
sequential changes can proceed without extra reads. Concurrent calls cannot
silently adopt a version written while they were waiting. Cancelled I/O does
not refresh observations, even if a started write finishes. A missing-file read
clears that caller's old observation, allowing deliberate recreation; other
failed reads do not refresh it. Conflict guidance appears in the normal terminal
tool result, and rereading uses the existing tools and permissions.

After restart, saved read paths remain routing hints, but existing files must
be read again before mutation. Numbered, windowed or truncated historical tool
output cannot establish the original file bytes. These are content checks, so
a timestamp-only change or replacement with identical bytes does not conflict.

This is per-call serialization and stale-content detection for cooperating
native tools on the owning event loop, not isolation of a whole workflow.
Provider-native tools, shell commands and other processes do not take these
locks. Their earlier changes can be detected, but a change between the content
check and replacement is not an atomic cross-process conflict check. Mixed
workflows still need explicit edit ownership or isolated worktrees.

Reports and returned aggregate text are each bounded to 1 MiB. Delivered
aggregate truncation is explicit and incomplete.

Ensembles exclude incomplete, failed, blocked or truncated answers from voting
and synthesis. When a sibling is unusable, any remaining answer is delivered as
incomplete with the successful work retained. Disagreement is recorded even when
all executions complete. A panel requires complete affirmative reviews from each
configured critic; the first line must be exactly `APPROVE`. With no critics,
the proposal is explicitly recorded as unreviewed. An optional escalation verifier similarly uses
an exact `PASS` first line. A successful premium fallback can recover execution
while earlier failed participants remain in the report.

Escalation automatically uses the declared requirements of the task owning the
actual calling run. It freezes that task's objective, requirements and source
session/task/run/basis before starting a participant. Each cheap/premium child
records those requirements before execution and receives them in its task context.
Core then rechecks the child's immutable output/tool records, including matching
arguments, successful terminal facts, current-run boundaries and artifact hashes.
The report records `recorded_checks` as its selection policy. A passing cheap
result avoids premium execution. A failed or unverified cheap result triggers
premium, which must pass the same checks; an unsuccessful premium result remains
incomplete with its answer and evidence retained. Truncated/incomplete executions
cannot pass even when their recorded bytes match.

The optional verifier remains advisory: it can request premium after a passing
cheap check, but `PASS` cannot override failed evidence. Existing requirements
always apply; a model cannot remove them by supplying tool arguments. The
`require_checks: true` argument additionally blocks before spawning children if
there is no active task with declared requirements. Configured escalation agents
can set the same boolean in frontmatter. Core provides this behavior with both
memory and agent-swarm absent; plugin workflow policy can invoke it normally.

For a small reproducible terminal example, declare an exact-output requirement:

```text
/task new Return exactly READY
/task require-json {"id":"answer","description":"Return READY without extra text","check":{"kind":"output","sha256":"c2e3ac47f4a325469c1a2d5f117e463ec943c721986d5d9f09ac4540b7d80526"}}
```

Then ask the agent to call `escalate` with the prompt `Return exactly READY`, your
catalog's `cheap` and `premium` aliases, and `require_checks: true`. `/tools escalate`
shows the full parameters. `/coordination` shows the frozen task source and each
participant's passed/failed/unverified checks, with child event/artifact references.
An interrupted verifier remains unconfirmed; a late read-only check cannot revive
an interrupted coordinator. Inspection and replay do not rerun the checks or models.

These checks establish matching recorded evidence, not general answer quality or
current workspace correctness. A `tool_result` check does not itself execute a
test. User-review requirements remain unverified and cannot be satisfied by a
model opinion; a workflow with only review requirements cannot automatically pass.
Selection evidence does not accept the parent task or automatically import child
evidence into `/task check`; the parent's existing evidence rules and explicit
operator acceptance still apply. Ordinary delegated subtasks do not implicitly
inherit a parent's requirements: the gate uses the tracked task owning the actual
calling run. With no such requirements and `require_checks` omitted/false,
escalation retains its legacy execution/advisory policy. Agreement, synthesis,
`APPROVE` and `PASS` alone do not verify user acceptance.

[Portable export](portable-continuation.md) includes coordination reports attached
to the exported task's tool calls and copies their aggregate answer artifacts.
Checked reports also retain the frozen requirements/source and participant grades.
Member output/report references remain source-session references; child logs and
artifacts are not recursively exported. A saved start without a terminal travels
as an unconfirmed entry without a report or aggregate output. Earlier logs may
have no start fact, so report absence alone does not prove that no coordination
ran; existing tool/child facts still need inspection.

The regression suite covers real child lifecycles, partial results, bounds,
cancellation, provenance, read-only CLI/TUI inspection and task export. A
controlled catalog test mixes native inference with the real external Codex
binding using scripted transports. This establishes contract behavior, not live
provider, installed-plugin or local-model qualification. Shared token/cost
budgets, broader edit ownership, plugin reconciliation and isolated improvement
patches/rollback remain ahead. Memory and agent-swarm remain plugins.
