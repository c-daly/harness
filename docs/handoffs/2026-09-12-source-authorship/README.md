# Local source-authoring measurement

The core can now run a bounded source-author task, validate its proposed patch,
evaluate fixed checks and exercise explicit adoption/rollback in an isolated
session. These trials demonstrate that workflow and also show why these local
models cannot be treated as reliable source improvers.

Three small maintenance defects were seeded by the operator: Unicode byte
truncation, an inclusive retry-limit comparison and interpreter normalization
that erased virtual-environment identity. The last reflects the failure class
encountered during PR58; this fixture is a small reproduction, not an experiment
in which a model independently discovered or repaired the full Harness project.
Each defect first failed its actual frozen check. Models received the request,
source file and failure observation, but not the check script. They received
one proposal request each. No prompt tuning, answer injection or grading changes
followed the results.

| Model/case | First run | Diagnostic repeat on final source |
|---|---|---|
| Qwen3 4B Instruct, UTF-8 prefix | Malformed structured response | Startup timeout; no inference |
| Qwen3 4B Instruct, retry boundary | Passed checks, selected launch and rollback | Passed checks, selected launch and rollback |
| Qwen3 4B Instruct, interpreter identity | Malformed structured response | Malformed structured response |
| Qwen3 8B, UTF-8 prefix | Valid patch; failed behavioral checks | Startup timeout; no inference |
| Qwen3 8B, retry boundary | Passed checks, selected launch and rollback | Startup timeout; no inference |
| Qwen3 8B, interpreter identity | Claimed a fix but returned unchanged source; refused | Claimed a fix but returned unchanged source; refused |

The first run had two measured improvements among six attempts. The diagnostic
repeat had one, with three attempts failing the configured 60-second startup
budget before the inference adapter received a request. Those are availability
outcomes, not evidence about coding ability. The host load was not controlled;
latency differences across these runs do not qualify model speed or compare
model quality statistically.

The captured 4B interpreter response used an object keyed by filename instead
of an array of `{path, content}` edits. Its code also retained symlink resolution
despite claiming to preserve environment identity. The 8B UTF-8 patch had a loop
boundary error and failed the fixed Unicode cases. Both 8B interpreter responses
claimed an improvement while reproducing the original code. Schema validation,
empty-edit rejection and behavioral checks correctly kept these proposals out
of source selection. Only the passing retry-boundary candidates were selected
by the operator-authored measurement driver; each selection was rolled back.

The [retained report](report.json) includes both runs, original report hashes,
frozen specifications, requests, responses where available, usage attempts,
paired check outputs, launch/rollback records and all management-source/driver
hashes. All 93 current core file hashes and the final driver match run v2.
The initial driver used `authorship=not_started` for some attempts that had
actually failed; `normalized_authorship` derives their proper status from the
retained agent terminal records without changing the original reports.
The diagnostic driver records those statuses directly and retains bounded raw
response previews. Raw malformed response text was not captured in run v1.

Full journals, blobs, original repositories, private/untracked fixture files and
independent launch copies remain under:

```text
.worktrees/tmp/source-authorship-measurement-v1/
.worktrees/tmp/source-authorship-measurement-v2/
```

All twelve source-preservation checks passed. These runs used existing local
catalog aliases `local-instruct` and `local`, backed by provisioned GGUF models
and llama.cpp on the local machine. No user-managed service was stopped, no
weights were downloaded or trained, and no live installation was changed.
Normal-context behavior is covered by fixture-based tests; these measurements
ran without plugins and do not qualify installed memory/swarm integration.

To reproduce into a fresh output directory with the recorded driver:

```sh
python -m scripts.measure_source_authorship --catalog MODELS.toml \
  --model local-instruct --model local --output NEW_DIRECTORY
```

Keep the exact catalog and runtime available locally. The generated candidates
and check scripts run with operator OS permissions during evaluation/launch;
fresh source directories provide edit separation, not a security sandbox.

This closes a narrow, supervised native-authoring workflow. Iterative repository
exploration, reliable local coding, stronger runtime isolation and broader M5/M6
qualification remain open.
