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
fields; old logs remain readable.

Fan-out permits at most 16 experts (plus an optional ensemble judge). Draft/refine
uses at most two entries and escalation at most three. Existing shared execution
and child limits still apply. Cancellation or an unexpected coordinator failure
cancels and awaits outstanding siblings before publishing the aggregate terminal
fact. Reports and returned aggregate text are each bounded to 1 MiB. Delivered
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
artifacts are not recursively exported. Report absence does not prove that no
coordination ran: a crash before its terminal event still needs inspection of
the existing tool/child facts.

The regression suite covers real child lifecycles, partial results, bounds,
cancellation, provenance, read-only CLI/TUI inspection and task export. A
controlled catalog test mixes native inference with the real external Codex
binding using scripted transports. This establishes contract behavior, not live
provider, installed-plugin or local-model qualification. Coordinators still
consume existing child capacity; separate admission, shared token/cost budgets,
edit ownership, plugin reconciliation and isolated improvement patches/rollback
remain ahead. Memory and agent-swarm remain plugins.
