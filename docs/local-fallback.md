# Task-preserving local fallback

Core can switch a failed inference request to an explicitly configured local
model before the current task has accepted a conversation response or begun
work. The switch retains the prepared context, task/run identity, acceptance
criteria, permissions, task deadline and cumulative model/tool call budget.
Memory and agent-swarm remain plugins. No semantic classification is required
to make this decision.

## Configure

Provision a local runtime and weights separately using the
[M3 example](local-assistant.md). Declare `tags = ["tools"]` on a candidate
that supports the tools your workflow needs. The supplied 8B catalog declares
this for the bounded M3 project workflow; `verified = false` still applies to
universal adapter conformance. Other required capability tags are explicit
operator declarations, not capabilities inferred from a readiness probe.

Merge this into the project's `.harness/routing.toml`, replacing `preferred`
with your configured primary alias:

```toml
default = "preferred"

[fallback]
models = ["local-small"]
required_tags = ["tools"]
```

Start Harness with this routing default. `--model` and `/model` are explicit
pins and block automatic fallback. The candidate list is ordered, unique and
limited to three aliases; no discovery, model download or installation occurs.
An absent project policy inherits the user routing policy. A project
`[fallback]` with `models = []` explicitly disables that inheritance.

Use the normal entry point with a catalog containing both aliases and a context
profile appropriate for the local model:

```sh
harness --catalog /path/to/models.toml --context-profile /path/to/project.toml
```

Configuring fallback allows reuse of the same prepared context at each listed
destination. Each destination still passes through hooks and permissions as
`model:<alias>`; grant only destinations appropriate for that context. A hook
cannot redirect a selected fallback alias to another model. A candidate must
resolve to a declared local inference runtime, satisfy all required tags and
include `tools` when tool schemas are present. Fresh busy observations skip a
candidate; dispatch then checks actual local readiness and bounded startup.

## What happens on failure

After the dispatcher's existing bounded retries, a typed network,
authentication, rate-limit, overload or local-readiness failure can trigger
fallback. Observed tool or child activity during a failed provider call also
stops the dispatcher's own retries. Failed reservations are retained and unknown
usage remains unknown.
Each alias is attempted at most once in the candidate chain, with the normal
bounded dispatcher retries inside that attempt. All candidates share the
remaining inference deadline; they cannot extend the enclosing task deadline.

The local model receives the existing prepared request. Context sources are
not fetched again. A failed stream's partial text is cleared in the TUI before
replacement output. The active model appears in the status bar, and a durable
`Fallback selected` event explains the change. Selection records the decision
to try that destination; the model and task outcomes establish whether it ran
or completed. `/status` and `harness status SESSION` show saved choices without
calling a model. The user's draft and selected default model remain intact.

The selected fallback remains in use for the rest of that run. Recovery of the
primary does not move an active task back. A subsequent explicitly submitted
task can use the configured primary again. Session replay restores the
decisions, not execution. A restarted process uses its current routing policy;
an old session policy alone does not enable fallback or resume work.

An accepted conversation response, non-context tool proposal or child-agent
execution blocks automatic switching, including activity during a failing
first inference call. Failures inside external agents are held for explicit
reconciliation because their native side effects are outside this boundary.
Policy denial, budget exhaustion, malformed output, context/input/output
limits, cancellation and deadlines do not trigger fallback. If every candidate
is unsuitable or fails, the existing task failure/queue pause behavior applies
and unfinished criteria remain recorded.

## Qualification and remaining M4 work

`tests/test_fallback.py` checks finite selection, actual dispatch/readiness,
authority, pins, shared budgets/deadlines, side effects, external runtimes,
stream replacement, replay and configuration precedence. The opt-in
`scripts/qualify_fallback.py` uses real Qwen3-8B inference in the loopback-only
M3 container, with connection refusal and HTTP 401 fault endpoints, both without
plugins and with normal memory. Its gates require one assignment, one exact
native write, retained criteria, one retrieval per source, visible status,
bounded latency and owned-runtime cleanup. The endpoints simulate provider
failures; this is not a live cloud-provider outage test.

Core now provides [session-tree local scheduling](local-scheduling.md) across
aliases, with visible queues and bounded background work. M4 remains open:
external-agent reconciliation and handoff, held-out semantic promotion, and
the supervised self-improvement activation/rollback cycle remain open. This
fallback gate does not qualify CPU-only execution, every local model, or
arbitrary agent tasks.

The [evidence handoff](handoffs/2026-09-07-local-fallback/README.md) records
the real gate outcomes, retained failures, source hashes and reproduction recipe.
