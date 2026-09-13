# Native Harness execution and review recovery

The user explicitly approved the configured GPT route receiving the relevant
agent-swarm instructions and Harness source, plan and workflow files. Three
subsequent attempts used **Harness** for both coordinator inference and native
`dispatch_agent` execution. The stopped direct Claude session was not resumed.

The [experimental binding](../../../plugins/agent-swarm-runner/README.md)
asks the installed plugin's manifest runner for eligible requests and runs the
first one. The plugin selected task 9, session integrity and lifecycle tooling,
from the existing queue. Native tools ran in that task's existing worktree.
Harness recorded the child relationship, tool effects, model attempts, limits,
usage, and terminal outcomes. Core imports no agent-swarm code.

This binding covers the manifest runner and a native implementer definition.
It does not port the Claude plugin's router identities, Serena protocol or
enforcement hooks. Memory, offline fallback, other providers and the TUI were
not exercised in these GPT runs. The outer operator authored and ran review
checks and reported failures to the plugin; this is supervised integration
evidence, not unattended operation or resident-led self-improvement.

## Observed attempts

Exact identities, source hashes, journal hashes, limits, usage and terminals are
in [native-evidence.json](native-evidence.json). Complete journals and blobs
remain in `.worktrees/tmp/harness-swarm-native-v1`, `-v2` and `-v3`.

| Attempt | Actual child limits | Observed result |
| --- | --- | --- |
| v1 | 20 iterations, 4,096 output tokens per response | Child ended `incomplete/max_tokens`; no file changes. |
| v2 | 48 iterations, 16,384 output tokens per response | Child completed 36 model turns and produced code; 23 focused tests passed, but independent review found three failing checks and four Ruff errors. |
| v3 | Same limits as v2 | Plugin retry corrected the three reproduced defects; unchanged operator checks passed, as did the 23 focused tests and Ruff. The complete manifest task remains unfinished. |

The first run's launch record called the context profile's 8,192-token setting
`response_output_token_cap`. That was not the effective cap: the recorded child
task and actual response show the smaller 4,096-token limit. The later driver
records the context cap and child configuration separately. The v1 driver was
formatted after launch; v2 and v3 record hashes of their loaded driver and
subagent runtime, which were unchanged between those runs.

This exposed and motivated the core change: direct agent definitions now accept
validated `TaskLimits`. The child loop uses those configured iterations and
passes the limits to inference, including children with recorded requirements.
Context limits and the parent's shared execution and usage budgets still apply;
unspecified agents retain their defaults. A larger context profile alone still
cannot raise a smaller explicit task cap.

## Independent review mattered

The v2 worker's own tests passed while `harness sessions --help` crashed because
its CLI target did not define `main`. Its repair authorization also hashed
findings without binding the underlying session bytes. Changing session content
or a torn tail without changing the error offset could reuse stale authority.

The frozen [operator checks](candidate_review.py) reproduced all three cases:
**3 failed in 1.32s**. They were not changed for the retry and subsequently
reported **3 passed in 2.92s**. Independent reruns of the worker's focused tests
reported **23 passed in 0.29s**, and Ruff passed. The rejected candidate is
preserved at task-branch commit `73717b577eaeb6329854f4f753c78652267f8956`.
The corrected partial candidate is checkpointed at `4cf2819`; it is clean,
unaccepted and unpublished. Neither candidate is part of the host-fix branch.

Other observed limitations remain:

- The v2 worker wrote implementation before tests despite the plugin's stated
  test-first protocol, and omitted Ruff. The native binding does not enforce
  those Claude-specific hooks. The retry ran Ruff, but did not itself invoke
  the frozen operator checks; the operator verified them independently.
- Both successful executions returned with substantive manifest requirements
  unimplemented, before exhausting their configured iteration budgets. The
  worker described the omissions, and Harness retained `acceptance=unverified`.
  A terminal answer is insufficient to release dependent tasks.
- Oversized reads and an empty path caused recoverable tool failures. A
  headless permission refusal was rendered as "denied by user" even though no
  person answered that request; the journal retains the resolver evidence.

## Preserved queue and next boundary

No task completion was written by this driver. Task 9 still requires storage
limits, complete lifecycle operations and their independent checks, additional
integrity coverage, session-listing changes and documentation. Its candidate
remains in the existing task worktree, separate from the Harness host fix.
Earlier unfinished worktrees and PRs 59–61 are preserved.

The plugin state records the most recent owning Harness root as `worker_id`.
Those recorded `spawned`/`active` fields do not prove that a process is running;
the three attempts have ended. Inspect the evidence and preserve/checkpoint the
candidate before another retry. Do not restart the original direct Claude host,
reset dirty work, silently grant missing router capabilities, automatically
accept the task or merge branches.

The next workflow increment needs requirement-specific acceptance checks and
continuation of partial work, then the remaining native router/memory bindings.
Broader dispatch has not yet been qualified by this one task.

## Host-fix validation

The Harness host branch (not the unfinished task-9 branch) passed:

- Python 3.13.15 complete suite: `2415 passed, 7 skipped, 6 warnings in 618.17s`.
- Python 3.12.14 complete suite: `2415 passed, 7 skipped, 6 warnings in 616.80s`.
- Repository Ruff and `git diff --check`.
- Offline wheel/source-distribution build and installed-wheel smoke, including
  the console entrypoint and all packaged modules.

Validation logs and hashes are retained in `native-evidence.json`. Runtime and
test sources were frozen during those suites. The six warnings are the existing
MCP client's deprecation warnings. Initial scripted binding checks stalled on
an async file worker under the restricted execution sandbox; the same wiring
passed outside it, as did the complete suites. This does not qualify the
generated task-9 implementation or the full agent-swarm plugin.
