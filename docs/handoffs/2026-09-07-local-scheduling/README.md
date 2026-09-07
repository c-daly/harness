# M4 local scheduling evidence

This slice starts from PR23's merge (`f8b7c1c`) on `feat/local-scheduling`.
The [configuration and behavior](../../local-scheduling.md) describe its
session-tree boundary. M4 remains in progress.

## Real scheduling gate

The first [scheduling run](scheduling-before-cleanup-guard.json) passed both fixed
`m4-local-scheduling-v1` journeys: plugins absent and normal memory enabled.
Two aliases use the same pinned Qwen3-8B Q4_K_M weights on different local
ports in the default device group. This tests process residency and admission,
not differing model capabilities. The inference streams, MCP retrieval,
native writes and terminal-compositor checks are real.

In each journey a real work stream holds the group. Another work request and
the user's project task queue behind it. A semantic assessment records `busy`.
The user cancels the queued task while retaining an unsent draft, then explicitly
resubmits it. Cancelling the original stream lets the interactive request run
before the queued work. Harness stops the idle owned runtime before loading
the selected alias. The completed task writes RESULT.json exactly once, retains
its unresolved review criterion, and does not mark itself accepted. All model,
tool, agent and local-request records settle, and owned processes stop on exit.

| Plugins | First real stream seconds | Queue cancellation seconds | Stream cancellation seconds | Project write seconds | Background abstention ms |
|---|---:|---:|---:|---:|---:|
| Absent | 17.054 | 0.021 | 0.010 | 15.169 | 4.744 |
| Normal memory | 12.764 | 0.023 | 0.013 | 17.080 | 6.232 |

Fixed before the first run: first stream <=35s, project task <=45s,
cancellation <=2s, UI mount <=3s, busy background abstention <=250ms.
UI mounts took 0.056s and 0.980s. Both journeys retained all five model-call
reservations, including the cancelled waiter. Recorded events show no overlapping
admissions and exactly one worker-start, worker-stop, foreground-start sequence.
Peak container memory was 2,058,534,912 bytes. This is not a GPU allocation cap.

The driver verifies the pinned image's preinstalled 8B weight hash and requires
loopback-only networking, <=4 GiB RAM, zero swap and <=4 CPUs. Model, repository,
normal memory and vault mounts are read-only; temporary sessions are deleted.
No private memory or generated prose is exported. This first report predates
the repeated-cancellation guard described below; it is retained as historical
evidence, with its original source hashes.

Reproduce using the [M3 offline container](../../local-assistant.md), replacing
its Python entry-point arguments with:

```sh
-m scripts.qualify_scheduling \
  --model-file /models/8b.gguf \
  --memory-root /home/fearsidhe/.claude/plugins/memory \
  --output /reports/scheduling.json
```

## Deterministic regression coverage

The final focused suite passed **160 tests in 11.88s**, including real fixture-process
ownership and HTTP behavior. Queue tests exercise foreground/FIFO ordering,
same-group exclusion, independent groups, queue bounds, reentrant requests,
permission/budget rejection, deadlines in startup and streaming, cancellation
at permit handoff, replay uncertainty, failed journals, same-endpoint model
replacement and a visible cancellable TUI queue.

Review of the idle replacement path identified cancellation during process
termination. Cleanup now finishes before releasing admission. A real process
that ignores TERM is killed and reaped even when the requesting call expires;
the replacement does not start after its deadline. Existing shutdown tests
continue to check actual process disappearance and now select resource events
explicitly because a scheduling terminal event follows the stop observation.

A stronger [repeated-cancellation regression](repeated-cancel-red.txt) then
reproduced a live process remaining after the second interruption. Cleanup now
continues under shielding through repeated cancellation, propagating cancellation
after the process is reaped. Both deadline and repeated-cancellation cases pass.

The first scheduling run on this final core source passed the plugin-free
journey, but the normal-memory journey exceeded the first-stream deadline.
That [failed report](scheduling-runtime-timeout.json) is preserved separately;
no gate, prompt, model or runtime setting was changed.

The [unchanged scheduling repeat](scheduling-final.json) passed both complete
journeys on the final source. This establishes the measured scheduling workflow
with normal memory as well as without plugins. The first-stream timeout remains
part of the evidence; a successful repeat does not establish reliable cold-start
latency. All final report core/helper hashes match the files in this branch.

Final-source scheduling measurements:

| Plugins | First stream seconds | Queue cancel seconds | Stream cancel seconds | Project write seconds | Busy abstention ms |
|---|---:|---:|---:|---:|---:|
| Absent | 13.691 | 0.027 | 0.016 | 15.316 | 3.914 |
| Normal memory | 11.919 | 0.021 | 0.009 | 16.547 | 4.573 |

Both final journeys retained five model reservations. Peak container memory
was 3,542,188,032 bytes; UI mount took 0.050s and 0.724s.

## Existing offline workflow regressions

The [first M3 regression](m3-runtime-timeouts.json) passed four of six project
journeys and all four unavailable-runtime recovery cases. Plugin-free harbor
and maple exceeded their resumed-answer deadlines. A live diagnostic trace
showed repeated runtime `loading` observations. The GPU monitor also exceeded
its five-second `nvidia-smi` deadline; the [traceback](m3-monitor-timeout.txt)
escaped at teardown and left the report unfinalized. The failed process-stop
checks also require a captured ready runtime; they do not alone establish a
leak. The pre-scheduling PR23 evidence already contains runtime and monitor
timeouts, so this scheduler is not presented as a fix for that variability.

The [fallback run before the cleanup guard](fallback-before-cleanup-guard.json)
passed all four real inference/TUI cases: connection refusal and HTTP 401,
with plugins absent and normal memory enabled. It retains its earlier core
hashes. Repeats use the existing M3 and fallback gates without relaxing them.

The [final-source M3 repeat](m3-final.json) passed all six full project journeys
and four unavailable-runtime recovery cases. It contains twelve exact native
writes, six real stream cancellations, restart with fresh project records,
normal memory and visible context/task status. Cancellation took 0.122–0.135s;
cold readiness was 10.64–13.31s. The reported device was an RTX 5070 with
12,227 MiB and driver 596.36; peak whole-device memory was 11,255 MiB, including
other applications. All recorded core/driver hashes match the final source.
This passing repeat does not erase the earlier latency failures or establish
consistent latency under uncontrolled external GPU activity.

The [final-source fallback run](fallback-final.json) passed all four journeys,
with one assignment, one exact native write, retained criteria, recorded/visible
switching, normal memory, settled replay and owned-runtime cleanup. Its recorded
source hashes also match the final core and qualification drivers.

## Scope and continuation

Final repository validation: **1468 passed, 7 skipped, 6 warnings in 328.32s**.
The seven existing skips are the unavailable Anthropic/Ollama recorded fixtures
and opt-in live Antigravity test. Locked offline sync, Ruff, whitespace checks,
sdist/wheel build and a clean Python 3.13 wheel smoke importing 65 modules passed.
The built wheel's core module bytes were checked against the current source.
The full suite ran after the GPU qualification containers exited.

The scheduler coordinates a single live Harness session tree and declared
device groups. It cannot control other applications, independent Harness
processes or GPU-driver latency. The existing external local server was not
stopped or adopted. Semantic assessments remain advisory under their prior
failed qualification gates. External-agent handoff still requires explicit
side-effect reconciliation. The next core work is the supervised improvement
candidate/evaluation/activation/rollback cycle, followed by the remaining M4
qualification and handoff work. Memory and agent-swarm remain plugins.
