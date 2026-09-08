# Model-selection continuity — September 8, 2026

Base: `ca5618c`, merged PR31. This fixes the next ordinary-workflow gap after
the project-wide status review. Selection is a session preference; the last
model call may instead be routed, fallback, delegated or internal inference.

## Regression and implementation

Three tests failed on the base: ordinary `--continue` used Echo after a catalog
selection; `/resume` dispatched using the departing session's model; and the
picker tore down the current session despite the target alias being unavailable.

The durable event records a catalog alias and pin when the preference takes
effect, including before its first call. Both CLI resume modes and the terminal
picker restore it using current catalog configuration. An explicit `--model`
overrides and saves the new choice. Deferred switches are recorded at the turn
boundary. Preflight validates missing/invalid aliases before TUI teardown; the
kernel resolves again from replay while holding the target writer lock. Failure
there closes the session without appending a resumed boundary.

Historical logs lack selection intent and retain the old startup/default
behavior until an explicit `--model` establishes it. Credentials, runtime
readiness, grants and fallback authority are not reconstructed from old calls.
See the [user guide](../../user-guide.md) for recovery instructions.

## Actual local inference across process boundaries

Installed assets were reused: Qwen3-8B Q4_K_M (5,027,783,488 bytes; SHA-256
`d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`) and
llama.cpp b9603. The temporary `continuity-cpu` profile uses port 8185, CPU
execution, an 8192-token context, four threads and disabled thinking. The
normal user catalog was not edited. MCP and plugins were disabled; the full
native tool inventory was present and all three prompts requested no tools.

The [terminal driver](terminal_check.py) calls the ordinary CLI `main()` in each
terminal process. It replaces only the display launch with Textual's virtual
terminal pilot; actual catalog loading, dispatch, inference and runtime
management run normally. The intervening headless invocation is the unmodified
`harness --continue -p ...` entry point in another process.

| Process | Selection path | Independently checked result |
|---|---|---|
| First terminal | Start in Echo, enter `/model continuity-cpu` | Teach synthetic codename `amberfern`; exact reply `CONTINUITY_SET` |
| Headless restart | `--continue`, no `--model` | Exact reply `amberfern`, same session and model alias |
| Second terminal | `--continue`, no `--model` | Alias restored before dispatch; exact reply `amberfern` |

The [journey report](journey-report.json) verifies three user messages, two
resume boundaries and exactly three completed calls on `continuity-cpu`.
Recorded call durations, including each owned runtime startup, were 41.714,
13.033 and 10.324 seconds. Other verification was running on the host; these
numbers are observations, not a latency benchmark or general CPU qualification.

Both terminal stages checked the alias, exact reply and unsent draft in the
final compositor. Draft preservation here means typing during a turn; drafts
are not persisted across processes. The [selection report](select-report.json)
and [resume report](resume-report.json) retain those checks and elapsed turn
times. Screenshots: [initial selection](select-terminal.svg),
[resumed terminal](resume-terminal.svg).

The event log contains three matched owned-runtime start/stop pairs. An
independent socket probe confirmed port 8185 closed after the checks. The normal
catalog's SHA-256 was unchanged. No weights were downloaded and no user-managed
server was stopped. This does not establish offline network isolation,
memory/plugin interoperability, larger-model performance or semantic quality.

To repeat, copy the driver to a fresh temporary directory, place a catalog with
the `continuity-cpu` alias there using the installed-model setup instructions,
and run `terminal_check.py select` through the project environment. Invoke
`harness --continue` with that directory's `sessions` data root and `models.toml`,
the same workspace and `--no-mcp --no-plugins`, asking for the taught codename.
Then run `terminal_check.py resume` in a third process. The driver expects the
normal catalog to exist so it can check that it remains unchanged.

## Automated verification

- Python 3.13: **216 passed in 241.00 seconds**, covering the complete affected
  terminal, CLI, queue, routing, loop, event, fold, resume and context groups.
- Python 3.12: **36 passed in 3.59 seconds**, using the separate existing
  environment for the new model-selection and replay/event/resume tests.
- Ruff and whitespace checks passed; the source distribution and wheel built
  offline. Full-suite CI remains a separate check on the published PR.
- New cases cover selection without a model call, explicit overrides, routing
  pins, current prices, non-preference calls, unavailable/invalid aliases,
  failed selection writes, clear, deferred switches, live ownership, torn-tail
  recovery and changed selection between preflight and writer acquisition.

The initial three failing regressions are the before-change evidence. An early
expanded-test run had two fixture errors; the final groups above include the
corrected cases. Full provider and platform qualification remains in the core
roadmap; these scoped checks do not close M6.

## PR32 review correction

The original `52e719f` implementation wrote a selection on every resumed kernel,
including legacy routing defaults, departing TUI models and administrative
model overrides. The review correction restricts resume-time persistence to an
explicit conversational selection. Existing preferences need no new event;
legacy sessions retain their old startup/default behavior until `--model` or
`/model` establishes a preference. Improvement and assessment commands preserve
the conversation's selection.

Six new regressions reproduced the problem; additional coverage includes both
headless and terminal CLI entry points. The original CI failures in two
improvement-command tests came from the same unconditional write and are part
of the affected verification group. The real local inference reports above
remain evidence from `52e719f`; the review correction's checks are tracked in
the [implementation record](../../superpowers/plans/2026-09-06-core-agency-progress.md).
