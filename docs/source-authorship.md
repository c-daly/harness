# Bounded source authoring

`/improvements source-author SPEC.json` runs a core native authoring task using
the selected inference model. It proposes one source patch through the normal
dispatcher. Local GGUF models can supply that inference through their existing
catalog profiles. External coding-agent aliases are not accepted by this path.

The operator chooses the incumbent, editable files, evidence and fixed checks
before inference. The model receives the request, selected UTF-8 source text and
recorded evidence observations. Normal configured context sources, including
memory, run through their existing permissions and budgets. Their content is
data; memory ownership remains in the plugin. Required context failures stop
authoring, while optional failures remain visible.

No tools are advertised to the author. It returns full replacement text for
selected files, or null for a deletion, along with a hypothesis and expected
benefit. Core rejects malformed, duplicate, unchanged and out-of-scope edits,
path traversal and file/directory collisions. Existing executable flags stay
intact; additions are non-executable. Binary files can remain in the incumbent
but cannot be selected for this text-authoring path.

## Prepare an authoring specification

First inspect `/improvements` and choose existing evidence IDs. For a project
with `calc.py` defining a broken `double(x)`, a minimal specification is:

```json
{
  "repository": "/absolute/path/to/project",
  "incumbent_revision": "COMMIT_OR_LOCAL_REF",
  "request": "Fix double(x) so it returns twice its argument. Preserve unrelated behavior.",
  "editable_paths": ["calc.py"],
  "evidence_ids": ["EXISTING_EVIDENCE_ID"],
  "suite": {
    "cases": [
      {
        "id": "existing-input",
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
}
```

The file can declare up to 32 editable paths and 32 unique evidence IDs. A path
absent from the incumbent declares an allowed addition. Only selected files go
to the model; include sufficient source context in that selection. Every
selected path is editable, so avoid listing unrelated sensitive files merely
for reference. Dirty/untracked work is excluded from committed snapshots and
never modified. Git object fetching, dependency installation and commits are
not part of authorship.

To continue from an authored or previously selected snapshot, replace
`repository` and `incumbent_revision` with an `incumbent` object containing the
exact `sha256` and `size` from its recorded snapshot reference. The blob must
already exist in this session. This supports successive source improvements
without pretending a generated content revision is a Git commit.

The `suite` uses the [source evaluation](source-improvement.md) schema and
limits. Its scripts and gates are frozen before the model call and are omitted
from the author request. Held-out status remains an operator declaration;
source and normal memory could already contain information about these checks.
The default benefit/latency gates still apply; an equivalent rewrite does not
count as improvement.

Optional `limits` use the core task schema. This author allows one inference
request, at most 256 KiB input (including schema/context), 128 KiB response,
16,384 output tokens and 16,384 stream chunks. The default output token budget
is 8,192. Smaller configured task/context limits still apply. Time budgets are
explicit execution controls, not hang detection. A large file may exceed the
model's context or response capacity even within these byte limits; refusal or
incomplete output does not create a candidate. Provider retry policy remains
the ordinary dispatcher policy.

## Author, inspect, evaluate

```text
/model LOCAL_OR_REMOTE_INFERENCE_ALIAS
/improvements source-author /path/to/author.json
/improvements show AUTHOR_ID
/improvements show CANDIDATE_ID
/improvements show PLAN_ID
/improvements source-evaluate PLAN_ID
```

The headless author command uses the existing inference-enabled improvement
frontend:

```sh
harness improve --base-dir DATA --catalog MODELS.toml --model ALIAS \
  --allow 'model:ALIAS' SESSION source-author /path/to/author.json
harness improvements --base-dir DATA SESSION --show AUTHOR_ID
harness improve-source --base-dir DATA SESSION evaluate PLAN_ID
```

The alias is pinned for this proposal; routing cannot silently substitute
another model or an external agent. An unavailable local model produces a
recorded failed attempt. Choose another alias explicitly for a new authoring
request. Headless plugin loading remains the existing `harness improve`
behavior: a saved required source whose tool is not installed in that frontend
will block the call. Use the configured TUI for installed-plugin context.

Each `SourceAuthoring` intent retains the operator specification, incumbent,
fixed suite/evaluator, model and author implementation digest. The ordinary
`AgentRunStarted`/`AgentRunFinished` and model/context events retain execution,
usage and original provenance. Author requests and responses stay outside the
conversational transcript and do not accept or replace the selected user task.

The generated candidate is an immutable source patch in the blob store. Its
`origin` is `authored` and its revision identifies generated content; committed
snapshots have `origin=git`. Original source directories and the running
installation are untouched. Evaluation and selected-source launch materialize
their own independent filesystem copies from verified blobs.

## Recover and promote

Esc in the TUI or Ctrl-C in the CLI cancels the owned task and settles its
inference stream before the terminal outcome. Restart aborts an unfinished
intent without rerunning it. Inspect the author ID to see execution status,
retained response and whether a candidate was published. A completed model
response is still unverified and may contain an invalid patch.

If the response completed but candidate/plan publication was interrupted:

```text
/improvements source-finalize AUTHOR_ID
```

```sh
harness improve-source --base-dir DATA SESSION finalize AUTHOR_ID
```

Finalization revalidates the recorded response against the frozen scope. It
reconstructs the same candidate/plan IDs and exact artifacts, makes no model
call, and is idempotent. Failed, cancelled, aborted, missing or ambiguous task
outcomes cannot be finalized. Corrupt artifacts are refused. A changed
evaluator can leave the recovered plan inspectable but ineligible to run;
prepare a new experiment with current checks instead.

Successful authoring does not execute checks or select code. Use the existing
[supervised adoption, rollback and fresh-process launch](source-promotion.md)
after reviewing a passing paired result. Changes to candidate permission or
evaluation code do not replace the management installation's controls. There
is no automatic adoption, shell-based repository exploration, security sandbox,
dependency attestation, or general coding-quality qualification in this increment.

The opt-in measurement driver is `python -m scripts.measure_source_authorship
--catalog MODELS.toml --model LOCAL_ALIAS --output NEW_DIRECTORY`. Repeating
`--model` measures each selected local alias on all three seeded defects. It
retains failures, requests, paired checks and supervised launch/rollback results.
