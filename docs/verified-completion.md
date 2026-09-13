# Verified task continuation

Task completion is now the next qualification milestone. A useful result must
survive independent checks; a worker's final response and its own test report
are execution evidence. They do not finish the operator's task.

The core `CompletionService` supervises one task using an operator-supplied
`CompletionPlan`, attempt executor, independent verifier and workspace identity
function. Queue selection, decomposition and provider preferences remain host
or plugin policy. The service is not registered as an agent tool.

The host freezes the original task, check IDs, grading configuration, maximum
attempts and deadline before execution. Harness records a baseline check, runs
a native child, checks the resulting workspace and supplies unresolved findings
to the next child. All children retain the same root execution and usage
accounting. Partial files remain in the worktree; no checkpoint commit, reset,
cleanup, merge or permission renewal happens between attempts.

Every attempt and check result is retained in the session journal. Check output
is a verified blob, with its return code, elapsed time and bounded stdout/stderr.
`checks_passed` means every declared check passed on one recorded workspace
version. It does not emit `TaskAccepted`, confirm review requirements, publish
code, or establish that the contract covered every aspect of the user's goal.
This distinction is necessary when a broad coding task also requires source
review, complete test suites, packaging and a pull request.

## Native agent-swarm host

`plugins/agent-swarm-runner/run_verified.py` uses the installed agent-swarm
plugin to select its first eligible request and record ownership. It invokes
Harness's native `SubagentRunner.run_result`, the same child runtime used by
`dispatch_agent`, without spending an inference call on a coordinator whose
only job would be to dispatch a predetermined request.

```sh
PYTHONPATH=src:. python plugins/agent-swarm-runner/run_verified.py \
  --plugin-root /path/to/agent-swarm \
  --manifest /path/to/manifest.yaml \
  --state-dir /path/to/orchestration-state \
  --output /path/to/new-evidence-directory \
  --catalog /path/to/models.toml --model gpt \
  --checks /path/to/operator-checks.json \
  --max-attempts 6 --max-model-calls 160 --timeout 1800
```

The check file contains explicit commands and pinned verifier files outside the
candidate worktree. Check subprocesses receive an explicit environment, not the
host's API keys or unrelated environment variables. For example:

```json
{
  "environment": {"PYTHONPATH": "src:."},
  "files": {"/absolute/operator/check.py": "<actual SHA-256>"},
  "checks": [{
    "id": "behavior",
    "description": "The documented behavior works",
    "argv": ["/absolute/venv/bin/python", "/absolute/operator/check.py"],
    "timeout_seconds": 120
  }]
}
```

The initial candidate must be clean and on the plugin's declared branch. The
host pins the selected native inference route. It keeps queue completion
withheld even after checks pass, so the operator can finish scope and release
review before releasing dependent tasks. There is no automatic provider
escalation or second queue scheduler. The experimental binding still lacks the
Claude router's identities and hooks, normal memory integration and TUI parity.

## Stop and resume behavior

`--pause-after 1` records a checkpoint after one attempt and its checks. Reuse
the same options and evidence directory with `--resume` to continue. The host
requires the same plugin owner, grading contract, route, limits and workspace
bytes. Previously consumed root counts, attempts and the original deadline
remain in force. Verification runs again before any new work.

Cancellation, an expired deadline, exhausted attempts, a failed verifier or a
worker stopped by its budget leaves an explicit stop state. The service does
not replenish limits or infer a hang from a quiet log. A crashed invocation
with an unresolved attempt requires effect reconciliation before another
attempt; safe checkpoint resumption does not claim automatic crash recovery.

The command verifier bounds output and execution time and reaps its process
group on timeout, excess output and cancellation. The native file tools retain
their workspace checks. Bash, verifier commands and candidate code still run
with operator authority: these controls are not an OS sandbox. File pins detect
changed verifier inputs; they do not freeze the Python interpreter and all its
dependencies. The Git workspace digest covers HEAD and tracked/nonignored file
names, modes and bytes, not ignored caches. Symlinks and submodules are refused
by this identity function.

## Qualification targets

The next release gate is useful task completion with few operator interventions:

- A partial final answer triggers independent rejection and continuation.
- Every declared check passes together; a regression cannot inherit an old pass.
- Attempts, failures, costs and preserved effects remain inspectable.
- Restarting a checkpoint retains the original allowance and checks current work.
- Verification trouble and uncertain interrupted effects remain unresolved.
- Full scope review and repository validation agree with the reported result.

Scripted transport tests establish these runtime mechanisms. Live task evidence
is recorded separately under `docs/handoffs/2026-09-13-verified-completion/`.
Neither category alone qualifies sustained mixed-provider work, offline memory
continuity, resident-led self-improvement or interface usability.
