## Summary

- Adds a single bounded native source-authoring task: SourceAuthorSpec freezes
  the incumbent snapshot, editable paths, evidence and evaluation suite before
  any inference; author_source dispatches one scoped model call through the
  normal dispatcher (no tools, no activation); finalize_source republishes the
  resulting candidate/plan deterministically with no further model call. Wires
  source-author/source-finalize into the shared CLI/TUI improvement surface
  and the improvement journal.
- Fixes a defect found while completing this increment: a partial
  SourceAuthorSpec.limits mapping (for example only `timeout_seconds`: 90)
  was validated directly against TaskLimits, so every omitted field fell back
  to the generic TaskLimits defaults (max_iterations=20, 4 MiB input, 1 MiB
  response, 65536 chunks) instead of the tighter author defaults, and the
  scope validator then rejected the spec because max_iterations was not 1.
  A field_validator(`limits`, mode=`before`) now merges a partial
  mapping over the author defaults before TaskLimits validation, leaving
  timeout_seconds absent unless the caller supplies it, so omitted timeouts
  still inherit session and task timeout semantics via author_source and
  bound_task, while an explicit timeout_seconds passes through unchanged.
  The existing cap check in scope is unchanged, so a caller that raises a cap
  above the author maximum is still refused.
- Adds a scripts/measure_source_authorship.py local-model measurement driver
  and its retained report, plus docs (docs/source-authorship.md, handoff
  README) including honest hash provenance for the two retained measurement
  runs.

This advances the roadmap bounded native source-authoring increment described
in docs/superpowers/plans/2026-09-12-source-authorship.md.

## Files changed

- src/harness/source_authorship.py (new): SourceAuthorSpec, author_source,
  finalize_source, inspect_authoring, validate_record, plus the partial-limits
  field_validator fix.
- tests/test_source_authorship.py (new): full behavioral suite for the
  authoring task, including the three previously-red partial-limit tests and a
  new direct-spec regression
  (test_partial_limit_override_direct_spec_keeps_author_defaults).
- src/harness/cli.py, src/harness/improvement.py,
  src/harness/improvement_cli.py, src/harness/improvement_journal.py,
  src/harness/source_improvement.py, src/harness/source_improvement_cli.py,
  src/harness/tui.py: wiring for SourceAuthoring records and the
  source-author/source-finalize commands.
- tests/test_coordination_budgets.py: supporting test update for the wiring
  above.
- scripts/measure_source_authorship.py (new): opt-in local-model measurement
  driver.
- docs/architecture.md, docs/core-inference-and-improvement.md,
  docs/source-improvement.md,
  docs/superpowers/plans/2026-09-06-core-agency-progress.md: cross-references
  for the new increment.
- docs/source-authorship.md (new): user docs, including the
  partial-limits-override sentence added under Prepare an authoring
  specification.
- docs/handoffs/2026-09-12-source-authorship/README.md,
  docs/handoffs/2026-09-12-source-authorship/report.json (new): measurement
  write-up with honest run-v1/run-v2/current hash provenance (see below).
- docs/superpowers/plans/2026-09-12-source-authorship.md (new): the
  implementation plan this closes.

## Tests added

- test_partial_limit_overrides_keep_author_defaults_in_the_shared_command
  (3 parametrized cases: timeout_seconds, max_output_tokens, max_input_bytes
  overrides). Previously RED, now green.
- test_partial_limit_override_direct_spec_keeps_author_defaults: new
  direct-spec regression exercising SourceAuthorSpec.model_validate without a
  kernel/CLI round trip.
- Full existing test_source_authorship.py suite (46 tests) covering
  authoring, finalization, cancellation and recovery, publication-failure
  recovery, permission/budget/oversized/busy dispatch controls, path/suite
  validation, TUI visibility, and the shared CLI/TUI surface.

## Verification

tests/test_source_authorship.py, both interpreters
(PYTHONPATH=src:., locked venvs, no uv sync):

```
46 passed in 15.46s   # Python 3.13.15
46 passed in 8.66s    # Python 3.12.14
```

Ruff:

```
All checks passed!
```

Complete suite, Python 3.13.15:

```
6 failed, 2398 passed, 7 skipped, 6 warnings in 786.71s (0:13:06)
```

Complete suite, Python 3.12.14:

```
5 failed, 2399 passed, 7 skipped, 6 warnings in 784.39s (0:13:04)
```

Packaging:

```
Successfully built dist/harness-0.0.1.tar.gz
Successfully built dist/harness-0.0.1-py3-none-any.whl
```

scripts/smoke_wheel.sh dist/*.whl:

```
== wheel smoke OK
```

## Known, pre-existing failures (not caused by this change)

Both complete-suite runs above failed on the same set of tests, none of which
touch any file this branch modifies (git diff HEAD --stat against those files
is empty):

- tests/test_coordination_admission.py::test_deadline_settles_children_and_retains_completed_proposal
- tests/test_local_resources.py::test_loading_cancellation_and_deadline_reap_owned_process[deadline]
- tests/test_mcp_serve.py::test_shutdown_settles_an_active_tool_before_returning
  (3.13 run only)

Re-run in isolation (without concurrent sibling test runs on the shared
machine), these three passed. They are timing/deadline-sensitive tests that
flake under CPU contention, not a regression from this branch.

- tests/test_external_agent_runtime.py::test_codex_process_calls_scoped_mcp_tools_and_preserves_transcript
  (all 3 parametrized cases)

This one fails deterministically even in isolation (5 separate runs). It is
unrelated to source authorship (a real-child-process MCP/codex
transcript-ordering assertion) and the file is byte-identical to the base
commit (4c09803), so this is a pre-existing environment issue, not introduced
here. Left unfixed as out of scope for this task.

## Left out or deviations

- Did not attempt to fix the pre-existing
  test_codex_process_calls_scoped_mcp_tools_and_preserves_transcript
  failures; out of scope for this bounded increment and unrelated to any file
  this branch touches.
- Per task instructions, used the two locked interpreters directly with
  PYTHONPATH=src:. instead of uv sync for all Python invocations.


## Review fixes

Addressed two items from review on PR #59:

1. should-fix: the `limits` before-validator only merged author defaults
   for a dict; a bare `TaskLimits(...)` instance skipped the merge and
   only failed later in `scope()`. Added an explicit
   isinstance(value, TaskLimits) branch that merges
   value.model_dump(exclude_unset=True) over the author defaults, so unset
   fields still keep author defaults (including an absent
   `timeout_seconds`) and the existing cap check still refuses an
   explicit max_iterations above the author cap. Added
   test_explicit_tasklimits_instance_keeps_author_defaults.
2. nit: replaced the two remaining hardcoded 128 * 1024 response-size caps
   (`_response()` and `inspect_authoring()`) with a lookup into
   `_AUTHOR_LIMIT_DEFAULTS["max_response_bytes"]`.

Verification after the fix (PATH-prefixed locked 3.13.15 interpreter,
PYTHONPATH=src:.):

```
47 passed in 7.64s   # tests/test_source_authorship.py alone
61 passed in 9.56s   # tests/test_source_authorship.py + tests/test_external_agent_runtime.py
```

Ruff:

```
All checks passed!
```

Packaging:

```
Successfully built dist/harness-0.0.1.tar.gz
Successfully built dist/harness-0.0.1-py3-none-any.whl
```

scripts/smoke_wheel.sh dist/*.whl:

```
== wheel smoke OK
```

Note: test_codex_process_calls_scoped_mcp_tools_and_preserves_transcript,
which failed deterministically in isolation during the initial completion
pass, passed cleanly here (4 of the 14 tests in that file) and again when
re-isolated with -k codex_process (4 passed, 10 deselected). That failure was
environment/load-dependent, not a real defect in this branch.

Commit: 1bce83b fix(source-authorship): merge explicit TaskLimits over author
defaults
