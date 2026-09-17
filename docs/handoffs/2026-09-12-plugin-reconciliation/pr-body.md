# Plugin workflow reconciliation

## What changed and why

Core needed an explicit, testable contract for referencing agent-swarm
workflow state and normal memory-plugin contributions without importing
either plugin, plus a specification of what restart reconciliation may and
may not claim. Core already records tool facts (ToolCallProposed,
DispatchResolved, ToolCallCompleted) for every MCP dispatch; this change
derives plugin workflow references purely from those facts, reconciles
them after a restart through ordinary dispatch, and states plainly which
plugin steps cannot resume.

New module `src/harness/plugin_reconciliation.py`:

- `PluginWorkflowRef` and `ReconciliationReport` (frozen dataclasses).
- `project_plugin_workflows(envelopes)`: pure, total fold of completed
  `mcp__SERVER__workflow__workflow_*` calls into refs. Never raises; an
  unexpected result shape still yields a ref with `last_phase=None`.
- `count_memory_contributions` / `count_accepted_records`: fold completed
  `mcp__memory__*` calls (context reads and accepted writes) from the same
  log.
- `reconcile_from_log(envelopes)`: log-only report. Every ref is reported
  `unknown` -- core never invents a status without a live check.
- `reconcile(kernel)`: the live version. Re-dispatches
  `workflow__workflow_get_state` through `kernel.loop.dispatcher` for every
  ref -- ordinary dispatch, ordinary permissions, ordinary log. Classifies
  `active`, `finished`, or `unavailable`; unavailable refs are listed in
  `non_resumable`.
- `main`: read-only `harness plugins reconcile SESSION_ID --base-dir DIR`,
  wired into `harness` at `src/harness/cli.py` right after the `budget`
  ladder entry. Never starts or contacts an MCP server.
- `harness status` (`src/harness/status_cli.py`) now joins one summary
  line: `plugins: N workflow refs (M unavailable), K memory contributions,
  J accepted records`.

New test fixture `tests/fixtures/agent_swarm_stub.py`: an in-memory-only
stdio MCP server exposing `workflow__workflow_start`,
`workflow__workflow_get_state`, `workflow__workflow_advance_phase`, and
`workflow__workflow_stop`. It exists only under `tests/fixtures` and is
never installed as a real plugin; state does not survive a process
restart by design, which is what proves non-resumability in the tests.

`docs/plugin-reconciliation.md` documents the contract, the explicit law
(an in-memory plugin workflow lost across a plugin restart is not
resumable by core), and the four independence properties. One paragraph
was added to `docs/architecture.md` under Plugins pointing at it.

## Roadmap items advanced

Task 3 (plugin workflow reconciliation) from the harness remaining-roadmap
manifest: an explicit, testable contract for referencing agent-swarm and
memory plugin state without importing either, proven against a reference
memory plugin and a stub agent-swarm server with the M5 gate independence
properties.

## Validation

Ruff:

```
All checks passed!
```

Focused suite (tests/test_plugin_reconciliation.py, 11 tests, stub and
reference memory server started and stopped cleanly, no leaked process
after the run):

```
11 passed in 1.21s
```

Complete suite, Python 3.13:

```
2369 passed, 7 skipped, 6 warnings in 681.00s (0:11:21)
```

Complete suite, Python 3.12 (first attempt hit one unrelated flake in
tests/test_coordination_admission.py, a timing-sensitive test racing a
0.1s deadline against a 3s wait under host load from concurrent sibling
worktrees on this shared machine; a second attempt hit a different
unrelated flake in tests/test_tui.py::test_tui_pipes_mcp_child_stderr_to_file,
which carries its own comment noting its 5s timeout depends on host load.
Neither test, nor any file either touches, appears in this diff; the first
failure was confirmed to pass in isolation. A third attempt was clean):

```
2369 passed, 7 skipped, 6 warnings in 615.52s (0:10:15)
```

Build:

```
Successfully built dist/harness-0.0.1.tar.gz
Successfully built dist/harness-0.0.1-py3-none-any.whl
```

Smoke (scripts/smoke_wheel.sh dist/harness-0.0.1-py3-none-any.whl):
every shipped module imports cleanly from the installed wheel, including
ok harness.plugin_reconciliation, ending with:

```
== wheel smoke OK
```

git diff --check: clean.

## Known limits and deliberate omissions

- reconcile(kernel) classifies a ref as unavailable whenever the
  dispatch comes back an error, whether the cause was the server being
  disabled, never connected, or a genuine live failure -- one code path,
  since the dispatcher already turns every one of those into a graceful
  is_error tool outcome rather than raising. The distinction between
  disabled and failed is therefore not separately recorded.
- reconcile_from_log (and the harness plugins reconcile / harness
  status join) never classifies a ref as unavailable or finished from
  historical log evidence alone; every ref is unknown until a live check
  runs. This is deliberate (core never invents a plugin status), not an
  oversight.
- The environment this task ran in exports PYTHONPATH pointing at the
  agent-swarm plugin install directory for its own tool routing; that
  directory ships its own scripts package, which shadows this
  repository top-level scripts/ namespace package under Python
  namespace-package resolution rules and breaks collection of 9 unrelated
  scripts.*-importing test files. All validation commands above were run
  with PYTHONPATH cleared to avoid that collision; nothing in this diff
  touches scripts/ or depends on that variable.

## Commits

- 9827f33 feat(plugins): reconcile agent-swarm workflow refs and memory
  contributions from the log
