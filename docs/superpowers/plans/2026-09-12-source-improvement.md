# Isolated source improvement experiments

Following PR56, implement the first operational source experiment in the existing
core improvement journal. Memory and agent-swarm remain independent plugins.

## Scope

- An operator imports an agent's committed candidate and its incumbent from a
  local Git repository. Read committed objects only; preserve every working tree.
- Freeze a portable, content-addressed incumbent snapshot, exact file-change
  artifact, and operator-authored Python checks before any check executes.
- Compare both versions, alternating order, in fresh source directories for
  every check. Bound overall time, per-check time, output, cases and input size.
- Record the existing evaluation intent/result/terminal events. Preserve unknown
  measurements on interruption; replay never executes code. Inspect the patch,
  fixed checks and process outcomes from normal improvement controls.
- Expose explicit CLI and TUI controls without requiring an inference provider.
  These controls are not model tools. No automatic code activation or adoption.

The checks execute source code with the operator's OS authority. Fresh directories
provide edit separation and reproducibility, not a hostile-code sandbox. Sanitized
child environments avoid implicit credential forwarding; they do not prevent
filesystem or network access. Operator-declared held-out cases are not attested
as secret. Exit status is a measured check outcome, not proof of general correctness.
Promotion, rollback, delegated patch authorship and stronger isolation follow.

## Test-first steps

1. Reproduce the missing source experiment path using a tiny committed Python
   project with one known defect, one regression, and an independent new case.
2. Implement validated snapshots/patches and prepare immutable core records.
   Reject symlinks, submodules, unsafe paths, excess sizes and empty candidates.
3. Implement bounded paired execution with process-group cleanup, frozen checks,
   provenance, and existing recovery. Test a useful fix, a regression, timeout,
   excessive output, cancellation, evaluator drift and corrupt artifacts.
4. Add operator CLI/TUI actions and readable patch/result inspection. Exercise
   their real surfaces and a fresh-session restart without a model or plugins.
5. Run both supported locked Python suites, Ruff, packaging and wheel smoke.
   Document exact results and remaining M5/M6 gates; commit and open the next PR.

## Validation correction

The first Python 3.13 full run exposed an existing tool-interruption fixture's
50 ms assumption: cancellation could precede tool entry. Python 3.12 passed.
A controlled 300 ms provider delay reproduced the same gap in all four related
tool-gather/repair cases. Their fixtures now wait for actual tool entry; the
partial-gather case additionally waits for the fast tool's recorded completion.
The model-interruption fixture also waits for provider entry. Cancellation,
history pairing, completed-result preservation, event ordering and recovery
assertions remain unchanged. All four delayed probes and all 15 loop tests pass
before repeating the complete supported-version suites. Application source is
unchanged by this fixture correction.

Further review reproduced implicit parent-repository discovery when `TMPDIR`
was itself inside a Git checkout. Check subprocesses now set a Git discovery
ceiling at their temporary root. The regression uses real Git beneath a real
parent repository. Full runs were stopped before editing; the corrected source
and loop tests pass all 60 cases before the final full reruns. The historical
source demonstration is repeated against this corrected runner.

## Outcome

Both final locked environments pass 2,333 tests (Python 3.13: 594.74 s; Python
3.12: 603.19 s), with seven skips and six existing MCP warnings each. All 257
source/test/driver hashes stayed fixed. Ruff, source/wheel builds and the clean
installed CLI/90-module smoke pass, and all 91 packaged core files match source.
The repeated retrospective judge-failure comparison passes and retains exact
runner hashes. Source promotion, rollback and stronger isolation remain separate.
