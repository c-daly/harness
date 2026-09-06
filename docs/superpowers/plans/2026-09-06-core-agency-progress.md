# Core agency implementation record

Implementation branch: `feat/core-agency`, based on `main` at `ce722b4`.

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
| M0 baseline and feasibility | In progress | Baseline: 885 passed, 7 skipped, 4 warnings in 254.23s. Lint fixed, CI defined, sdist/wheel built. Local request shape investigated; broader quality, latency, and installation checks remain. |
| M1 correctness and interaction | In progress | Lifecycle and storage integration: 914 passed, 7 skipped. Four subsequent delegation/validation RED cases reproduced. Input/controller and applicable enforcement controls remain. |
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
- Full context budgeting, root-shared delegation limits, and broader storage
  fault qualification remain separate work. This is not a complete M1 claim.

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
