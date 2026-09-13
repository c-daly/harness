# Experimental native manifest-runner binding

For independent checks and automatic continuation of partial attempts, use
[`run_verified.py`](run_verified.py) and the
[verified-completion guide](../../docs/verified-completion.md). The original
`run_one.py` remains available for one supervised attempt and comparison.

This driver hands the installed agent-swarm runner's first eligible request to
a real Harness coordinator and `dispatch_agent` child. The installed plugin
parses the existing manifest, computes dependency eligibility, builds the worker
prompt and records the dispatch. It is not replaced with a second scheduler.
Harness owns inference, tools, workspace checks, permissions, execution counts,
usage observations, cancellation and parent/child session journals.

This is a **partial native binding**, not a compatible import of agent-swarm's
Claude Code plugin. It explicitly supplies a native implementer role. It does
not load the Claude router's identities, policy hooks, Serena editing protocol,
compaction hooks, or memory. Those integrations remain unqualified. The native
role must not be represented as a registered Claude router implementer.

## One supervised attempt

Install Harness's dependencies, and supply the existing agent-swarm installation,
manifest, state directory, model catalog and a new evidence directory:

```sh
PYTHONPATH=src:. python plugins/agent-swarm-runner/run_one.py \
  --plugin-root /path/to/agent-swarm \
  --manifest /path/to/manifest.yaml \
  --state-dir /path/to/orchestration-state \
  --output /path/to/new-evidence-directory \
  --catalog /path/to/models.toml \
  --model gpt --max-model-calls 48 --timeout 1800
```

Use `--inspect` first to save the plugin's eligible requests without inference
or queue mutation. Stop the prior orchestration host before transferring queue
ownership. The advisory lock coordinates copies of this driver only; the
installed plugin's other frontends do not honor it. This is deliberately not
an unattended multi-host scheduler.

The first eligible request must name a clean worktree on its declared branch.
Dirty work is refused for explicit reconciliation, never reset or overwritten.
The coordinator can dispatch only the pinned request and model; the child has
native read/write/edit/search/bash tools and cannot delegate. One child and one
worktree per invocation avoid relying on child-specific workspace support that
Harness does not yet provide. Native workspace checks are not an OS sandbox;
the shell retains the operator's authority. The role instructs the worker to
avoid network operations, commits, publication and destructive lifecycle actions;
these instructions are not shell containment.

The native agent explicitly configures 48 iterations and 16,384 output tokens
per response. Otherwise the core defaults remain 20 iterations and 4,096 tokens;
a larger context-profile cap alone cannot raise a smaller agent-task cap.
The attempt is bounded by model-call count, tool-call count, per-response output
tokens, inference time and task time. Token stop limits additionally apply when
the selected catalog entry declares reported accounting. An entry declaring
unknown accounting stays unknown; the driver does not alter the user's catalog
to invent a billing guarantee. Provider usage and unknown counts are recorded.

The plugin stores the owning Harness root session as `worker_id`. Evidence maps
that root to the actual child session. `SIGINT` or `SIGTERM` requests cancellation
through Harness and retains partial effects and terminal records. No elapsed
time or stored `active` flag is treated as a hang or proof of a live owner.

**The driver never marks the queue item complete**, even when the model says it
finished. It leaves dispatch recorded and writes `result.json` for independent
inspection of the child terminal, actual diff and check results. A supervisor
must resolve that state before retrying or releasing dependent work. The driver
does not automatically retry, merge, clean worktrees, publish or accept tasks.

## Evidence and limits

Each invocation saves the plugin's eligible requests and prior queue state,
the exact worker prompt, manifest/prompt hashes, model route, initial branch and
commit, owning PID, root and child session identities, native journals, root
outcome and usage, and remaining Git changes. Keep the evidence directory:
`result.json` alone is not a replacement for the journal or independent checks.

`tests/test_agent_swarm_runner.py` uses scripted inference with real Harness
dispatch and native file effects. It checks prompt pinning, child lineage,
refusal of dirty work and withholding queue completion. It does not establish
model ability, real installed-plugin compatibility, memory continuity, sustained
workflow recovery, or TUI usability; those require separate live evidence.
