# Core agency implementation record

Implementation branch: `feat/core-agency`, based on `main` at `ce722b4`.

Committed checkpoint: `0351b75` (roadmap, lifecycle, and storage/result integrity).

**Resumed after machine restart.** All 26 files in the
[restart handoff](../../../HANDOFF.md) matched their saved hashes before new
edits. That handoff remains a historical snapshot; this record tracks subsequent
implementation and verification.

The user authorized implementation of the
[core agency roadmap](2026-09-06-core-agency-roadmap.md) on September 6, 2026.
This record separates implemented behavior, verification, and outstanding
qualification. Milestone completion requires its roadmap gates, not just code.

| Milestone | State | Evidence / remaining work |
|---|---|---|
| M0 baseline and feasibility | In progress | Baseline: 885 passed, 7 skipped. CI, build and fresh wheel installation checked locally. Two restart inference probes remain too slow for synchronous UI use. Hardware/profile selection and cold startup remain. |
| M1 correctness and interaction | In progress | 931 passed, 7 skipped after storage, delegation, controller/UI and MCP capability changes. Context budgets, broader enforcement/redaction and fault qualification remain. |
| M2 inference / agent contracts | Pending | Separate execution kinds and migrate actual call paths. |
| M3 local core assistant | Pending | Readiness, runtime ownership, normal memory access, and offline task journey. |
| M4 bounded semantic agency | Pending | Typed functions, evaluation, scheduling, capability-aware fallback, and operational self-improvement lifecycle. |
| M5 heterogeneous work / plugins | Pending | Supervision, plugin reconciliation, portable continuation, isolated improvement patches and rollback. |
| M6 daily-use qualification | Pending | Measured UI, live adapter boundaries, offline and human dogfood gates. |

## Baseline observations

- Source baseline: `ce722b4`; existing user `.claude/` and `.context/` remain
  outside the implementation changes.
- Installed local endpoint: `127.0.0.1:8080`; health reports `ok` and the model
  inventory reports `unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS`.
- GPU inventory: RTX 5070, 12,227 MiB. This identifies the test host; it is not
  a performance qualification or portable minimum requirement.
- The existing local server is user-managed. Implementation must not stop or
  restart that process as if Harness owned it.
- CI now defines Python 3.12/3.13 lint, tests, packaging, and a wheel-install
  smoke check. A workflow definition is not evidence of a hosted CI run.

## Completed change: model lifecycle and internal inference

- Added failed/aborted model facts, cancellation metadata, and a defaulted call
  purpose. Resume repairs both tool and model intents in proposal order.
- All three CLI adapters use the same MCP lifecycle context manager, covering
  cancellation during startup and generator teardown before server shutdown.
- Compaction now runs through dispatch, permissions, and accounting; its result
  does not become an ordinary assistant message. Session model identity ignores
  internal inference.
- Versioned telemetry projects pending/terminal calls, rejects stale schemas,
  and applies event batches atomically and idempotently.
- Validation: 90 focused tests passed; the first full integration run found one
  compaction test relying on the old unaudited-call contract. Updated it to
  verify both accounting and transcript isolation; all five compaction journeys
  passed. The final integration run reported **897 passed, 7 skipped, 4 warnings
  in 249.31s**; Ruff and `git diff --check` passed.
- Integration command: `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra
  --disable-warnings --ignore=tests/test_result_integrity.py`. The ignored file
  contains the next change's seven newly reproduced RED cases and is not part
  of this completed change. Those cases must pass before the storage change is
  complete. No live provider-containment claim follows from these tests.

## Local feasibility investigation

The initial six probes produced three timeouts and three unparseable responses.
An explicit no-reasoning request completed more quickly but still lacked JSON
conformance. A subsequent probe using nested `response_format.json_schema`
returned the expected classification in **7,906.88 ms**, with 31 prompt and 19
completion tokens. The installed server's request shape matters; endpoint health
alone does not demonstrate the requested inference capability.

`scripts/measure_local.py` now records the corrected request profile and keeps
usage, finish reason, and response lengths when validation fails. It measures
an existing service; it does not qualify cold startup or a held-out decision
corpus. The initial response-format reference was the
[llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/tree/master/tools/server);
the running server's actual response determines whether a capability works.

After restart, the corrected nested-schema probe returned a valid acknowledgement
in **12,708.45 ms**; the supplied-context probe timed out after **15,012.37 ms**.
The endpoint reported the same model identifier. Exact output is preserved in
[the restart probe](../../handoffs/2026-09-06-core-agency/local-probe-after-restart.json).
These two samples do not qualify latency or offline startup. Core interface
controls must stay independent of inference completion.

## Storage and result integrity

- Blob references validate digest syntax and nonnegative size. Reads verify
  regular-file identity, size, and SHA-256 without following file symlinks.
  Publication syncs complete bytes before an exclusive link; an existing corrupt
  object is an error, never silently overwritten or reused.
- Dispatch resolves sidecars before inference. The external-agent text bridge
  now includes tool names, call IDs, arguments, results, and error status; it
  rejects unsupported images and unresolved sidecars. MCP returns full results.
- Recovery owns a persistent advisory guard through read, atomic repair, fold,
  and writer acquisition. Exact torn bytes survive partial UTF-8 and interrupted
  publication; failed recovery releases ownership for a subsequent retry.
- The original seven RED cases now pass in a 64-test focused run. Additional
  tests cover real process death, absent blobs, symlinks, newline commit boundaries,
  recovery retry, transcript fidelity, and the MCP client path.
- Full integration after storage changes: **914 passed, 7 skipped, 5 warnings
  in 255.15s**, using `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra
  --disable-warnings`. Localhost MCP integrations ran with socket permission.
  The next file, `tests/test_delegation_scope.py`, was written after this run's
  collection and separately produced four expected RED failures; it is not
  evidence against the completed storage change or a passing delegation gate.
- Full context budgeting and broader storage
  fault qualification remain separate work. This is not a complete M1 claim.

## Delegation, controller, and UI checkpoint

- Descendants retain their actual parent session and intersect tool restrictions,
  including through coordination tools. Root-shared dispatch/depth/child limits
  cannot be reset by a grandchild. Tests cover excluded tool attempts, recursive
  limits, retry accounting, cancellation release, and failed child initialization.
- Dispatcher validates final arguments, records tool cancellation, and preserves
  the same terminal result in live and folded history. Concurrent tool failures
  settle siblings before teardown. Outward MCP requires a per-run URL capability
  and rejects browser-origin requests.
- Both frontends use the core prompt controller. TUI follow-ups are acknowledged
  and queued; edit/remove/pause/resume/clear are explicit, draft history is
  preserved, and model changes wait for logical turn boundaries. The queue is
  visibly **memory only**. Queued text is not logged as user speech until started.
- Final-compositor tests found overlapping bottom widgets; one container now
  renders queue, status, and composer without hiding them. Tests verify visible
  pending/paused state, unsent drafts, edit/remove, and a model switch between
  whole turns rather than between tool iterations.
- Catalog resolution no longer imports LiteLLM or triggers its remote cost-map
  fetch. It reads the installed snapshot; unknown metadata remains unknown.
- Final integration: **931 passed, 7 skipped, 5 warnings in 261.13s** with
  `UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra` and localhost/process
  access. Warnings are the existing deprecated MCP client helper in five tests.
  Ruff and whitespace checks passed. Sdist/wheel built, and the wheel installed
  offline into a new isolated environment for the CLI help check.
- Diagnostic qualification: restricted test runs exposed a 300-second asyncio
  executor shutdown warning. A minimal `asyncio.to_thread(lambda: 1)` reproduction
  with a one-second shutdown timeout reproduced it in the restricted sandbox
  (1.003s), while the same program outside it finished in 0.002s without warning.
  This distinguishes the sandbox notification issue from catalog SDK imports;
  do not claim that the metadata change alone fixes that sandbox behavior.
- This checkpoint does not implement resident local fallback, the inference/agent
  contract split, or self-improvement experiments/adoption. Those remain active
  roadmap work, including the user's first-class self-improvement requirement.

## Validation policy

Use meaningful failing regression cases for behavior changes, then targeted
checks and full-suite integration checks. Preserve skipped, unavailable, and
human-only evidence as such. Replay fixtures cannot certify live provider
containment; a fake local endpoint cannot qualify actual local-model quality.

## Scope steering

On September 6 the user made self-improvement a first-class requirement.
The roadmap now includes evidence, candidate and experiment records, evaluation,
adoption policy, safe activation, and rollback throughout M0–M6. Automatic
adoption policy is an outstanding product preference; foundation work continues.
