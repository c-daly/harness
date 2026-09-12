# Supervised source promotion and rollback

A passing [source experiment](source-improvement.md) can now be explicitly
selected for a new process. Core records the selection in the same session
journal, retains the exact preceding source and entrypoint, and offers rollback.
Memory and agent-swarm remain independent plugins. No model is required for
these operator controls, and they are not exposed as model tools.

## Select and start

Review the candidate patch, frozen checks and completed result first. For a
Harness source snapshot, use its normal `harness.cli:main` entrypoint and `src`
import root:

```text
/improvements source-adopt RESULT_ID trial harness.cli:main src
/improvements
/improvements show CHANGE_ID
```

The equivalent headless command is:

```sh
harness improve-source --base-dir DATA SESSION adopt RESULT_ID trial harness.cli:main src
```

`trial` is a named selection within this improvement session. Slot names contain
letters, digits, dots, underscores or hyphens and begin with a letter or digit.
Another Python project can name its own `MODULE:FUNCTION` and import root (`.`
for modules at the project root). The function follows the ordinary console-script
contract: read arguments from `sys.argv` and return an exit status or `None`.
Modules must exist in both snapshots; packages require `__init__.py`. Preparation
checks module ownership without executing imports. The operator should include
entrypoint/startup checks in the experiment; adoption does not prove that any
particular function or dependencies work.

Adoption requires the latest result for that candidate to pass its exact plan,
including benefit and held-out gates. The paired run must have its completed
terminal record, and no evaluation may still be running in the session. The
current management evaluator, fixed suite, artifact integrity and reconstructed
candidate snapshot must match. The first adoption retains the experiment's
incumbent as the rollback baseline. Later adoptions must start from the currently
selected exact snapshot. A changed evaluator or interpreter environment requires
a new plan and run; equivalent lexical spellings of the same interpreter path
are normalized, while distinct virtual environments remain distinct.

Start the selected version from a shell in the project where it should work:

```sh
harness run-source --base-dir DATA SESSION trial -- --help
harness run-source --base-dir DATA SESSION trial -- --model YOUR_ALIAS
```

Arguments before `--` identify the improvement journal and slot. Arguments after
it go to the selected program, including any `--base-dir`, `--resume`, catalog or
other application options. The improvement journal's base is not implicitly
substituted for the new program's data directory. Normal application defaults
still apply. Existing session locks still prevent two processes from resuming
one active conversation.

The launcher verifies the selected blob and writes a new private source tree
below `DATA/source-launches/launch-*`. It prints the selection ID, snapshot digest
and tree path to stderr, then replaces itself with the current Python interpreter
using a fixed bootstrap and `-I -B`. The selected import root takes precedence,
and the entrypoint's resolved file must belong to that snapshot. Ambient
`PYTHONPATH` cannot substitute a checkout or installed Harness for the selected
entrypoint. The selected program receives the operator's working directory,
environment and terminal streams, so ordinary project context, configuration and
memory-plugin access work through the usual application paths.

Each launch gets its own source copy. A running process keeps that copy when
another selection is adopted or rolled back. Launch adds no supervisor or
arbitrary process deadline. A startup/copy/exec failure leaves the selection
intact; failures caught before exec remove only the newly created copy. Source
trees from successful execs remain for inspection, including changes the program
may have made to its own copy. Remove them manually only after the corresponding
process has exited and any wanted work has been preserved. The immutable blobs
remain the source of future launches.

## Roll back

Use the original management installation to restore the preceding selection:

```text
/improvements source-rollback trial
```

```sh
harness improve-source --base-dir DATA SESSION rollback trial
harness run-source --base-dir DATA SESSION trial -- --help
```

Rollback restores the exact preceding snapshot and entrypoint. It does not rerun
checks, install dependencies, replay task tools, change running processes, or
undo writes those processes made to project/session data. It is an undo of the
last selection change; undoing a rollback selects the version preceding that
rollback. Keep the original management installation available even when the
selected version is broken or lacks these controls.

The journal append is the entire selection transaction. There is no second
mutable deployment pointer to reconcile after a crash. Replay and inspection
reconstruct selection records without starting processes or materializing files.
A torn journal must be recovered through the ordinary session recovery path
before launch; the launcher does not guess or repair concurrently with a writer.

## Scope of the guarantee

This is supervised source activation for future Python processes, not a package
release or service-deployment system. The selected code runs with normal operator
OS authority. Private copies separate edits; they are not a security sandbox.
The current interpreter's installed dependencies are used; their contents are
not frozen or attested. Data/schema migrations, service shutdown/restart,
dependency changes and live compatibility require separate review and testing.

Candidate edits to evaluation or permission code remain data while the existing
management installation evaluates adoption. A passing result alone cannot select
code or change that installation's policy. Explicit adoption does authorize the
selected program to run with operator authority at the next launch; review code
and launch configuration accordingly. The experiment's `activation_qualified=false`
continues to mean that the checks alone are insufficient authorization or broad
runtime qualification.

Core-directed patch authorship, stronger isolation, installed memory/swarm
reconciliation and M5/M6 live-use qualification remain open.
