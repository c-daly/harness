# M3 evidence and resumption

Branch: `feat/local-assistant-m3`, based on merged PR20 (`33a24fc`).
The current authority is [the implementation record](../../superpowers/plans/2026-09-06-core-agency-progress.md)
and the [M3 usage/gate document](../../local-assistant.md).

Reports, in order:

- `m3-diagnosis-baseline.json`: three same-model 4B failures. Its original path
  categorizer did not account for workspace canonicalization.
- `m3-diagnosis-path.json`: one corrected trace proves the model read the source
  label `project-facts` as a filename instead of using the fetched `FACTS.json`.
- `m3-diagnosis-provenance.json`: three successful 4B writes after the origin and
  label fix, with the historical resident pilot's prompt/profile otherwise fixed.
- `diagnose-context.py`: formatted copy of that opt-in narrow diagnostic driver.
  Its report path names the provenance arm; use the historical source checkout
  to reproduce the pre-fix baseline. It is not the M3 gate.
- `m3-qualification-initial.json`: full CLI-default/shipped-profile 4B v1 run;
  4/6 journeys pass, two resumed writes fail, and the explicitly paused empty
  queue defect fails both unavailable cases.
- `m3-qualification-profile.json`: explicit file-operation guidance and queue
  fix; 4/6 journeys pass, with two initial writes omitted. Both unavailable cases
  pass. The 4B model remains unqualified.
- `m3-qualification-8b.json`: preinstalled 8B model with the same fixed inputs and
  bounds; 6/6 journeys and 2/2 missing-assets cases pass.
- `m3-qualification-final.json`: first stricter v2 run; 6/6 journeys and 4/4
  missing-assets/startup-exit cases pass. Subsequent full-suite/launcher checks
  exposed two automatic-pause regressions and ignored CLI grants without config.
- `m3-qualification-release.json`: **the final source-matching v2 report**, after
  those fixes; 6/6 journeys and 4/4 fault cases pass. Twelve exact file writes,
  six cancellations/restarts, fresh normal-memory retrieval, and visible recovery.

Each full report retains the source hashes available to that driver version;
metadata fields expanded during v2. All reports omit private memory text and
model prose. Qualifier sessions are temporary and deleted. The actual normal
vault is mounted read-only. No model assets, user catalog or external server
were changed. Diagnostic model/profile selection was supervised; these records
do not claim automatic improvement adoption or a pre-registered paired experiment.

The final full suite passed **1347 tests, 7 skipped, 6 warnings in 305.65s**.
The skips are missing Anthropic/Ollama fixtures (three each) and opt-in live
Antigravity. Locked offline sync, Ruff, whitespace, package build and the
62-module fresh-wheel smoke passed. The first full run's two failing recovery
cases are preserved in the implementation record; both pass after the fix.

Next work is M4: bounded semantic functions, graceful resource changes/fallback,
and a supervised improvement cycle with explicit adoption and rollback. Retain
this M3 suite as a regression gate; broader model/task reliability, CPU-only
support, mixed-agent supervision and daily-use qualification remain open.

`launcher-smoke.json` separately verifies the shipped launcher: a real terminal
project-only task and a headless normal-memory task both wrote exact public
artifacts and stopped their owned runtime. The temporary sessions are deleted
after metadata export. The launcher inherits the saved context profile on resume;
explicit CLI overrides still apply.
