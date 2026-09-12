# Orchestrator preface prepended to every worker prompt

The text below is inserted between the router briefing and the runner-built
task prompt. It does not modify the manifest or plan.

---

## Orchestrator instructions (read before the task prompt)

- First read `/home/fearsidhe/projects/harness/.worktrees/remaining-roadmap-workflow/docs/handoffs/2026-09-12-agent-swarm/worker-protocol.md` completely; it governs environment setup, validation, commits and reporting and overrides the generic TDD template below where they differ.
- The "Do NOT modify files outside `.` and `tests`" rule below means: stay inside your worktree. Edit only the files your task names.
- "At least 5 test functions" is a floor from a generic template; write the meaningful behavioral tests your task describes and do not pad.
- Where your task text says push or open a PR: commit, write `docs/handoffs/2026-09-12-<task-slug>/pr-body.md`, commit that too, and stop. The orchestrator pushes and opens the PR.
- Run `uv sync`, full test suites and builds detached with `nohup … &` and poll the log; router calls time out at about 30 seconds.
- Your final message must contain the exact complete-run pytest summary lines for both Python versions, or say precisely which run did not complete and why.
