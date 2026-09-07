# Core agency implementation — restart handoff

> **Latest continuation, September 7 — M3:** PR20 merged at `33a24fc`.
> Current branch: `feat/local-assistant-m3`. The [offline local assistant](docs/local-assistant.md)
> now passes the M3 workflow gate on the provisioned 8B CUDA profile, with plugins
> absent and normal memory enabled. It includes a runnable launcher, effective
> context-source provenance, explicit queue-pause handling and working fresh-install
> CLI grants. The 4B failure reports remain preserved. See the
> [current implementation record](docs/superpowers/plans/2026-09-06-core-agency-progress.md)
> for validation and the next M4 work. Earlier updates below are historical.

> **Continuation update, September 7:** [PR #19](https://github.com/c-daly/harness/pull/19)
> merged at `eb77885` after Python 3.12/3.13 CI and automated review passed.
> Implementation continues on `feat/resident-workflow`, based on that merge.
> [Resident continuity](docs/resident-workflow.md) now fetches explicit context
> sources once per root attempt through normal tool enforcement, retains a
> bounded previous-attempt brief, and joins task/context/local snapshots in
> `/status`. `harness status SESSION` inspects historical state without a provider
> or log repair. Source configuration survives resume and explicit overrides.
> Required-source failure stops inference; optional failure is visible. Result
> caps, total input limits, task/source deadlines and root tool budgets apply.
> Typed observations reference the existing session blob store. No context fetch
> creates an orphan conversation tool result or implicitly repeats in children.
> Expired permission dialogs are retired without dismissing another live prompt.
> PR20 review fixed status inspection immediately after resume: reasserting an
> unchanged policy retains recorded source results; actual policy changes still
> invalidate them. Six regressions cover CLI, repeated resume and the compositor.
> **Validation:** 1329 passed, 7 skipped, 6 warnings in 308.39s; locked sync,
> Ruff, whitespace, packaging and the 62-module fresh-wheel smoke passed. The
> [implementation record](docs/superpowers/plans/2026-09-06-core-agency-progress.md)
> also retains the restricted MCP-bind failures, host-overload test failures,
> initial red regressions and final packaging scope.
> The [real offline pilot](docs/handoffs/2026-09-07-resident-workflow/resident-workflow.json)
> **failed overall**. Both configured queries, fresh normal-memory retrieval after
> restart, visible status, real streaming cancellation (123 ms), preserved draft,
> stable task/profile and resumed factual answer passed. The initial write task
> failed to create the exact artifact and omitted the expected facts. The normal
> memory index was 13,802 bytes; these facts do not establish the failure's cause.
> The initial probe's cancellation-method error is retained separately. Neither
> run qualifies a fallback. The pilot hashes precede final permission-dialog
> cleanup and source-error hardening, which is covered by deterministic composed-terminal tests.
> Next: measure context relevance and local tool selection against exact project
> artifacts, then qualify a repeatable useful workflow. Continue UI onboarding,
> discoverability, heterogeneous-agent work and portable handoff. Do not substitute
> broader model installs or weaker checks for the failed workflow evidence.
> Larger models, Unsloth and Hugging Face remain on the candidate list. The prior
> local response candidate remains rejected (9/18 versus 14/18 incumbent).
> Automatic adoption policy remains unanswered; no fallback or improvement
> activation is enabled. M0–M4 remain in progress; M5–M6 pending.
> Memory and agent-swarm stay plugins. The user authorized commits, PRs and
> continued implementation. User `.claude/`, `.context/`, private memory and
> user-managed services are preserved. The inventory below is historical.

## Historical pre-reboot inventory

**Saved:** 2026-09-06. Work paused at the user's request to restart the machine.
**Workspace:** `/home/fearsidhe/projects/harness`
**Branch:** `feat/core-agency`, created from `main` at `ce722b4`.
**HEAD:** Still `ce722b4`; no implementation commits or pushes have been made.
**State:** Implementation changes are on disk, unstaged and uncommitted. Preserve them.

## Resume objective and authority

The user requested a new base branch from `main` and implementation of the
[core agency roadmap](docs/superpowers/plans/2026-09-06-core-agency-roadmap.md).
That implementation remains the active objective. The pause is for reboot,
not cancellation or a request to stop at the first tranche.

Read the roadmap and the
[implementation record](docs/superpowers/plans/2026-09-06-core-agency-progress.md)
before continuing. The [older hardening plan](docs/superpowers/plans/2026-09-03-production-beta-hardening.md)
provides detailed backlog tasks; the new roadmap supersedes its ordering and
universal-completion abstraction. See also [contributing](docs/contributing.md)
and [architecture](docs/architecture.md) for kernel invariants and review rules.

The latest user steering is explicit: **self-improvement must be a first-class
citizen in this effort.** The roadmap has been amended accordingly. It includes
evidence collection, candidates, bounded isolated experiments, evaluation,
adoption policy, safe activation, and rollback across M0–M6. Do not revert this
to a future-only feature or reduce it to an advisory memory note.

Other requirements to preserve:

- Harness remains the consistent assistant/interface across providers and agent
  runtimes. Project information and normal memory supply continuity.
- Distinguish raw model inference, bounded agent execution, and the continuing
  core assistant. Support all three; external CLI agents are agent runtimes.
- Basic agency, local inference operation, resource awareness, interaction/task
  state, routing, delegation, recovery, and self-improvement belong in **core**.
- `memory` and `agent-swarm` remain independent plugins. Core is useful without
  them and accesses configured normal memory through general contracts. Do not
  absorb their internals or create a competing private memory store.
- Support heterogeneous collections of agents, offline startup from provisioned
  assets, graceful resource changes, editable drafts, queued follow-ups, visible
  progress, interruption, recovery, and portable continuation outside Harness.
- Be candid about evidence. Unit tests, live capability proof, model quality,
  performance, and human dogfood are different gates.

## Outstanding product preference

An asynchronous question was asked and **no answer had arrived before this
handoff**:

> Which changes may the resident agent adopt automatically after evaluation?
> 1. Prompt/routing changes automatically; code patches require review.
> 2. Both behavior changes and code patches automatically.
> 3. Produce/test proposals; all adoption requires review.

The question concerns the product's adoption policy, not permission to continue
implementation. Do not treat a UI-preselected answer as submitted authorization.
Continue independent work and make policy explicit/configurable. Candidate
changes cannot grant their own permissions or change their own grading/adoption
authority. Source patches belong in isolated checkouts with versioned artifacts,
validation, safe promotion, and rollback, rather than opportunistic edits to the
running installation. Weight training is outside the current scope.

## Existing work and what must be preserved

The user-owned `.claude/` and `.context/` directories were already untracked
before this effort. They have not been modified. Do not reset, clean, stash,
stage, or overwrite them as incidental cleanup. The older hardening plan was
also already untracked; its content has been preserved.

Tracked files modified by this effort:

```text
docs/architecture.md
docs/contributing.md
src/harness/dispatcher.py
src/harness/events.py
src/harness/fold.py
src/harness/mcp_serve.py
src/harness/provider_antigravity.py
src/harness/provider_claude_code.py
src/harness/provider_codex.py
src/harness/sessions.py
src/harness/telemetry.py
src/harness/tui.py
tests/test_tui.py
```

New implementation/plan files, also untracked at handoff:

```text
.github/workflows/ci.yml
docs/superpowers/plans/2026-09-06-core-agency-roadmap.md
docs/superpowers/plans/2026-09-06-core-agency-progress.md
scripts/measure_local.py
src/harness/persistence.py
tests/test_runtime_lifecycle.py
tests/test_result_integrity.py
HANDOFF.md
docs/handoffs/2026-09-06-core-agency/   # saved probe evidence and file manifest
```

## Implemented and validated: first lifecycle change

- Added `ModelCallFailed` / `ModelCallAborted`; defaulted cancellation metadata
  and model-call purpose preserve old logs.
- Fold tracks `open_model_intents` separately. Resume repairs tool and model
  intents in proposal order, once, without replaying side effects.
- Dispatcher emits model terminal facts on policy denial, provider failure,
  timeout, and cancellation, including cancellation during permission waits.
  Permission waits record denial when cancelled. Provider generators close
  explicitly and ambient dispatch context is reset.
- Failure telemetry records a bounded generic error class rather than raw
  provider exception bodies that might contain secrets.
- All three CLI adapters use `running_tool_server()` with startup inside the
  cleanup scope. Generator teardown precedes MCP shutdown.
- `/compact` now calls the dispatcher with `purpose="compaction"`. It is
  enforced/accounted, but its completion is excluded from conversation folding
  and from the session's last conversational model identity.
- Telemetry has `TELEMETRY_SCHEMA_VERSION = 1`, pending and terminal model-call
  rows, schema mismatch rejection with rebuild guidance, an indexed-event table
  for idempotency, and atomic projection batches. Stats show terminal counts.
- Fixed the two pre-existing Ruff E741 variable names in the TUI test.
- Added CI for Python 3.12/3.13: locked dev installation, lint, tests, build,
  and wheel installation/help. Hosted CI has **not** run.

Validation actually completed:

| Check | Result |
|---|---|
| Original baseline suite, before runtime edits | 885 passed, 7 skipped, 4 warnings, 254.23s |
| New lifecycle regressions before implementation | 9 failed, 1 passed |
| New telemetry regressions before implementation | 2 failed |
| Focused lifecycle/fold/resume/events/telemetry/session tests | 90 passed |
| First full integration run after changes | 1 failed, 896 passed, 7 skipped; the compaction test assumed an unlogged internal call |
| Corrected compaction journeys | 5 passed; assertions now verify both accounting and transcript isolation |
| Latest completed integration set | **897 passed, 7 skipped, 4 warnings, 249.31s** |
| Ruff and `git diff --check` | Passed; Ruff rechecked at handoff |
| Offline sdist/wheel build | Both succeeded; installation smoke is still pending |

The latest integration command was:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --offline pytest -q -ra --disable-warnings --ignore=tests/test_result_integrity.py
```

**That is not a claim that every current test passes.** The excluded file was
authored independently for the next storage/result change and contains seven
known failing regression cases. It is not part of the validated lifecycle change.
Keep it out of a lifecycle-only commit, then include it when its implementation
passes. Do not weaken or delete those cases to obtain a green suite.

The seven persistent skips are three missing Anthropic conformance fixtures,
three missing Ollama fixtures, and one opt-in live Antigravity test. Local MCP
socket tests needed unrestricted localhost execution through the tool's approval
mechanism; sandbox socket denial is not a Harness defect.

The build landed under `/tmp/harness-core-agency-dist`; these artifacts may not
survive reboot and can be rebuilt. It was a packaging check, not an immutable
release-candidate qualification. A standalone untracked persistence helper was
present during that build and remains unqualified below.

## Next change: seven RED storage/result cases

`tests/test_result_integrity.py` was run and all **seven tests failed for the
intended reasons**:

1. Modified blob bytes are returned without integrity rejection.
2. A wrong recorded blob size is accepted.
3. A digest containing a path traversal is accepted by `BlobRef`.
4. Partial UTF-8 at a log tail raises before repair/quarantine can run.
5. Repair truncates the original file directly instead of atomically publishing
   a repaired copy after a durable quarantine.
6. Resume does not hold ownership across the initial read and sequence-number
   selection; the injected intervening writer demonstrates the race.
7. Large results cannot be consumed uniformly by inference and external-agent
   transcripts. `ToolOutcome.read_text()` does not yet exist.

`src/harness/persistence.py` has just been added as a **standalone, not yet wired
or behavior-tested helper**. It supplies directory fsync and atomic 0600 file
publication; exclusive publication uses a synced temporary file and hard link.
Review and test it before use. None of the seven defects is fixed yet.

Implementation direction considered, but not yet executed:

- Validate blob digest/size, reject corruption on reads and existing-content
  deduplication, and use durable atomic writes with temporary-file cleanup.
- Scan log bytes rather than decoding the whole file first. Preserve the
  intact prefix, quarantine the exact damaged bytes durably, then atomically
  publish repair. Keep the differing-quarantine refusal behavior.
- Hold one ownership lock across repair, read/fold, sequence selection, and
  writer construction. An advisory lock on a persistent inode can serialize
  stale-PID cleanup without unlink/recreate races; preserve compatibility with
  existing PID markers and live-lock rejection. Re-read under ownership before
  mutating a file initially scanned without a lock.
- Resolve tool-result blobs at the core model-context boundary and through a
  runtime `ToolOutcome` accessor for MCP. Keep the durable event blob-backed.
  Subscription transcript rendering must retain tool-call/result information.
  Do not substitute a placeholder or silently drop unsupported content.

After storage/result repair, continue cumulative nested authority and correct
parentage, root-shared limits, queue/draft/controller work, then M2–M6. The
roadmap is broader than the lifecycle change; no milestone is claimed complete.

## Local inference evidence and restart cautions

Before reboot, a **user-managed** Docker llama.cpp server was healthy at
`http://127.0.0.1:8080`; `/v1/models` returned
`unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS`. GPU: RTX 5070, 12,227 MiB.
Several configured catalog aliases point to that same endpoint. Do not assume
they are separately resident models or restart/stop the existing process as a
Harness-owned process. Recheck availability after reboot.

The initial probe used the flat schema form described in current llama.cpp
documentation. Six calls gave three timeouts and three JSON parse failures.
Explicitly disabling reasoning made two subsequent calls finish, but their
responses still lacked JSON conformance. A third, single-call probe using the
**nested** schema form returned the correct object in 7,906.88 ms:

```json
{"response_format":{"type":"json_schema","json_schema":{"name":"message_kind","strict":true,"schema":{"type":"object","properties":{"kind":{"type":"string","enum":["acknowledgement","question","stop","uncertain"]}},"required":["kind"],"additionalProperties":false}}},"reasoning_effort":"none","chat_template_kwargs":{"enable_thinking":false}}
```

This distinguishes a request-format issue from model quality. One correct
answer does not qualify latency or semantic decision reliability. Typing,
submission acknowledgement, cancellation, and explicit commands must remain
independent of model inference.

The current `scripts/measure_local.py` includes the corrected nested form and
records response metadata before JSON validation. **Its final corrected
six-call configuration has not yet been rerun.** After verifying service
availability, use:

```bash
python scripts/measure_local.py --samples 3
```

Saved probes are under
[`docs/handoffs/2026-09-06-core-agency/`](docs/handoffs/2026-09-06-core-agency/).
They use only public synthetic fixtures, no project content or credentials.
The nested-schema result is transcribed from the tool response and labeled as
such. The first two files are exact copies of the probe's JSON output formerly
under `/tmp`.

## Suggested resumption sequence

1. Read this file, the implementation record, and the amended roadmap. Check
   `git status` and the saved file hashes before any cleanup or edits.
2. If the adoption-policy answer arrived in the resumed conversation, incorporate
   it. Otherwise continue independent work; do not restart the entire design
   discussion or make all implementation depend on that preference.
3. Review the lifecycle diff for spec compliance and code quality. It is ready
   for a scoped commit once satisfied; storage tests/helper are separate WIP.
   No previous implementation commits need discovering or cherry-picking.
4. Implement the seven storage/result regressions, then run focused checks and
   the full suite **including** them. Proceed through the roadmap in reviewable
   changes; keep updating the implementation record with actual evidence.
5. Include self-improvement as core functionality throughout: persistent
   evidence/candidates, isolated experiments, evaluated adoption, rollback, and
   a usable interface. Do not use an LLM's self-score as its grading authority.

At handoff, no pytest, local probe, or build process from this task remained
running. The existing local model and other user services were left alone.
