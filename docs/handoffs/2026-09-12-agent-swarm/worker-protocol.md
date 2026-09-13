# Worker protocol for the Harness remaining-roadmap queue

Read this completely before touching any file. It applies to every task in
`remaining-work-manifest.yaml`. The orchestrator (agent-swarm) dispatched you;
the user owns merges and releases.

## Identity and tools

- Every action goes through `mcp-call --caller-id=<your agent id> <tool> '<json>'`.
  Your id is in your briefing. Read files with `native__read_file`, search with
  `native__grep`, run shell with `native__bash`, edit with
  `serena__replace_content` or `native__edit_file`, create files with
  `native__write_file`.
- Pass `"cwd"` to `native__bash` as your worktree path on every call.

## Where you work

- Work only inside the worktree directory named in your task prompt. Its
  branch is already checked out. Never create, switch, rebase or force-push
  branches. Never `cd` into `/home/fearsidhe/projects/harness` or any other
  worktree, and never edit files there.
- Preserve everything you did not create: untracked files, `.claude/`,
  `.context/`, `.local-runtime/`, `.worktrees/tmp/` evidence, user config under
  `~/.config/harness/`, running services and model weights. Do not stop
  processes you did not start. Do not download weights or start local models
  unless your task explicitly says so.
- Scratch goes under `/home/fearsidhe/projects/harness/.worktrees/tmp/<task-name>/`
  (create it). Export `TMPDIR` to that directory and
  `GIT_CEILING_DIRECTORIES=/home/fearsidhe/projects/harness/.worktrees/tmp`
  before running tests that create nested Git fixtures.

## Environment

```sh
cd <worktree>
uv sync --locked --extra dev --python 3.13      # creates <worktree>/.venv
uv run ruff check .
uv run pytest -q tests/test_<focused>.py
```

Use `uv run …` for every Python command. The router shell exports a global
`PYTHONPATH` pointing at the agent-swarm plugin, whose `scripts` package
shadows this repository's `scripts/` namespace and breaks collection of the
`scripts.*`-importing tests; clear it for every test, build and smoke command:
`env -u PYTHONPATH uv run pytest -q` (or `PYTHONPATH= uv run …`). `uv sync --locked` must succeed
without editing `pyproject.toml` or `uv.lock`; a dependency change is a
reportable deviation, not something to do quietly. If the default uv cache is
not writable, prefix commands with `UV_CACHE_DIR=/home/fearsidhe/projects/harness/.worktrees/tmp/uv-cache`.

## How to build

- Test first. Write the failing test, run it and confirm it fails for the
  intended reason, implement the minimum, run it green. Tests must be
  meaningful behavioral checks of the change; do not pad the count.
- Follow `docs/contributing.md` and `docs/architecture.md`: the event log is
  the unit of truth; the event union is closed and additive (new fields have
  defaults; never remove or retype); tool errors are values with teaching
  text; dispatch hooks fail closed; no silent caps; deterministic artifacts;
  Ruff line length 100, no `noqa`, no unused imports.
- Keep memory and agent-swarm as plugins: core must not import their internals
  or depend on their private state formats.
- No self-approved activation, no weight training, no changes to evaluation
  gates or fixtures merely to obtain a pass. A failed experiment is a finding.
- Stay in scope. No opportunistic refactors, no edits to unrelated modules.
- Update user-facing docs in the same change when behavior changes. Do NOT
  edit `docs/superpowers/plans/2026-09-06-core-agency-progress.md`; the
  orchestrator consolidates records. Put your validation record and any
  retained evidence in `docs/handoffs/2026-09-12-<task-slug>/README.md`.

## Before you push

Run, in this order, with sources frozen (any later edit restarts the sequence):

```sh
uv run ruff check .
uv run pytest -q                                           # complete 3.13 run
uv sync --locked --extra dev --python 3.12 && uv run --python 3.12 pytest -q   # complete 3.12 run
uv build --out-dir dist
scripts/smoke_wheel.sh dist/*.whl
git diff --check
```

If the two full runs cannot both complete (for example a genuine environment
failure), say exactly which one ran, what it reported, and why the other did
not; never report a partial or interrupted run as green. Copy the exact
`N passed, M skipped …` summary lines into your handoff README and PR body.
Seven skips (Anthropic/Ollama fixtures and the opt-in Antigravity probe) and
six MCP deprecation warnings are the known baseline.

## Long-running commands

Router calls time out after about 30 seconds. Run anything longer (uv sync,
full pytest runs, builds) detached and poll the log:

```sh
nohup uv run pytest -q > /home/fearsidhe/projects/harness/.worktrees/tmp/<task-name>/full-py313.log 2>&1 &
# later
tail -5 /home/fearsidhe/projects/harness/.worktrees/tmp/<task-name>/full-py313.log
```

Wait for the process to exit before reading a summary; a log without the final
`N passed` line is an incomplete run.

## Commit and PR body (the orchestrator pushes)

Your role cannot push. Where your task text says "push" or "open a PR", do
this instead:

```sh
git add <exact files>
git commit -m "<type>(<area>): <what changed>"
```

Then write `docs/handoffs/2026-09-12-<task-slug>/pr-body.md` (commit it too)
stating: what changed and why, the roadmap items it advances, the exact
validation summary lines for both Python versions, packaging/smoke results,
known limits and anything deliberately left out. The orchestrator pushes your
branch, opens the PR with that body against the base named in your prompt,
and inspects CI. Do not merge, tag or release anything.

## Report back

Your final message lists: files changed (path and what changed), tests added
(names), the exact pytest summary lines for both versions, Ruff/build/smoke
results, the commit hashes on your branch, and anything left undone or
deviated from the task with the reason. Report blockers instead of working
around them.
