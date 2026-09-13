# Reliability integration checkpoint

The user requested a new integration branch, a committed checkpoint of current
work, and a development branch from that checkpoint, with reviews deferred.

`integration/harness-reliability` combines the existing source-authorship and
native agent-swarm/verified-continuation history with the PR60 plugin workflow
reconciliation and PR61 external agent bindings branches. Existing PR branches
and original worktrees remain intact. This is an integration checkpoint, not a
release or task acceptance.

It also retains task9 session lifecycle work at `4cf2819` plus the uncommitted
files left by the four native Harness attempts in completion-live-v1 (root
`877e64566f234c459bffbd7722ddba68`). The last independent behavior check had
5 passing and 2 failing tests; the fourth attempt was interrupted before its
final independent check. Task9 is unfinished. Repair handling, real advisory
locking, and conflict-safe lifecycle operations still need attention.

The continuation implementation at `410d8c8` passed 2436 tests with 7 skipped and
6 warnings on both Python 3.12 and 3.13. That evidence predates this integration
and does not qualify the combined tree or task9. The live trial also predates
several final host fixes; its exact runtime source is retained separately.

Next branch: `feat/progress-aware-completion`. Make internal thresholds progress
checkpoints rather than implicit task deadlines; retain explicit user limits.
Supply interface requirements in worker-readable context. Continue real work
through Harness with agent-swarm owning queue selection. Preserve partial work
and independent checks. Record lightweight evidence, without a new productivity
gate while this mechanism is still under development.

Outstanding reviews remain deferred. Known follow-ups include errored-tool
workflow-reference handling in PR60 and formatting in PR61. No PR is merged or
closed by this checkpoint, and no original workflow task is marked complete.
