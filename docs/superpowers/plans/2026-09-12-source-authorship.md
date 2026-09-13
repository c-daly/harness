# Bounded source authorship

PR58 merged at `4c09803`. Complete the missing proposal step with a core native
agent task driven by a selected inference model. This increment makes one bounded
proposal; it does not add an unrestricted coding shell or unattended activation.

## Contract

- An operator supplies a committed incumbent, selected evidence, a request,
  explicit editable text paths (including declared additions), and a fixed
  `SourceSuite`. Validate and freeze these before invoking a model.
- Record a typed `SourceAuthoring` improvement intent. Execute through the
  existing `AgentTask`/dispatcher contracts, shared counts, permissions, local
  scheduling, context sources, cancellation and durable run recovery.
- Send selected source text and evidence observations as data. The model cannot
  select the incumbent, edit scope, evidence IDs, evaluation scripts/gates, or
  adoption policy. No tools are advertised. Normal configured context retrieval
  remains core-controlled, with its existing permissions and provenance.
- Validate one structured text patch (whole-file replacements, additions,
  deletions), reject duplicate/out-of-scope/empty edits, and preserve executable
  modes. Binary files remain unchanged. Content-derived candidate revisions
  explicitly distinguish authored snapshots from committed Git revisions.
- Publish an immutable candidate and plan bound to the intent and completed
  task output. If interrupted publication leaves only a completed response,
  explicit finalization reconstructs the same artifacts without another model
  call. Failed/cancelled/aborted tasks cannot be finalized. Replay never calls
  models or executes source.
- Candidate source is immutable blob data. Evaluation and selected-source launch
  already materialize independent filesystem copies. The running installation
  and original dirty/untracked work stay untouched.
- CLI/TUI expose authoring, inspection and finalization. Evaluation, adoption,
  rollback and launch continue through the existing operator controls.

## Test-first tasks

1. Write failing tests for the full author/evaluate/adopt/rollback path,
   preserved source and task state, no grading inputs/tools in the author call,
   edit confinement and pre-call input validation.
2. Implement typed intent, exact response/candidate/plan provenance and idempotent
   finalization. Test malformed output, provider/permission/budget failures,
   cancellation cleanup, torn publication, restart, tampered provenance, and
   old/mangled event parsing.
3. Add CLI/TUI controls, normal context acquisition and inspection. Exercise a
   live inference model on a small source defect if locally available; retain
   the full request/outcome and fixed paired checks, including failed attempts.
4. Review authority and failure boundaries; run Ruff, complete Python 3.12 and
   3.13 suites, packaging and installed-wheel smoke with frozen source/tests.

## Limits

Single-request authorship is not iterative repository exploration. The supplied
file scope may omit necessary context. Authorship success means a valid proposed
patch, not improvement; only subsequent fixed checks measure it. Held-out labels
do not prove secrecy, including against information already present in source or
normal memory. No security sandbox, dependency installation, automatic evaluation
or promotion, external coding-agent qualification, or M5/M6 completion is claimed.

## Implementation and observed corrections

The implementation uses a typed `SourceAuthoring` improvement record and the
existing agent run lifecycle. Candidate records optionally link that intent;
the journal derives their exact expected patch from the completed task response
and rejects altered claims or grading. Publication is idempotent by intent ID.
Existing source schemas add a defaulted `origin` field, distinguishing authored
content revisions from Git commits. Both committed repositories and stored
incumbent snapshots can seed the next proposal.

The first 13 tests failed because the author module did not exist. After
implementation, the affected source/prompt set passed 103 cases. Expanded
recovery/context coverage reproduced the missing authored-incumbent input;
implementing it produced 40 passing authoring tests. A further regression showed
that serializing default task limits before binding converted the default 600
seconds into an explicit cap. The fix retains whether the timeout was explicitly
provided, so session configuration governs omitted values. The rendered terminal
test prompted shorter source-control lines; its assertion now normalizes screen
line wrapping. Both final time/UI cases pass. There are 42 new authoring cases.

Live measurements use three operator-seeded maintenance defects and two existing
local GGUF profiles. The first run and a diagnostic repeat are retained separately;
the latter captures bounded raw response text to explain structured-output
failures. No model-specific answer, grading relaxation or prompt retuning was
introduced in response to those outcomes. Complete supported-version suites,
packaging and final measurement reconciliation are recorded in the progress log.

The first complete runs finished with **2,400 passed, 7 skipped, 6 warnings on
Python 3.12.14 (829.78 s)** and **2,399 passed, 1 failed, 7 skipped, 6 warnings on
3.13.15 (828.43 s)**. The failure was
`test_invalid_grants_leave_timer_and_log_unchanged[nan]` at the `parked` fixture's
three-second member-entry wait, before its grant assertion. A retained probe
delaying member entry by 3.2 seconds reproduced that exact `TimeoutError`.

The fixture now awaits member readiness or the actual bounded task ending and
joins both futures on cleanup. Production deadlines and grant behavior are
unchanged. The coordinator/authoring/probe set then passed **76 tests in 17.95 s**.
The application sources still match the diagnostic local measurement; both
complete suites are repeated with the corrected fixture and a new freeze.
