# Retrospective source-runner demonstration

[report.json](report.json) records a real code comparison through the new core
source-improvement runner. It compares PR56 before its judge-failure review fix
(`957b8fb0e363ea81921c845a73cecb0826239e7a`) against the corrected source
(`6f5fa416245cb62853473aed86c5ba9f0676c45b`). The report retains the exact check
scripts, gates, paired measurements, source hashes and artifact references.

Checked selection passed on both revisions. Native judge failure lost passing
work on the incumbent and preserved it as incomplete on the candidate. The
candidate also preserved child settlement and explicit task acceptance. All four
check invocations completed; the paired verdict passed. Durations were roughly
1.08–1.22 seconds per invocation on a shared host running the broad test suites;
they are observations, not a performance qualification.

This is a known historical regression with an operator-declared validation
partition, not a secret held-out trial. The scripts freeze the earlier test
support code outside both snapshots and use deterministic fake providers. No
local or remote model calls occurred. No new patch generation, autonomous
improvement discovery, code promotion, rollback or daily-use gate is claimed.

The complete core journal and verified blobs were retained locally at
`.worktrees/tmp/source-demonstration/sessions/judge-recovery-v2/`. The first
development run, before the Git discovery ceiling, remains under `judge-recovery/`.
This report describes the repeated comparison with the corrected runner. The committed
report is a compact derived record: it omits raw failure tracebacks and temporary
machine paths, while retaining the original report's digest and size. The fixed
scripts can be reused in a `SourceProposal` with these two revisions and the
recorded gates/configuration; select new evidence IDs in the destination session.
Use the current evaluator to prepare a new plan, as described in
[source improvement](../../source-improvement.md).
