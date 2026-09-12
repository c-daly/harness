# Supervised source promotion and rollback

Base: PR57 merged at b863dbf. Scope: the next explicit core source-improvement
control, independent of memory and agent-swarm plugins.

## Contract

An operator adopts a latest, completed, passing source experiment into a named
session-local selection. The record binds exact preceding and selected snapshot
blobs, entrypoints, result and evaluator identity. Existing selections must match
the experiment incumbent. Initial selection retains that incumbent for rollback.
Refuse stale evaluator/suite, failed or incomplete results, missing/corrupt blobs,
unfinished runs, invalid entrypoints and conflicting predecessor records.

Selection is one durable ImprovementRecorded append. No external activation pointer
or live files are changed. Rollback records the exact preceding source and launch
configuration without rerunning checks. Replay reconstructs selections only.

`harness run-source --base-dir DATA SESSION SLOT -- ARGS` reads the selection,
verifies its snapshot, writes a fresh private launch tree below DATA/source-launches,
and replaces the launcher process with the current Python interpreter executing
the selected module:function. A fixed bootstrap inserts the selected import root;
Python -I -B excludes ambient PYTHONPATH and avoids bytecode writes. Launch keeps
operator cwd, environment and stdio for ordinary project/memory/UI behavior.
Launch trees remain available for inspection; running processes keep their copy
across subsequent adoption/rollback. The original management installation remains
the rollback interface. No dependency installation, migration or service restart.

## Test-first sequence

1. Add source change schema, exact-version/predecessor rules, journal blob and
   completed-run validation. Test passing adoption, rejection and replay.
2. Add explicit CLI/TUI adopt and rollback controls with visible selection IDs.
   Test model-free controls and terminal behavior.
3. Add next-process launcher with fresh source copy and module:function entrypoint.
   Test actual incumbent/candidate/rollback output, argument/cwd propagation,
   mutation separation, corruption and failed materialization/exec behavior.
4. Probe the boundaries, document usage/limits, run locked Python 3.12 and 3.13
   suites, Ruff, build and installed wheel smoke before commit/push/PR.

## Authority and remaining scope

Operator controls are not model tools. Passing tests alone never select code.
Candidate evaluator or permission edits remain data until explicit operator
selection; adoption checks run in the existing management installation. Trusted
source runs with normal operator OS authority. This is neither a sandbox nor a
package release system; dependencies, migrations and running service coordination
need independent qualification. Agent-directed source authorship and M5/M6 live
qualification remain open.

## Validation corrections

The first 24 source-promotion cases passed, and the final focused source/CLI set
passed 104 tests. A final fault probe then found an unhandled TornLogError in the
launcher. The launcher now gives recovery guidance, preserves the torn log, and
does not create a launch tree; a real CLI regression checks this behavior.

The interrupted full runs also exposed existing short setup deadlines in
coordinator fixtures: Python 3.13 stopped with 442 passed, three failures and six
skips; Python 3.12 stopped with 432 passed, four failures and six skips. These
are incomplete runs. A controlled 650 ms member-entry delay reproduced all five
related fixture cases. They now capture the real timer after entry, verify that
failed/root/nested grants preserve the intended deadlines (or extend exactly the
selected deadline), then expire that timer explicitly. Deadline outcomes,
cleanup, persistence failure, restart and exported lineage assertions remain.
Production execution limits are unchanged. The torn-journal regression and five
delayed probes failed before correction; the complete affected set passes 63
tests afterward. The source demonstration is repeated with the final source
before new complete supported-version suites.

Final locked validation: **2,358 passed on Python 3.13 (663.09 s)** and **2,358
passed on Python 3.12 (670.15 s)**, each with seven skips and six existing MCP
warnings. All 275 source/test/driver/configuration files stayed frozen during
these runs. All 92 application-source hashes match the repeated demonstration.
Ruff, whitespace, source/wheel builds and the installed CLI/91-module smoke pass;
all 92 packaged core Python files match source. The source distribution is
refreshed after recording these documentation-only results.
