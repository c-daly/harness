# Local request scheduling

Core serializes local startup and inference within each declared device group.
The default group is `local`, shared by every local alias in the session tree,
including native subagents. Give aliases that use the same GPU the same group:

```toml
[models.local-small.local]
resource_group = "local"
```

Add this field to an existing local profile; its endpoint, command and required
assets remain configured as described in [local readiness](local-runtime-readiness.md).
A different group allows concurrent requests, so use that only for independently
provisioned capacity. Group names contain 1–64 ASCII letters, digits, dots,
underscores or hyphens and start with a letter or digit. The manager rejects
assigning the same normalized endpoint to different groups during its lifetime.

## Admission and controls

One request holds a group from readiness/startup through HTTP stream cleanup.
Root conversation and compaction requests take priority over queued work and
subagent inference; each class is FIFO. Priority does not interrupt a running
generation. The shared queue holds at most 32 waiting requests by default.
Embedding applications can set `LocalResources(max_waiting=...)`; zero permits
immediate admission only. Queue limits cover all groups in that manager.

The existing request deadline includes waiting, startup and generation. Waiting
does not obtain a fresh timeout or refund a model-call reservation. Permissions,
routing and cumulative budget checks happen before admission. A recursive call
waiting for its own ancestor's group fails promptly instead of deadlocking.
Cancellation removes a waiter without starting its model. Process cleanup may
finish after the deadline: Harness gives its owned process group two seconds
to stop, then kills remaining members before admitting a successor.

The TUI shows `waiting for local group ...` and records the queued alias. The
composer remains editable; the normal interrupt control cancels the current
request and preserves its unsent draft. `/resources` reports the active alias
and waiting count for each group. `/status` and headless session inspection
show recent scheduling facts linked to the model call and task run. A saved
queued/acquired fact explicitly says live admission is unknown. Restart does
not recreate or execute an old in-memory queue.

Semantic and evaluation requests are opportunistic background work. They do
not queue behind another request, cold-start a runtime, or replace a different
warm Harness-owned runtime. A semantic service records `busy` and leaves its
deterministic behavior intact. The background busy result also handles races
after the service's initial readiness check. Explicit stop controls never need
a semantic inference call.

## Runtime residency

Before a foreground/work request loads another alias in the same group, Harness
stops idle runtimes that it owns. It waits for active requests to release their
group first. Aliases with the same endpoint and model route can borrow the
existing runtime. A different model on the same endpoint can replace an idle
owned server after an inventory mismatch; externally owned servers are never
stopped or adopted. Stopping an owned server invalidates cached readiness for
aliases that may have borrowed its endpoint.

An explicit `/resources stop` requires the target's device group to be idle,
including any alias borrowing its runtime. Activity in an independent group
does not block the stop.

The existing global limit of one Harness-owned runtime still applies by default,
even across distinct device groups. This is session-tree admission and owned
process management. It does not measure GPU free memory, arbitrate between
separate Harness processes, control other programs, or prevent a misconfigured
group declaration from overcommitting a device. Long active generations can
still delay interactive work, and repeated model changes can incur cold starts.
Local admission refusal does not trigger an automatic fallback loop.

## Evidence and remaining work

`tests/test_local_scheduling.py` covers priority, queue bounds, alias exclusion,
deadlines, cancellation at handoff, failed journals, authority, replay, real
process replacement and final TUI rendering. The opt-in
`scripts/qualify_scheduling.py` uses the pinned M3 8B model on two local ports,
with plugins absent and with normal memory. It checks a real stream, queued
user cancellation, preserved draft, background abstention, interactive priority,
owned replacement, one exact project write, retained criteria and settled logs.
The two aliases load the same weights; this does not compare model quality.

Run the driver in the [M3 offline container](local-assistant.md) with its normal
read-only memory/vault and model mounts:

```sh
python -B -m scripts.qualify_scheduling \
  --model-file /models/8b.gguf \
  --memory-root /home/fearsidhe/.claude/plugins/memory \
  --output /reports/scheduling.json
```

Provision weights and runtime separately. The driver requires loopback-only
networking, finite CPU/RAM limits, zero swap and the pinned weight hash. Its
metadata report contains no private memory or generated prose. See the
[evidence handoff](handoffs/2026-09-07-local-scheduling/README.md) for results.

M4 still needs the supervised improvement candidate/evaluation/activation/
rollback cycle, held-out semantic promotion, and explicit reconciliation for
external-agent handoff. Memory and agent-swarm remain plugins; scheduling is core.
