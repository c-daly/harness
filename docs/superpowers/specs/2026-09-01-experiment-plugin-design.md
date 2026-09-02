# Experiment plugin — Design

Date: 2026-09-01 · Status: approved in direction (Chris, 2026-09-01: scope,
driving, identity, home; design sections approved 2026-09-01) · Branch:
feat/experiment-plugin

The experiment plugin is a constellation plugin (constellation plan item 8.5
and Wave G, "Experiment-as-Plugin") that has been parked inside the agent-swarm
repository as `lib/experiment_*.py`, `config/workflows/experiment.yaml` and
`skills/experiment/`. This design lifts it into harness as a native plugin. It
does not touch the agent-swarm repository, and it does not touch, reverse or
qualify the 2026-08-31 "agent-swarm is subtracted, not ported" decision, which
concerns agent-swarm's Claude Code workaround machinery, not experiment.

The draft of this design went through an adversarial review (four lenses,
each refuted, plus a completeness critic) before Chris saw it; the confirmed
findings are folded in below and the review transcripts are in the session
record.

## 0. Decided by Chris (not re-litigated here)

1. **Scope of v1:** the single-run loop (read, plan, work, eval, journal,
   decide) plus the (reader, writer) run store, working against the existing
   `goal.yaml` + `eval/` experiment directories. Phase-gated tool restrictions
   become a harness dispatch hook. The arms x trials x compare coordinator is
   a follow-on.
2. **Driving:** model-driven, no harness kernel change. A command starts a run;
   the model advances phases through plugin tools; the dispatch hook enforces
   phase rules. Works in the TUI across turns and headlessly with `--allow`
   grants. An autonomous multi-turn driver is a harness-level follow-on.
3. **Identity and home:** experiment is its own plugin, not agent-swarm. Code
   lives at `harness/plugins/experiment/` next to the golden memory plugin;
   docs live in a new vault entity `10-projects/experiment/`.

## 1. Layout

```
plugins/experiment/
  plugin.toml              hooks module; dispatch hook phase_gate (priority 990);
                           lifecycle hook session_start; MCP server "experiment"
  README.md
  server.py                FastMCP server: the tool surface (section 5)
  engine.py                the loop: phases, transitions, gates, state.json, evals/ records
  evals.py                 Goal/Constraints loading, criteria validation, run_eval, check_criteria
  store.py                 JournalProvider contract + file and vault providers (frozen layout)
  policy.py                phase deny table + protected paths, as data validated at import
  hooks.py                 phase_gate (stdlib + harness imports only); session_start capture
  skills/experiment.md     the protocol
  commands/experiment.md   /experiment <experiment_dir> [--max-iterations N]
  agents/experiment-worker.md   a subagent definition with a narrowed tool list
```

Sibling modules self-load via `importlib.util.spec_from_file_location` (the
memory plugin's documented pattern). `hooks.py` imports nothing but the stdlib
and `harness.*`; the policy table is validated at import, so a broken table
fails at `load_plugins`, never at dispatch. `server.py` imports `engine.py`,
`evals.py` and `store.py` and needs `mcp` and `pyyaml` under whatever
`python3` is first on PATH, exactly like the memory server (under
`uv run harness` that is the harness venv; both are harness dependencies).
There is no `phases.toml`: the policy is a Python constant, because a data
file parsed at call time would turn a typo into a session-wide fail-closed
denial. The ported skill's frontmatter carries only `name` and `description`
(harness frontmatter forbids unknown keys, and the parked file has
`user_invocable`).

## 2. What is ported, what changes, what is dropped

| parked (agent-swarm repo) | here | status |
|---|---|---|
| `experiment_harness.py`: Goal, Constraints, EvalResult, `run_eval`, `check_criteria`, `[METRIC]` parsing, pytest summary parsing | `evals.py` | ported; `Journal` dropped, the store's `record_observation` is the journal (closes the migration deferred on 2026-08-13) |
| `experiment_workflow.py`: transition table, iteration counting on decide->plan, cap, decide->done recompute gate, `best_metrics` direction, `record_hypothesis` | `engine.py` | ported; every daemon call becomes a read/write of `state.json`; CLI `main()` dropped; `execution_mode`, `environment` (as loop state) and `max_agents` dropped |
| `experiment_store.py`: contract ABCs, `LocalFsExperimentStore`, frontmatter observation format, `validate_experiment_name` | `store.py` | ported byte-for-byte on disk as `FileJournalProvider`; `MemoryMirrorWriter` and `MemoryPluginSink` **deferred, not dropped** (section 12); `VaultExperimentStore` becomes `VaultJournalProvider` (section 4) |
| `experiment_server.py`: six contract tools | `server.py` | the six tools keep their frozen names and signatures; loop tools are added beside them; per-project multiplexing replaced by an injected `store_root` |
| `experiment.yaml` + `permissions.yaml experiment:` | `policy.py` | re-expressed as deny lists (section 7); differences listed there |
| `skills/experiment/SKILL.md` | `skills/experiment.md` | ported; team/fan-out and remote-environment sections dropped; additions listed in section 8 |
| daemon, router, `DaemonClient` | nothing | harness has no daemon; the run directory is the state |
| `run_scorer.py`, `scripts/format_experiment.py` | not ported | cost and attribution come from harness telemetry; outcome recompute lives in the gate; the format script is unrelated |

Deliberate behavior changes, each restated where it applies: `success_criteria`
are read from `goal.yaml` (the parked skill never passed them, so its gate was
vacuous); criteria are validated at start with an alias map; `goal.environment`
reaches the eval; derived `test_pass_rate`; a second `experiment_start` while a
run is open refuses instead of overwriting; the default `max_iterations` stays
10; `HARNESS_EVAL_TIMEOUT` is replaced by `goal.eval_timeout_s`; refusals are
`isError` results.

## 3. Processes and the run record

Two processes, one truth on disk:

- `hooks.py` runs in the harness process: it knows the environment, the cwd,
  the owning session id, and sees every `ProposedToolCall` before dispatch.
- `server.py` runs as an MCP child with the SDK default environment (HOME,
  PATH, ...), so it reads no configuration from the environment; everything
  it needs arrives as tool arguments.

```
<store_root>/<experiment>/runs/run-NNN/
  run.json          frozen 7-key contract record (unchanged from the parked store)
  state.json        loop state (below); additive sibling the CC-hosted store never reads
  journal/          NNN_<slug>.md observations, frontmatter canonical (unchanged)
  evals/            NNN.json + NNN.out per eval run, append-only history
  methods.md        materials and methods, written once at start (below)
  goal.yaml         verbatim copy of the experiment's goal.yaml at start
  constraints.yaml  verbatim copy when the experiment has one
<store_root>/<experiment>/experiment.md
                    the experiment's landing page: question, description, methodology, runs table

$XDG_STATE_HOME/harness/experiment/active/<session_id>.json
                    {run_id, run_dir, experiment_dir, session_id, provider description
                    (provider, root, and for the vault provider vault_dir + project)} while a run is open
```

`state.json`: `phase`, `iteration`, `max_iterations`, `active`, `exit_reason`,
`exit_note`, `experiment_dir` (resolved absolute), `experiment`, `task`
(goal objective), `success_criteria` (validated, normalized), `eval` (the
resolved eval spec frozen at start), `eval_python`, `environment`,
`protected_digest`, `hypotheses_tested`, `best_metrics`,
`last_eval_metrics: null`, `last_eval_passed: null`, `last_eval_index`,
`eval_recorded_this_visit`, `eval_in_flight`, `session_id`, `started_at`,
`ended_at`. `null` versus `{}` for `last_eval_metrics` is load-bearing: the
gate says "no eval result recorded" for `null` and "criteria not met" for `{}`.

**The record describes the experiment and its method.** `goal.yaml` may carry
`description` (what the experiment is and why) and `methodology` (how it is
measured and the procedure) beside `objective` and `context`; an experiment
`README.md` stands in for `description` when that field is absent. At start the
engine writes `runs/run-NNN/methods.md` once and never changes it: canonical
frontmatter (run, started, session, iteration cap, eval form and command,
interpreter, timeout, environment names, criteria, protected-file count,
plugin version) and a materials-and-methods body: Question, Description,
Methodology (the author's prose, then a generated Measurement subsection with
the eval command, interpreter, timeout, a criteria table with direction, primary
and report-only flags, the protected files with digests, and the environment),
Constraints, Provenance. The run directory also receives verbatim copies of
`goal.yaml` and `constraints.yaml`, so a run reproduces from its own directory
even after the experiment is edited. Per experiment,
`<store_root>/<experiment>/experiment.md` is refreshed at every start and end
with the current question, description and methodology and a Runs table
(outcome, iterations, best value per criterion). In vault mode it is the
experiment's landing page beside its runs. Both files are invisible to the
parked reader.

**The journal is the coherent record.** An observation recorded on a loop run
carries engine-owned facts the model cannot write or alter: `iteration`,
`eval_index`, `metrics`, `passed`, `criteria` (per-criterion actual, threshold,
met) and, when the model left `hypothesis` blank, the hypothesis recorded at
entry to work. They are additive frontmatter keys plus an "Eval
(engine-recorded)" body section rendered from them, so a human reading the
markdown sees the verified numbers next to the model's diagnosis, and a
disagreement between the model's `result` prose and the engine's metrics is
visible on the page. Entries written through the raw writer carry no such keys
and are byte-identical to the parked format; the parked reader ignores the
extra keys on loop entries.

Invariants:

- **A pointer exists iff the run is open** (`run.json.ended_at` is null).
  Every end path clears it. Pointers live outside the record tree so the
  store stays plain, inspectable data with no runtime files.
- **A run is bound to the session id that started it.** Resuming a session
  under its original id (`harness --resume`) finds its pointer and the gate
  continues in the recorded phase; any other session must call
  `experiment_resume`.
- **`state.json` wins over the log.** After a crash between the server's
  write and the `ToolCallCompleted` event, the run directory is the truth;
  the event log is the history.
- All state writes are atomic (tmp + rename).

## 4. Where the record lives, and how the server learns it

**The journal has a provider.** The record (runs, observations, methods pages,
the experiment page) is written and read through a `JournalProvider`: the
(reader, writer) contract plus the run files and the experiment page. Two ship:
`FileJournalProvider(root)`, the default, rooted at an explicit path or the
environment root; and `VaultJournalProvider(vault_dir, project)`, rooted at
`<vault>/10-projects/<project>/experiments/` (the layout the parked store used),
which requires the entity to exist (the plugin never creates a vault entity) and
stamps `experiment.md` with vault frontmatter (`project`, `type: experiment`,
`updated`). Anything somebody writes and registers in the provider table is a
third option; the frozen contract is the interface, and a provider is built from
the keys of its `journal` block. The plugin itself is the experiment; the
journal is the pluggable part (Chris, 2026-09-02).

**Selection is per experiment, in `goal.yaml`:**

```yaml
journal:
  provider: vault        # or file (the default when the block is absent)
  project: LOGOS         # vault only; the entity must exist
  root: ../journal-out   # file only; optional, relative to the experiment dir
```

With no block, the environment decides: `HARNESS_EXPERIMENT_PROVIDER` (`file`
unless set to `vault`), `HARNESS_EXPERIMENT_DIR` (the file root, default
`~/.local/share/harness/experiments`), and for a vault default `MEMORY_VAULT_DIR`
with `HARNESS_EXPERIMENT_PROJECT`. A machine can default to the vault while the
shipped default stays the filesystem. `experiment_start` echoes the provider,
its root, and how they were chosen.

**In-run calls follow the run, not the environment.** The session pointer
records the provider and its parameters (`provider`, `root`, and for the vault
provider `vault_dir` and `project`); the hook injects them for every
`mcp__experiment__*` call while a run is open, so `advance`, `run_eval`,
`record_observation` and the readers rebuild the same provider.
`experiment_resume(run_id, experiment_dir=None)` finds a run through the
environment default provider, else any session's pointer, else the `goal.yaml`
of the given `experiment_dir`. This closes the experiment-to-project mapping the
2026-08-13 decision deferred.

**How anything reaches the server.** The plugin declares no manifest `env`: a
missing variable would fail the server start, and configuration must degrade,
not fail. Instead the `phase_gate` hook rewrites **every** `mcp__experiment__*`
call, in every branch that is not a deny, to carry:

- `provider`, `store_root`, `project`, `vault_dir`: from the session's pointer
  while a run is open; otherwise the environment defaults above (provider
  `file` unless `HARNESS_EXPERIMENT_PROVIDER=vault`; root `HARNESS_EXPERIMENT_DIR`
  or the XDG default; `project` from `HARNESS_EXPERIMENT_PROJECT`; `vault_dir`
  from `MEMORY_VAULT_DIR`). The hook never blocks on configuration; a vault
  provider that cannot be built (no vault directory, missing entity, no project)
  is a teaching error raised by the server.
- `session_id`: the owning session, captured by the SESSION_START lifecycle
  hook. Child sessions also fire SESSION_START; the first id is the owner.
  The owner is process-global (one per `load_plugins` result) and is replaced
  by a later SESSION_START only when no pointer exists for the current owner.
  If no owner was captured, experiment tools are Blocked with a teaching
  reason.
- for `experiment_start` and `experiment_resume` only: `experiment_dir`
  canonicalized as `Path(cwd, experiment_dir).expanduser().resolve()`.

The rewrite **overwrites** these keys unconditionally; the server refuses a
call that arrives without `store_root` ("experiment tools require the harness
experiment plugin hook"). Rewrites are recorded with full args in `HookDecided`
and `DispatchResolved`, so the provider and root every run used are auditable
from the log. A permission prompt for a rewritten call shows the injected
arguments; that is by design and the README says so.

## 5. Tool surface (`mcp__experiment__*`)

Refusals are raised inside the server so FastMCP returns `isError=True`; the
dispatcher records `is_error=True` and the text stays inline. (The memory
plugin returns `"error: ..."` strings; the difference is deliberate so a
refused transition is distinguishable in the log without parsing text.)
Every refusal and every phase-gate reason names the active run id.

Schemas are typed and re-checked in the server (law 10): `observation` is a
model with six string fields (`title` non-empty, the rest default `""`,
unknown keys rejected); `phase` is an enum of the seven phase names;
`outcome` for `experiment_end` is `user_stopped | escalation`;
`max_iterations` is an integer `>= 1`, default 10; `run_id` matches
`<experiment>/run-NNN`.

The frozen contract, unchanged names and signatures:

| tool | policy while a loop run is open |
|---|---|
| `experiment_start_run(experiment, goal)` | denied (the loop owns lifecycle) |
| `experiment_record_observation(run_id, observation)` | journal phase only; on a loop run the engine attaches `iteration`, `eval_index`, `metrics`, `passed`, `criteria` and the recorded hypothesis (section 3), and refuses when no eval ran in this visit |
| `experiment_end_run(run_id, outcome, metrics)` | denied |
| `experiment_list_runs(experiment)` / `experiment_get_run(run_id)` / `experiment_observations(run_id)` | always; `experiment_get_run` also returns `methods` (the run's methods page) |
| `experiment_compare_runs(experiment)` | always; read-only: every run with outcome, iterations, each criterion's final and best value, and the best run per metric by the criterion's direction, as rows plus a rendered table; also the experiment's `description` and `methodology` |

The loop tools:

| tool | does |
|---|---|
| `experiment_start(experiment_dir, max_iterations=10)` | requires `goal.yaml`; validates criteria and environment **before** creating anything; refuses with a teaching error if any run of this experiment is open ("resume or end run-NNN first") or another live pointer names this directory; `start_run`; freezes eval spec + protected digest into `state.json`; writes the pointer; a failure after `start_run` marks the run `aborted`. Returns run_id, resolved `store_root` and `experiment_dir`, objective, criteria, constraints, and a bounded prior-runs summary ("showing N of M observations; use experiment_observations for the rest") |
| `experiment_resume(run_id, experiment_dir=None)` | re-attaches this session to an open run (new pointer); locates the run in the fallback root, any pointer, or the given experiment directory's `goal.yaml`; returns status |
| `experiment_status(run_id=None)` | phase, iteration/max, best and last metrics, constraints, allowed transitions; with no `run_id` resolves this session's pointer, so a model that lost the id can recover it |
| `experiment_advance(run_id, phase, hypothesis=None)` | transition table; `hypothesis` recorded on entry to `work`; decide->plan increments the iteration and at the cap ends the run with `max_iterations` (pointer cleared); eval->journal refused unless an eval was recorded during this visit; decide->done recomputes criteria (section 6) from `last_eval_metrics` and refuses on `null` ("no eval result recorded") or unmet; `done` ends with `success` (pointer cleared); refuses on an inactive run |
| `experiment_run_eval(run_id)` | eval phase only; runs the frozen eval spec (section 6); no path or timeout arguments |
| `experiment_end(run_id, outcome, note="")` | ends any open run by id, clears every pointer naming it; `note` -> `state.json.exit_note` |

Mutating loop tools verify `run_id` matches the pointer of the injected
session. `run_id` is explicit everywhere except the `experiment_status`
recovery form.

## 6. Eval execution

- **Spec frozen at start.** `goal.eval` is captured into `state.json`; the
  model cannot choose what runs. Forms: a directory -> `python -m pytest <dir>
  -v -s`; a `.py` file -> run it; a string containing whitespace -> a command
  (`shlex.split`, run in `experiment_dir`), e.g. `uv run harness/run.py
  --replay` or `poetry run pytest eval/`.
- **Interpreter** for the first two forms: `goal.eval_python` if set, else
  `<experiment_dir>/.venv/bin/python` if present, else the server's
  `sys.executable`. Integration-mode evals that need the target's environment
  use the command form.
- **Environment:** the server's environment plus `goal.environment`, values
  coerced with `str()` and validated as a flat mapping at start.
- **Isolation:** `stdin=DEVNULL`; run with `asyncio.create_subprocess_exec`,
  `start_new_session=True`, process group killed on timeout or cancellation;
  `eval_in_flight` in `state.json` makes a second call return "eval already
  running".
- **Timeout:** `goal.eval_timeout_s`, default 300, clamped to the manifest's
  `tool_timeout_s` (3600) minus 60 with the clamp stated in the result.
  `tool_timeout_s` is per server, not per tool: a hung server makes every
  experiment tool wait that long, so the eval budget stays well under it.
- **Integrity:** at start the engine digests the protected tree
  (`goal.yaml`, `constraints.yaml`, `eval/**`, `fixtures/**`, and the resolved
  eval target; `__pycache__` and `.pytest_cache` excluded);
  `experiment_run_eval` refuses if the digest changed ("protected files
  changed since start: <paths>"). This closes the paths the hook cannot see
  (bash, python, MCP editors).
- **Result:** `metrics`, `passed`, `criteria_details`, `return_code`,
  `timed_out`, `tests_run`, `tests_passed`, `tests_failed`, and a bounded
  stdout/stderr tail (truncation marked). `passed` = criteria pass over
  recorded metrics using primary criteria if any is marked primary, else all
  criteria (the parked `_criteria_pass` rule), independent of the return
  code; a crashed or timed-out eval with empty criteria is `passed=false`
  (deliberate deviation: empty criteria plus a crash must not read as
  success).
- **Derived metrics** `test_pass_rate`, `tests_run`, `tests_passed`,
  `tests_failed` are added only when a pytest summary parsed with
  `tests_run > 0`, and never overwrite an explicit `[METRIC]` of the same name.
- **Record:** each eval appends `evals/NNN.json` (metrics, passed, timed_out,
  return_code, command, interpreter, duration, digest) and `evals/NNN.out`;
  `state.json` caches the last values and index. The decide->done recompute
  reads `last_eval_metrics`, which is always a copy of `evals/<last>.json`.

**Criteria validation at start** (new): each criterion needs `metric` and
`threshold`; `comparison` accepts `>= <= > < ==` and the aliases
`comparator`, `ge le gt lt eq`; `comparison: report` (or `threshold: null`)
marks a report-only criterion that is recorded but never gates; any other key
or operator is a teaching error before a run is created. `check_criteria` no
longer defaults an unknown operator to `>=`.

## 7. Phase policy and enforcement

The policy is deny-only; the plugin narrows and never grants. The permission
engine (priority 1000) runs after it and its own denies stay absolute, so the
user's `permissions.toml` still decides which shell commands are allowed in the
work phase.

| phase | denied |
|---|---|
| read, plan, decide | `write_file`, `edit_file`, `bash`, `experiment_run_eval`, `experiment_record_observation` |
| work | `experiment_run_eval`, `experiment_record_observation` (bash falls through to the engine) |
| eval | `write_file`, `edit_file`, `bash`, `experiment_record_observation` |
| journal | `write_file`, `edit_file`, `bash`, `experiment_run_eval` |
| any open run | `experiment_start_run`, `experiment_end_run`, `write_file`/`edit_file` under the protected tree |

Two kinds of deny, deliberately different:

- **Phase discipline** (the per-phase rows) -> `Ask`. In the TUI the human
  can approve a one-off out-of-phase action, such as `git status` during
  plan, without ending the run; headless, `HeadlessResolver` denies it. The
  model never gets to decide, which is the property that matters.
- **Integrity and lifecycle** (the protected tree, the raw writer tools while
  a run is open) -> `Block`. Nobody overrides these mid-run; end the run
  instead.

Versus the parked allow-lists this is looser in one way and stricter in two,
all deliberate: (looser) tools the plugin does not know, such as `web_fetch`,
`dispatch_agent`, or third-party MCP editors, are not gated by phase, because
harness's permission engine, not a plugin, owns that decision, and the hook
cannot enumerate other servers' tools; (stricter) the journal phase no longer
allows `write_file` because the journal is written through the store, and the
protected tree is immutable in every phase and additionally digest-checked at
eval time. The parked `bash(pytest*)`-style prefix allow-lists are gone: they
were unenforceable (`python -c` writes anything) and blocked the setup
commands real experiments need.

The `phase_gate` dispatch hook at priority 990 (after `WorkspaceGuard` at 900,
before the engine at 1000):

1. Not a tool call -> `Allow()`.
2. Locate this session's pointer. None, or its `state.json` says `active:
   false` -> stale pointer removed; the plugin is dormant: experiment tools
   get their `Rewrite` (section 4), everything else `Allow()`.
3. Pointer present but `state.json` unreadable -> experiment tools still get
   their `Rewrite` (so `experiment_end`, `experiment_start` and
   `experiment_status` can report and repair), every other tool is Blocked
   with a reason naming those tools and the run id. Fail closed for the work,
   recoverable from inside harness.
4. Protected tree: for `write_file`/`edit_file`, resolve `file_path` ourselves
   (the guard's canonical rewrite is treated as an optimization, not a
   dependency) and `Block` if it is under a protected path of the resolved
   `experiment_dir`.
5. Phase table -> `Ask` (discipline) or `Block` (lifecycle) with a reason
   naming the phase, the tool, the run id, and the transitions available;
   otherwise experiment tools -> `Rewrite`, others -> `Allow()`.

A phase-gate `Block` never reaches the human (it is a tool error the model
sees); an `Ask` does, and the prompt shows the effective call including the
injected arguments.

**Permission rules the plugin needs.** With any `permissions.toml` present,
unmatched tools fall to the layer default (usually `ask`), so the README ships
the rule set and the equivalent `--allow` list for headless runs:
`mcp__experiment__*`, `invoke_skill`, and the native tools the work and eval
phases use (`write_file`, `edit_file`, `bash(...)` as the user chooses). A
test proves `experiment_advance` under a `default = "ask"` layer plus the
shipped rules produces no `PermissionRequested`.

Known limits, documented in the README: the hook gates by tool name and file
path only; `bash` and other servers' tools are not path-confined (harness's
own rule); the digest check is the backstop. Tool calls batched in one
assistant message dispatch concurrently, so a phase transition must be the
sole tool call in its message; the server's own phase checks on `run_eval`
and `record_observation` are the floor.

**Workspace root.** File tools are confined to the harness workspace root
(`--workspace`, default cwd). `experiment_start` returns a teaching error if
`experiment_dir` or a resolved `goal.target` lies outside cwd, and the README
says to launch harness from a directory containing both the experiment and any
integration target. (`--workspace` different from cwd is unsupported in v1: a
plugin hook has no access to the configured root; harness-level question.)

**Subagents.** Children share the HookBus and the owner's injected
`session_id`, so the same gate applies to them. The plugin ships
`agents/experiment-worker.md` whose `tools` allow-list excludes
`experiment_start*`, `experiment_advance`, `experiment_end*` and
`experiment_resume`, and the skill requires
`dispatch_agent(agent="experiment-worker")` during a run. This uses the
existing registry-time primitive rather than the missing per-session
principal.

## 8. Command and skill

`/experiment <experiment_dir> [--max-iterations N]`: the body says invoke the
`experiment` skill, call `experiment_start` (the model parses
`--max-iterations`; `$ARGUMENTS` is substituted textually), follow the
protocol. Headless users prompt the same text (commands are TUI-only today).

The skill, one section per phase, each ending with the tool that ends it:

- **read**: study the returned objective, criteria, constraints
  (`do_not_do`, `known_findings`, `escalate_if`), prior runs; read legacy
  `<experiment_dir>/journal/*.md` if present.
- **plan**: review constraints and prior failures; never repeat a failed
  approach without a new hypothesis; `experiment_advance(work,
  hypothesis=...)`.
- **work**: standalone in `<experiment_dir>/workspace/`; integration in the
  target checkout (branch first; the skill records `git rev-parse HEAD` in the
  observation). Protected files are immutable. Dispatch only
  `experiment-worker`.
- **eval**: `experiment_run_eval`. Crash (`return_code != 0` with no metrics,
  import error, timeout) -> `experiment_advance(work)` to fix; a metric miss
  -> `experiment_advance(journal)`.
- **journal**: `experiment_record_observation` with title, hypothesis,
  changes, result, diagnosis, next_direction.
- **decide**: `experiment_advance(done)` or `experiment_advance(plan)`;
  escalation per constraints -> `experiment_end(escalation, note)`.
- **Turn boundaries:** a phase transition is the sole tool call in its
  message, and each transition ends the reply: call `experiment_advance`,
  report `experiment_status`, stop. A harness turn is bounded by the loop's
  per-turn model-call budget; the human (or a future driver) re-prompts, and
  `experiment_status()` with no arguments recovers the run id.

## 9. Events and telemetry

Every experiment tool call is in the session log (`ToolCallProposed`,
`HookDecided` with the rewrite, `DispatchResolved`, `ToolCallCompleted` with
`is_error` for refusals), so phase history is a fold over the log.
`state.json.session_id` joins a run to `harness stats` / `harness compare`
for cost and per-agent attribution. `harness outcome <session_id>` stays the
human verdict channel. No `CustomEvent` emitter in v1: the tool results
already carry every fact; a run-summary event is a follow-on once something
consumes it.

## 10. Error handling

Server refusals are exceptions rendered as `isError` tool results with
teaching text. Hook Blocks and Asks name the phase, the tool, the run id, and
the way out. A missing owner session id Blocks experiment tools only.
Lifecycle hooks catch everything and return `[]`. Server start failure
(missing `mcp` or `pyyaml` under `python3`) is recorded by `McpHost` as
`server_failed` and the session continues without experiment tools; the
skill's first line names the tool that must exist. A server crash mid-run is
survivable: the server is stateless, `McpHost` restarts it (`on_failure`),
the in-flight call returns a tool error, and `experiment_status` recovers. A
crashed or capped session leaves an open run whose pointer is keyed by the
dead session: the next `experiment_start` for that experiment refuses and
names it; `experiment_resume(run_id)` re-attaches, `experiment_end(run_id,
user_stopped)` closes it. Two sessions cannot open the same experiment
directory at once (pointer scan), and `start_run` creates the run directory
with `exist_ok=False` and retries on collision.

## 11. Testing (harness suite; full suite green and ruff clean before the PR)

- `tests/test_experiment_engine.py`: the parked `test_experiment_workflow.py`
  matrix on a tmp store (no daemon mock): valid/invalid transitions, kickbacks,
  cap ends the run and clears the pointer, inactive-run refusal, decide->done
  gate (metrics fail, `null` recorded, claimed-pass-but-fail, pass, no
  criteria), no-primary fallback, `null` vs `{}`, `best_metrics` direction,
  hypothesis recorded, eval-required-this-visit, second-start refusal,
  aborted-on-late-failure, resume, end-by-id clears pointers.
- `tests/test_experiment_evals.py`: the parked harness tests plus criteria
  validation and aliases, report-only criteria, command-form eval,
  interpreter selection, env coercion, `stdin` isolation, timeout kill,
  digest refusal, derived-metric precedence, bounded output tail.
- `tests/test_experiment_store.py`: the parked store tests minus the memory
  sink, plus a byte-compat fixture: a run directory written by the parked
  code round-trips, and `state.json`/`evals/` are invisible to the parked
  reader.
- `tests/test_experiment_policy_hooks.py`: every phase row and its
  Ask-or-Block kind; protected tree with resolved paths; dormant, stale and
  unreadable branches; `Rewrite` always present for experiment tools including
  when dormant (asserted on `HookDecided.kind == "rewrite"`); overwrite of
  model-supplied keys; owner capture with a child SESSION_START and the reset
  rule; missing owner Blocks experiment tools only; a user-layer `deny bash`
  still wins in the work phase (deny-absolute after a plugin `Allow`);
  concurrent `experiment_advance` + `write_file` in one message yields the
  documented outcome; fail-open lifecycle.
- `tests/test_experiment_server.py`: in-memory MCP transport (memory plugin
  idiom): start -> advance -> run_eval on a tmp eval -> record_observation ->
  advance(done); every refusal is `isError`; every rejected schema shape; a
  call without `store_root` refuses.
- `tests/test_experiment_plugin.py`: `load_plugins([plugins_dir])` loads
  memory and experiment together; `build_kernel` registers the hook at 990;
  a `FakeProvider` script proves a `write_file` under `eval/` is blocked
  during a run and allowed with no run; a subagent dispatched during `read`
  has `write_file` gated; a second `AgentLoop` built with the same session id
  sees the gate active in the recorded phase; `experiment_advance` under a
  `default = "ask"` layer plus the shipped rules produces no
  `PermissionRequested`.
- `tests/test_experiment_subprocess.py`: the real path. `McpHost` launches
  `python3 server.py` with the environment scrubbed to the SDK default set,
  the real dispatcher runs with `phase_gate` registered, and start ->
  advance -> run_eval -> done lands under the injected `store_root`.
- journal coherence: an observation on a loop run carries the engine facts of
  the eval recorded in that visit (frontmatter and body), byte-identical
  rendering when there are none, refusal without an eval this visit, and
  `experiment_compare_runs` tabulates two runs with the best run per metric
  chosen by direction (`<=` picks the lower value).

## 11b. Documentation and vault (lands with the code)

- `plugins/experiment/README.md`: layout, record layout, the three
  environment variables, the permission rules and the `--allow` list, the
  phase table with its Ask/Block kinds and its differences from the parked
  policy, eval forms, headless recipe (the loop's per-turn budget; repeated
  `-p` calls with `experiment_resume`), known limits.
- `docs/user-guide.md` plugins section: the second shipped plugin.
- `docs/plugin-authoring.md`: record the fact only: plugin MCP servers
  receive the SDK default environment, and a manifest `env` naming a missing
  variable fails the server start; point at the open question. The plugin
  README owns "this plugin injects `store_root`/`session_id` by `Rewrite` as
  a provisional answer".
- Vault entity `10-projects/experiment/` (Chris's decision, 2026-09-01):
  `Experiment.md`, `narrative.md`, `decisions/` (scope, driving, home,
  store-root default), `specs/` (this document). Memory entries are written
  through the memory plugin with `subject="experiment"`, never by hand.
- The harness-level open questions in section 12 are recorded under
  `10-projects/harness/` as an open-questions note with back-links to this
  spec, so the next harness design pass finds them.
- Pointers from `10-projects/harness/narrative.md` and the agent-swarm
  narrative ("experiment now lives in harness; the parked copy is unchanged").

## 12. Deferred, and harness-level questions this plugin surfaces

Plugin follow-ons: arms x trials x compare coordinator (cross-arm comparison;
the single-experiment `experiment_compare_runs` reader is in v1; the
experiment-to-project mapping is settled by the `journal` block's provider in `goal.yaml`); presence-gated memory mirroring of observations (a
2026-08-13 contract element, deferred until harness has a way for one plugin
to detect another's store); constraints time limits enforced in code;
importing legacy `journal/` entries; a run-summary event when a consumer
exists; eval integration with the target's environment beyond the command
form.

Harness-level questions, named and left open (recorded in the harness vault
entity per section 11b):

1. Plugin-contributed in-process tools (would remove the two-process split,
   the pointer, and the argument injection).
2. Optional or defaulted `env` references in plugin manifests; today plugin
   servers receive only the SDK default environment, and the golden memory
   plugin's server therefore never sees `HARNESS_MEMORY_DIR` while its hook
   does.
3. A lifecycle context that tells a hook the workspace root and whether a
   session is a root or a child; and a per-session principal on
   `ProposedToolCall`.
4. Dynamic phase layers or arg-scoped rules in the permission engine
   (2026-08-31 deferred them; this plugin is evidence for the need).
5. Headless multi-turn driving: `-p` expansion of plugin commands, and a CLI
   flag for the loop's per-turn model-call budget, which v1 usability leans
   on most.
6. Worktree isolation for integration-mode runs.
7. A plugin-writable session outcome (`SessionOutcome` is CLI-only).
8. Cross-plugin presence detection (needed by the memory mirror follow-on).

## 13. Alternatives considered

- **Skill prose plus the engine's gates, no dispatch hook.** The engine-run
  eval and the decide->done recompute already make the outcome
  non-voluntary; what the hook uniquely adds is protected-tree immutability
  and per-phase tool discipline. Chris chose structural enforcement.
- **State folded from the event log by a subscriber, server as pure store.**
  Enforcement cannot depend on an asynchronous subscriber; transitions need a
  tool, hence the server anyway.
- **One subagent per phase with `FilteredRegistry`.** Cannot express
  path-scoped rules; Chris chose model-driven-in-session. The narrowed
  `experiment-worker` agent keeps the useful half.
- **Store root via manifest `env`.** A missing variable fails the server
  start; configuration must degrade.
- **Store root via a plugin config file both processes read.** Workable for
  the root, but harness has no plugin-config primitive to lean on and the
  `session_id` injection is needed regardless; one more key in the same
  `Rewrite` is the smaller change. Revisit under harness question 2.
- **Block for every phase rule.** Rejected in favour of `Ask` for discipline
  rules: harness already has a human channel, and a run should not have to
  end so a human can approve one command.

## 14. Acceptance: the self-test experiment

Experiments normally live in their own directories outside harness (the
logos-experiments convention). One experiment ships with the plugin because it
tests the plugin: `plugins/experiment/examples/selftest/`, driven two ways.

- **Scripted driver** (`tests/test_experiment_selftest.py`): a fixed tool
  sequence through the real subprocess server, one scenario per feature group,
  asserting on the run directory and the event log. This proves the machinery
  and runs in the normal suite.
- **Dogfood run** (README): a real model follows the skill through the same
  directory, once on `local36` and once on `claude`, then `harness compare`
  joins the two sessions. This proves the skill prose. A checklist of expected
  artifacts makes it pass or fail.

**The experiment.** A synthetic, self-grading task: write
`workspace/solution.py` exposing `solve(fixture) -> dict` that reproduces
`fixtures/expected.json` from `fixtures/input.json`. The eval is a pytest
directory that imports the solution, scores it, and prints `[METRIC]` lines.
The workspace state selects the outcome, so one experiment reaches every gate:

1. **Attempt 1 crashes**: no solution file, `ImportError`, no metrics,
   non-zero return code, `evals/001.out` captured. Exercises the eval-to-work
   kickback and "crash with criteria is not a pass".
2. **Attempt 2 fails the gate**: a stub scores one key of three, the pytest
   test still passes (so `test_pass_rate` is derived as 1.0 while the primary
   criterion fails). Exercises iteration counting, best-metrics, a journal
   observation, and the done gate refusing with recomputed details.
3. **Attempt 3 passes**: the real solution. Exercises done, `success`, pointer
   removal, `run.json` metrics.

`goal.yaml` uses every accepted criteria form (`comparator: ge` primary
`score`, `gt` on `token_seen`, `comparison: "<="` on `mismatches` so
lower-is-better best-metrics is exercised, a report-only `elapsed_ms` with a
null threshold, a `description` on each), sets `environment:
SELFTEST_TOKEN`, which the eval echoes as the `token_seen` metric to prove
environment injection, `eval_timeout_s: 20`, and `description` and
`methodology` prose that the engine freezes into every run's `methods.md`. `constraints.yaml` carries
`do_not_do`, `known_findings` and an `escalate_if` that a `workspace/ESCALATE`
marker triggers through the `escalate` metric. `fixtures/input.json` is read by
the eval, so the protected digest covers a file the task depends on. A sibling
`examples/selftest-cmd/` uses the command eval form (`python3 eval/run.py
--replay`).

**Driver scenarios**, each with a fresh store and state dir:

1. The three-attempt loop; `evals/001..003`, two observations each carrying
   the engine-recorded facts of its eval, `methods.md` naming every criterion
   with its direction and the copied `goal.yaml`, `experiment.md` listing the
   run after `done`, `outcome: success`, no pointer, the refused `done`
   recorded as an error.
2. Protected tree: `bash` appends to `eval/scoring.py` during work, then
   `experiment_run_eval` refuses naming the path; `edit_file` on
   `fixtures/input.json` is blocked by the hook.
3. Phase discipline headless: `write_file` during read is asked and denied;
   `experiment_start_run` while open is blocked; reasons name the run id.
4. Iteration cap: `max_iterations: 2` with the stub left in place ends with
   `max_iterations` and no pointer.
5. Resume, end, prior runs, stale pointer: a second session resumes by id and
   advances; a third ends it with a note, then starts a new run whose result
   carries the bounded prior-runs summary; a stale pointer is cleaned on the
   next call.
6. Subagent: `experiment-worker` dispatched during read cannot write (asked,
   denied) and has no `experiment_advance` tool.
7. Timeout: a `workspace/SLOW` marker makes the eval sleep past
   `eval_timeout_s`; the result reports `timed_out`, the process group is gone.
8. Command-form eval on `selftest-cmd`; the recorded command is the argv.
9. Escalation: `workspace/ESCALATE`, the eval reports `escalate=1`,
   `experiment_end(escalation, note)` records the outcome and note.
10. Compare: after a stub-only run ended by hand and a passing run,
    `experiment_compare_runs` lists both, names the passing run best for
    `score` and `mismatches`, carries the experiment's description and
    methodology, and `experiment.md` lists both runs with the passing one best.

Budget: the scenarios share the plugin server start pattern of the existing
subprocess test and stay under about forty seconds together.
