# Isolated source improvement experiments

Core can now import a committed source change, freeze paired checks, and evaluate
it through the normal improvement journal. This is the source counterpart to the
[supervised prompt loop](supervised-improvement.md). An agent can author the
candidate in its own checkout; an operator selects its two revisions and checks.
No model, provider, memory plugin or agent-swarm plugin is needed to run the checks.
The initial runner uses POSIX process groups and is tested on Linux.

## Prepare and review

Work in an isolated checkout when authoring changes. Commit the candidate, leaving
the installation running the incumbent alone. Obtain an existing evidence ID with
`/improvements` or `harness improvements SESSION --base-dir DATA`. Evidence comes
from the core improvement lifecycle; it still points to the original session event.
Use `/improvements show ID` to inspect the observation before selecting it.

Create an operator-authored specification, for example this small Python project
whose `calc.py` defines `double`:

```json
{
  "repository": "/absolute/path/to/candidate-checkout",
  "incumbent_revision": "BASE_COMMIT",
  "candidate_revision": "CANDIDATE_COMMIT",
  "evidence_ids": ["EXISTING_EVIDENCE_ID"],
  "hypothesis": "Use both operands when doubling",
  "expected_benefit": "Correct previously failing inputs without regressions",
  "cases": [
    {
      "id": "existing-behavior",
      "partition": "regression",
      "critical": true,
      "script": "import sys\nsys.path.insert(0, '.')\nfrom calc import double\nassert double(1) == 2\n"
    },
    {
      "id": "independent-input",
      "partition": "held_out",
      "critical": true,
      "script": "import sys\nsys.path.insert(0, '.')\nfrom calc import double\nassert double(7) == 14\n"
    }
  ]
}
```

The proposal supports the same quality gates as other experiments:
`min_improved_cases` (at least one), `min_held_out_correct` (at least one),
`min_held_out_improved` (default zero), `max_latency_ratio` (default 1.2), and
`max_case_latency_ms` (default 30,000). Latency includes child startup and cleanup,
but excludes source materialization. Choose gates before running checks; a single
paired run does not establish stable performance or general correctness.

```text
/improvements source-prepare /path/to/source.json
/improvements show CANDIDATE_ID
/improvements show PLAN_ID
```

The headless equivalent requires no model or catalog:

```sh
harness improve-source --base-dir DATA SESSION prepare /path/to/source.json
harness improvements --base-dir DATA SESSION --show CANDIDATE_ID
harness improvements --base-dir DATA SESSION --show PLAN_ID
```

Preparation only reads local committed Git objects. Dirty edits and untracked
files are excluded, working trees and branches are unchanged, and no hooks or
checkout filters run. Revisions resolve to full commit IDs with replacement
objects disabled. Git snapshots refuse symlinks, submodules, unsafe paths,
more than 2,048 files, or more than 16 MiB of file content. Provision required Git
objects locally first. Dependency installation and network fetching are not part
of preparation.

The candidate artifact is a versioned JSON file-change patch: exact additions,
replacements, deletions, executable modes and binary bytes, with preceding file
digests and an immutable incumbent snapshot reference. Its SHA-256 is the
candidate version. Inspection derives a unified text diff and labels binary
changes; a 65,536-character preview limit is explicit. The complete patch remains
in the verified blob store. Plan inspection shows the frozen check scripts and
gates. This format is not a shell script or a `git apply` input.

## Execute and inspect

```text
/improvements source-evaluate PLAN_ID
/improvements show RESULT_ID
```

```sh
harness improve-source --base-dir DATA SESSION evaluate PLAN_ID
harness improvements --base-dir DATA SESSION --show RESULT_ID
```

Every side of every check receives a fresh source directory materialized from
verified blobs. Incumbent/candidate order alternates between cases. The frozen
operator script sits outside that directory and runs with the current Harness
Python interpreter using `-I -B`; its working directory is the selected source
tree. Import project code explicitly, as in the example (`'src'` for a source
layout). Candidate edits to repository tests do not replace this script.
Installed dependencies must already be available in that interpreter environment.
The record binds runner source, interpreter path/version and configuration;
it does not freeze or attest installed third-party dependency contents.

Children receive a small explicit environment, a temporary home and temporary
directory, without inheriting parent credentials or `PYTHONPATH`. Ordinary Git
discovery stops below the temporary root, so a check
cannot accidentally discover a parent repository when `TMPDIR` is inside one.
Time and output limits are separate from agent call budgets: these operator controls make no
inference calls and are not model tools. Defaults are 30 seconds per check,
600 seconds overall, and 65,536 combined stdout/stderr bytes per invocation.
Limits are explicit in the specification: `case_timeout_seconds` (up to 300),
`timeout_seconds` (up to 3,600), `max_output_bytes` (256 through 1,048,576), and
2–32 cases. Each case runs twice. Snapshot artifacts are limited to 24 MiB;
CLI proposal files are limited to 1 MiB. Oversize input is refused, not truncated.

The runner kills its owned process group and reaps its leader before recording
a terminal outcome or releasing temporary directories, including cancellation
during launch. Esc cancels the TUI operation; new conversational work retains the
existing priority behavior. Ctrl-C interrupts the CLI. An output overrun preserves
the bounded prefix with `output_limit`; a case timeout records `timed_out` and a
failed check. Overall interruption leaves unattempted measurements unknown.
Nonzero exit status is a failed check, even when stdout claims success.

The shared verdict rejects known regressions and critical failures, missing
measurements, unmet benefit thresholds and latency overruns. The report retains
each check's script, exit status, stdout/stderr, duration and process status, plus
both revisions, reconstructed candidate-tree digest and evaluator version.
Result inspection includes this report up to 1 MiB and explicitly points to the
retained blob for larger reports. Evidence IDs and the latest source plan remain
visible in `/improvements` after restart.

`EvaluationRunStarted` is persisted before checks; the existing result and terminal
records close the run after cleanup. Recovery marks an interrupted intent aborted,
or restores an already recorded result when its terminal was interrupted. Replay
never executes checks. A new explicit run can use the same frozen plan after the
original repository is moved or removed. Changing the runner/configuration requires
a new plan. Read-only inspection remains available for older generic code records.

## Authority and remaining work

Fresh directories provide edit separation, **not a security sandbox**. Check scripts
and imported candidate code execute with the operator's OS permissions, including
filesystem and network access. Processes that create separate groups or sessions
are outside the owned process group; source writes, disk consumption and CPU/memory
use are not OS-isolated.
Evaluate trusted code in an appropriate externally managed environment when needed.
Held-out partitions are operator declarations; this runner does not attest secrecy
or independence of cases. Candidate code could manipulate its own process behavior.

A passing result remains review evidence with `activation_qualified=false`. It
does not grant permissions, replace the running installation, select a prompt,
accept a task, or execute an adoption policy. Candidate changes to evaluator or
permission files remain candidate data and cannot authorize their own activation.
The default automatic-adoption policy remains empty.

This increment implements preparation, paired execution, inspection and recovery.
[Supervised source promotion and rollback](source-promotion.md) now provide
explicit selection and launch at a new-process boundary. Core-directed patch
authorship and stronger runtime isolation remain subsequent M5 work. No live model
quality or daily-use gate is established by these controlled source checks.
