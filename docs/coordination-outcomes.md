# Inspect delegated work

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
Expiry returns `incomplete` with reason `coordination deadline`. Cancellation,
deadline expiry or unexpected failure cancels and awaits outstanding siblings
before publishing the aggregate terminal fact; completed participant outputs stay
inspectable. Cancellation is cooperative, so cleanup may extend beyond the
deadline. An earlier ancestor deadline still cancels its descendants. Active
coordinators also prevent idle-only handoff and improvement operations. Handoffs
retain the stricter source limits and hold unreconciled descendant activity.

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

These are advisory model-review policies. Without a verifier, escalation checks
execution status and truncation only. Neither agreement, synthesis, `APPROVE`,
nor `PASS` is evidence that the user's acceptance criteria have been met. Tracked
requirements, checks and explicit operator acceptance remain separate. Replacing
the advisory selection policy with evidence-based escalation is still an M5 gate.

[Portable export](portable-continuation.md) includes coordination reports attached
to the exported task's tool calls and copies their aggregate answer artifacts.
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
budgets, edit ownership, plugin reconciliation and isolated improvement
patches/rollback remain ahead. Memory and agent-swarm remain plugins.
