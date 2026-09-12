# Harness remaining-work inventory (agent-swarm, 2026-09-12)

Produced by the agent-swarm pipeline (Stage 0) from the
[handoff brief](remaining-work-design.md) at `f8f7284`. Every claim below was
checked against the source tree at `origin/main` = `4c09803` (PR58 merged), the
implementation record, the retained handoffs and the unfinished
`feat/source-authorship` worktree. The inventory is the input to the
[implementation plan](remaining-work-plan.md); the plan's tasks are the
execution queue, and this file holds the crosswalk for everything else.

## Verified starting state

| Item | Observed |
|---|---|
| Remote | `origin/main` at `4c09803`; no open PRs; CI on main: PR58 push run succeeded (8m30s). |
| Control checkout | `.worktrees/remaining-roadmap-workflow`, branch `docs/remaining-roadmap-workflow`, one commit ahead (`f8f7284`, the brief). |
| Local `main` ref | Was 32 commits behind `origin/main` (`0184e00`); fast-forwarded with `git fetch origin main:main` before creating task worktrees. No checkout was switched. |
| Default checkout | `/home/fearsidhe/projects/harness` on `feat/resident-helper-measurements` at `e5a54c3`; untouched. |
| Source size | 91 modules, 24,227 lines under `src/harness/`; 149 test files, 35,447 lines under `tests/`. |
| Last complete suites | 2,358 passed on 3.12 and 3.13 (PR58 head), seven skips (Anthropic/Ollama fixtures, opt-in Antigravity), six MCP deprecation warnings. |
| Unfinished work | `.worktrees/source-authorship` on `feat/source-authorship` (base `4c09803`): 19 files staged, one unstaged test change, uncommitted. Checkpoint patch `sha256 b2679a45…` retained under `.worktrees/tmp/agent-swarm-roadmap-handoff-20260912/`. Three RED tests: `test_partial_limit_overrides_keep_author_defaults_in_the_shared_command[limits0..2]` fail with the author-limit `ValidationError` because a partial `limits` object is validated with generic `TaskLimits` defaults. |
| Locked envs | `.worktrees/tmp/source-authorship-py31214` (3.12.14) and `…-py31315` (3.13.15), both importing harness from the source-authorship worktree. |
| Host | 16 CPUs, 21 GiB RAM, RTX 5070 12 GiB (4 GiB already in use by another process at inspection), `/tmp` 8.3 GiB free, project disk 482 GiB free. `bwrap` and Docker availability recorded in the workflow status file. |
| Local models | Qwen3-8B Q4_K_M and Qwen3-4B-Instruct Q4_K_M with llama.cpp b9603 under `.local-runtime/`; normal catalog aliases `local` (port 8080) and `local-instruct` (port 8081) with on-demand profiles. |
| Plugins | Installed memory and agent-swarm plugins under `~/.claude/plugins/`; the repository ships only the reference `plugins/memory`. Core references agent-swarm only through an optional TUI activity-panel section that calls `mcp__agent-swarm__workflow__workflow_get_state` when an MCP server named `agent-swarm` is enabled. |

## Milestone status against the roadmap gates

| Milestone | Gate status | Evidence and what remains |
|---|---|---|
| M0 baseline | Mostly met; UI latency trace missing | CI, lint, packaging and wheel smoke exist and run on every PR. Catalog entries carry `execution_kind`. Local feasibility was measured (8B CUDA). No compositor latency trace has been recorded (that is the M6 playability measurement). |
| M1 correctness and interaction | Implemented for exercised paths; qualification open | Storage/blob integrity, torn-tail repair under the advisory guard, single terminal facts for model and tool intents, audited compaction, nested scope/limit propagation, the shared prompt controller, argument validation and bearer-protected outward MCP are in source. Remaining: pattern redaction with recorded outcomes, minimal child environments, provider conformance corpus, fault qualification (see hardening tasks 8, 10, 11A). |
| M2 inference/agent contracts | Inference and native tasks done; two adapters and richer evidence open | `dispatch_inference`, `AgentTask`/`AgentRuntime`, Codex task binding, nullable usage, improvement records and exact-byte task checks exist. Remaining: typed task bindings for Claude Code and Antigravity (both still use the legacy conversation bridge), workspace-level artifact predicates and natural requirement proposals, live capability qualification. |
| M3 local assistant | Complete for the measured 8B CUDA profile | Six offline journeys and four recovery cases pass repeatedly. Outside the gate: managed weight download and runtime installation, split GGUFs, CPU qualification, larger-model capacity measurement. |
| M4 semantic agency and resource changes | Machinery complete; every quality gate failed; correction loop missing | Three shadow functions, paired evaluation with held-out minimums, supervised prompt lifecycle per function, task-preserving fallback, device-group scheduling, external reconciliation/handoff, memory-loss/destination-loss/busy/Esc journeys and core context eligibility exist. Remaining: genuinely held-out semantic confirmation (all development trials failed the critical ambiguity case), the explicit user-correction-to-repair loop, an expired-credential journey and an MCP-loss journey recorded as fault journeys. |
| M5 heterogeneous work and plugins | In progress | Typed coordination outcomes, coordinator admission and deadlines, native file ownership and stale-read refusal, evidence-gated escalation and checked ensembles, usage stop limits, execution controls, live activity, timer grants, durable admission counts, targeted cancellation, live mixed-runtime journey (failed local artifact gate), portable export with an independent consumer, isolated source experiments, supervised source promotion/rollback. Remaining: source authorship completion (unfinished branch), installed memory + agent-swarm workflow reconciliation, cross-process edit ownership / isolated worktrees, progress-sensitive supervision and suspected-stall handling, exact pre-call usage reservations, provider/member timer extension, task-specific acceptance and recovery for wrong participant results, recursive child artifacts in continuation packages, live heterogeneous supervision/accounting qualification. |
| M6 daily-use qualification | Pending | Nothing in M6 has run. See the hardening crosswalk rows 15–19C. |
| Self-improvement (roadmap gates) | M4 gate implemented (hold path proven); M5 gate one increment from implemented; M6 gate not exercised | Repeated invalid output → candidate → paired evaluation → hold/adopt/rollback exists for prompts. Source experiments, promotion and rollback exist; agent-authored source patches are implemented but uncommitted. No improvement has produced a measured useful local quality gain. |

## Hardening backlog crosswalk (tasks 1–19C)

Dispositions: **implemented** (in source at `4c09803`), **partial** (named gaps), **remaining** (a plan task exists or is listed below), **qualification-only** (needs live/hardware/human evidence, not code), **deferred** (explicitly out of scope per the roadmap or hardening plan).

| Task | Disposition | Verified detail |
|---|---|---|
| 1 CI/package baseline | implemented | `.github/workflows/ci.yml` runs lint, tests, build and wheel smoke on 3.12/3.13; `tests/test_package_metadata.py` exists. |
| 2 terminal facts/cleanup | implemented | `ModelCallFailed`/`ModelCallAborted`, shared MCP lifecycle context manager, versioned telemetry. |
| 3 request/status contract | reworked → partial | Replaced by inference/agent contracts (`inference.py`, `agent.py`, `agent_runtime.py`). No `ProviderStatus`/`ProviderCapabilities`; capability snapshots exist only for the Codex runtime binding. Plan task: Claude Code and Antigravity bindings. |
| 4 execution profiles | remaining (M6) | No `runtime_policy.py`, no `ExecutionProfile`. Roadmap: "M1 for exercised paths, M6 qualification". Plan task in the trust wave. |
| 5 compatibility/doctor | partial | `harness models`, `harness resources`, catalog execution kinds exist. No `doctor`, `provider_compat.toml`, `release_support.toml`, stable exit-code table or `cli_output` seam. Plan task in the trust wave. |
| 6A blobs/artifacts/results | implemented (core) / partial (typed artifacts) | Digest/size/identity verification, corrupt-object refusal, sidecar materialization. No `ArtifactRef`/`ToolResult` structured results; MCP image/audio/resource content is still rejected rather than preserved. Listed as remaining-later; not queued this run. |
| 6B bounded transcripts | partial | External bridge carries tool names/IDs/args/results; context profiles bound input bytes; no `provider_transcript.py` with per-result caps and recorded transforms. Remaining-later. |
| 7 tool validation/risk | partial | Final arguments are schema-checked; no `ToolRisk`/source metadata or MCP invalid-schema quarantine. Remaining-later. |
| 8 conformance corpus | partial / qualification-only | `tests/conformance` parametrizes recorded fixtures; only `tests/fixtures/openai` exists, so Anthropic/Ollama scenarios skip. Recording real streams needs credentials outside CI. |
| 9A filesystem containment | remaining (M6) | No `process_sandbox.py`; bubblewrap availability recorded in the status file. Plan task in the trust wave. |
| 9B CLI containment classification | qualification-only after 9A | Requires live probes per installed CLI version. |
| 10 storage/locking/repair | partial | Advisory `SessionLock`, atomic torn-tail repair and blob verification exist. No `verify_session`/`IntegrityReport`, `RepairAuthorization`, storage probes or `StorageLimits`. Plan task: session integrity CLI. |
| 11A redaction/environments | remaining | `redaction.py` is the identity seam; no `PatternRedactor`, `RedactionOutcome`, `process_env.py` or `security.toml`. Plan task. |
| 11B authenticated outward MCP | partial | Random per-server bearer capability in the URL, origin rejection. Claude Code receives it by config file; Codex passes it in argv (`-c mcp_servers.harness.url=`); Antigravity passes it to `agy mcp add` argv. Plan task (after adapter bindings). |
| 12 grants/plugin trust | remaining (M6) | No `plugin_trust.py`, `plugin_digest` or `PluginTrustDecided`; grant persistence path unchanged. Plan task in the trust wave. |
| 13 budgets/concurrency | implemented (different names) | `ExecutionBudget`, `ExecutionLimits`, `UsageBudget`, coordinator admission, durable counts. Exact pre-call reservations remain (plan task). |
| 14 loss-aware projections | remaining-later | No `projection.py`/`GapNotice`; live activity tracker exists. Not queued this run. |
| 15 identity/onboarding/switching | partial | Turn-boundary model switching and saved selection exist; no onboarding modal, no identity view-model. Plan task (UI wave). |
| 16 composer/permission review | partial | Drafts, queue and permission dialogs exist; no multiline `PromptComposer` or `PermissionPresentation`. Plan task (UI wave). |
| 17A discoverability | partial | `/help`, `/tools <name>`, `/model` text listings; no searchable palette/picker/browser screens. Plan task (UI wave). |
| 17B inspection/export/recovery | partial | `/export`, `harness export`, session picker and read-only inspectors exist; no `sessions verify/repair/trash/restore/purge`. Plan task with 10. |
| 18A compositor/accessibility | partial | Final-compositor checks at 52/60/80/120 columns are widespread; no golden set, no no-color/high-contrast matrix. Plan task (UI wave). |
| 18B plain terminal | remaining (M6) | No `plain.py`. Plan task (UI wave). |
| 18C playability | partial | Bounded queue, `/queue`, visible phases and elapsed time exist; no `ActivityPhase` table, `InteractionTrace`, `measure_playability.py` or dogfood protocol. Plan task (UI wave). |
| 18D evidence renewal | qualification-only | Requires 8/9A/9B evidence first. |
| 19A/19B/19C release | deferred until M6 gates pass | Per roadmap crosswalk; no release tooling exists. |

## Remaining items by disposition

### Queued in this run's plan (implementation)

1. Source authorship completion and publication (unfinished branch, partial-limit defect, honest measurement reconciliation).
2. Sustained multi-session scenario: frozen criteria, independent oracle, scripted driver, failure taxonomy, human protocol.
3. Installed memory + agent-swarm workflow reconciliation contract and journey.
4. Progress-sensitive supervision: suspected-stall observation and recorded operator decisions.
5. Exact pre-call usage reservations against the shared usage budget.
6. Typed task bindings for Claude Code and Antigravity.
7. Outward MCP capability delivery without argv exposure (Codex, Antigravity).
8. Pattern redaction with recorded outcomes and minimal child environments.
9. Session integrity verification, authorized repair, storage limits and lifecycle CLI.
10. Task-specific acceptance and recovery for incorrect participant results in coordinations.
11. Cross-process edit ownership through core-managed isolated worktrees for delegated tasks.
12. User-correction-to-repair loop feeding the improvement journal.
13. Workspace artifact predicates and natural-language requirement proposals for task evidence.
14. Recursive child artifacts in portable continuation packages.
15. Managed weight download and runtime installation with pinned provenance.
16. Multiline composer and complete permission review.
17. Command palette, model picker, tool browser and first-run onboarding.
18. Plain interactive terminal frontend.
19. Compositor golden set, terminal-size matrix and no-color/high-contrast checks.
20. Playability measurement, activity state table and dogfood protocol.
21. Diagnostics: `harness doctor`, provider probes, compatibility manifest and stable exit codes.
22. Execution profiles and recorded trust boundary.
23. Filesystem containment primitives (bubblewrap) with live boundary tests.
24. Plugin digest trust and atomic grant storage.

### Remaining later (implementation not queued this run)

- Typed structured tool results and MCP media preservation (6A remainder).
- Bounded provider transcript renderer with recorded transforms (6B).
- Tool risk/source metadata and MCP schema quarantine (7).
- Loss-aware subscriber projections (14).
- Provider and member-agent timer extensions (M5).
- Split GGUF support and larger-model capacity optimisation (M3 candidates).

### Qualification-only (needs live models, hardware, credentials or humans)

- Held-out semantic confirmation (M4) with a distinct frozen hypothesis; all development trials failed.
- Larger-model capacity experiment (Qwen3-14B) per `docs/local-model-candidates.md`.
- Recording real Anthropic/Ollama/CLI streams for the conformance corpus (8); live containment classification (9B); evidence renewal (18D).
- Live mixed-runtime rerun with acceptance recovery; live installed-plugin workflow with the real agent-swarm server.
- M6 dogfood journeys, offline cold start with memory, resource-loss handoff, plugin removal/portable continuation, 90-minute session, clean-setup user, wall-clock latency budgets on the reference machine.
- Sustained scenario live sessions and human evaluation.

### Human/release gates (user-owned)

- PR merges, release candidate construction (19B), tagging/promotion (19C), publication decisions.

### Explicitly deferred

- Multi-user daemon/service, native Windows/macOS, untrusted-plugin sandboxing, PyPI publication, distributed orchestration, session labels/pins, Antigravity production containment claims, weight training/fine-tuning (Unsloth), open-ended agent society, workflow language, resident model per role, broad provider matrix.

## Execution constraints carried into the plan

- Base every task branch on `origin/main` (`4c09803`) except source authorship, which continues the existing worktree.
- Dependencies are satisfied by merging the prerequisite task branch into the dependent worktree before its worker starts; the runner's dependency status alone does not import code.
- The runner's automatic merge-to-main and forced worktree cleanup are not invoked. Workers push branches and open PRs; the user merges.
- Complete 3.12/3.13 suites, Ruff, packaging and installed-wheel smoke run at each PR boundary with frozen sources. Full suites and local-model trials are not run concurrently.
- Repeated experimental failure is recorded as a finding; gates are not loosened.
