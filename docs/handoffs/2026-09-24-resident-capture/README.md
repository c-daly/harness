# First native capture slice: evidence and limits

Implemented on `feat/resident-continuity-capture`, based on the reviewed resident
startup branch (PR #64). Configuration is opt-in; the user's installed Harness
configuration and normal vault were not modified. The dirty root checkout's 23
tracked/untracked working files matched their pre-work SHA-256 snapshot.

Core owns capture intent, bounded inference, prepared artifacts, status and
recovery in the existing session journal. The adapter uses the installed memory
plugin's recorder, writer, provider, index, lock and normal reader. There is no
new memory database and no external agent CLI used for the trial.

## Engineering controls

- Initial focused regression set: **109 passed**, Python 3.13.
- Expanded startup/context/events/capture/scheduling set with the installed
  plugin: **148 passed**, Python 3.12, before destination binding was added.
- Final capture, destination-binding, real-plugin and scheduling controls:
  **58 passed on each of Python 3.12 and 3.13**.
- TUI queue plus the earlier capture/scheduling/integration controls:
  **66 passed**, Python 3.13, before destination binding was added.
- Final TUI context/capture display and handoff destination controls:
  **11 passed**, Python 3.13 (**41.29 seconds**).
- Ruff and `git diff --check` passed.

The installed-plugin controls are not mocks of its storage. They start the
adapter over MCP, import the actual installed plugin, and use isolated temporary
vaults. They cover the recorder/writer/index/read-back path, a fresh Harness
session retrieving the record, identical-write reconciliation, content/project/
destination mismatch refusal, and a file-write-before-index interruption.
[Source hashes](plugin-revision.json) identify the installed files used.

The [full Python 3.13 suite](full-suite-313.log) reported **2,510 passed,
10 skipped, 1 failed**. The failure was the existing
`test_destination_fault_and_explicit_recovery[True-interrupt]` journey with a
`TimeoutError`. Capture was not configured in that scenario. All seven handoff
destination controls passed in an isolated follow-up (**34.51 seconds**).
This does not erase the full-suite failure or establish its exact cause.
The test now preserves its complete journey report and reports the failed stage
and state, so another occurrence is diagnosable. Its deadlines and gates were
not relaxed. A clean full CI run is still required before treating this branch
as ready to merge.
The published test log replaces personal home and pytest-user paths with
placeholders; test outcomes and the failure text are otherwise retained.

## Live local-model trial: not passed

The [frozen trial driver](../../../plugins/resident-memory/outing.py) requires:
an acknowledged capture; no mutation in the correction session; a fresh control
session writing `production`; another fresh session with normal project memory
writing `staging`; and distinct session IDs. File contents, not model prose,
determine the action gates.

The [original report](local-outing-initial.json) retains this unsuccessful run:

| Session | Observation |
|---|---|
| `28b2cd4a71bd47319a6d5d67f38e9b5b` | First conversational inference exceeded the configured 120-second task deadline. No memory write attempted. |
| `afe74943f0c24ebfae6264b763c51a7f` | Control inference also exceeded 120 seconds; no file was written. |
| `ff12501b6f5f4fa0ae12a64f07a2b11d` | Normal memory search ran; trial was interrupted by the operator after approximately 30 seconds. No file was written. |

The selected alias was `local`, configured for Qwen3-8B Q4_K_M at an already
running llama.cpp endpoint. The driver did not start or stop a model runtime.
A health probe succeeded; a slot probe observed active generation, so this was
not a missing-server failure. During the run the RTX 5070 reported 100% GPU
utilization and 10,663/12,227 MiB used, while another project's CUDA worker was
running. This supports a contention hypothesis; it does not isolate the exact
cause of latency or qualify model behavior under ordinary load.

Only the trial process was interrupted. Other workloads were left running.
The original report predates the driver's improved interrupted-run reporting:
its empty `captures` list reports observations, not intents. The seed journal
does contain `capture_requested` at sequence 22, left pending. The revised
driver includes such pending intents and explicitly marks interruptions.

Original journals, blobs, workspace and isolated vault remain at
`/tmp/harness-continuity-outing-20260924`; the raw driver log is beside that
directory. No production memory was written. None of the live behavior gates
is claimed as passed.

## Remaining work

Move routine capture off the foreground turn path; it currently waits within
its configured deadline despite using background local admission priority.
Then rerun the unchanged behavioral gates when the selected runtime has usable
capacity. Bind the actual continuity resume-brief composer and PM/experiment
interfaces, add stale/conflicting record and model-switch controls, and run the
planned longer outing with one agent-swarm worker through Harness.

The first capture source is the recorded user/final-assistant exchange with
explicit execution and acceptance status. Rich tool evidence, capture before
compaction/shutdown, project resolution and retry/discard UI remain open.
