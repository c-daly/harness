# Core execution controls

An elapsed time budget limits how long work may run. Reaching it does not prove
that an agent hung or stopped making progress. Harness keeps finite defaults,
but the operator can change them for a workload without editing code.

```sh
harness --task-timeout-seconds 1800 --inference-timeout-seconds 300 \
        --coordination-timeout-seconds 1800 --max-active-children 4
```

These flags work in interactive and headless mode, with `--resume` and
`--continue`. In the terminal:

```text
/execution
/execution task-timeout-seconds 1800 inference-timeout-seconds 300
```

Inspection makes no model call and does not change the event log or the draft.
Changes to session defaults require an idle root session with no queued prompts, active turn,
compaction, child, or coordinator. The operator command is core and cannot be
shadowed by a plugin command. Models have no tool for changing these settings.

## Scope and defaults

| Setting / CLI flag | New-session default | Applies to |
| --- | ---: | --- |
| `task-timeout-seconds` | 600 | A native turn or external-agent task, including context/tool/permission waits |
| `inference-timeout-seconds` | 120 | Each native conversational model request, including its retries, after routing |
| `coordination-timeout-seconds` | 600 | An admitted ensemble/panel/other coordinator and its descendants |
| `max-model-calls` | 1024 | Shared model-attempt admission count |
| `max-tool-calls` | 4096 | Shared tool-call admission count |
| `max-children` | 128 | Shared descendant admission count, including pure coordinators |
| `max-depth` | 4 | Descendant nesting depth |
| `max-active-children` | 16 | Simultaneous child-agent admission |
| `max-active-coordinators` | 16 | Simultaneous pure-coordinator admission |

Seconds must be positive and finite. Counts must be nonnegative integers; zero
refuses that category of new work. Infinity and disabling timeouts are not
supported. Eligible live root timers can receive an explicit operator extension
as described below.

Fresh tasks use the current task timeout. An explicit task timeout can make the
task shorter, but cannot exceed the session cap. Serialized tasks record the
effective timeout, so replay and handoff keep it even when the destination
allows longer fresh tasks. Children share the root settings, and an enclosing
task/coordinator deadline can cancel a child before its own budget expires.

Codex, Claude Code and Antigravity process-adapter defaults follow the owned
call's budget using a context-local binding; concurrent calls never mutate a
shared provider's settings. An explicit adapter-constructor timeout remains a
stricter cap. Standalone calls outside core retain their 600-second default.
Antigravity's existing `--print-timeout` keeps its five-second shutdown headroom
(with a one-second minimum) under the selected process backstop. Adapter setup
timeouts and provider/server limits may still stop work sooner.

The native request limit is applied after routing, so a routed external agent
does not accidentally inherit the shorter native inference deadline. Separate
internal requests, such as semantic assessments and compaction, retain their
own declared request budgets. Fallback retains the first native request's
remaining budget across replacement models; the enclosing task also bounds
the whole sequence.

## Extend a live root task

Use `/execution` while work is running to see the live task timers in the current
session. An eligible run includes a command using its run-ID prefix:

```text
/execution extend RUN_ID ADDITIONAL_SECONDS
```

For example, copy the displayed run ID and grant another `300` seconds. The
command adds time to the existing deadline; it does not restart the timer from
the moment you enter it. A prefix must contain at least eight characters and
identify exactly one live run. Repeated grants are cumulative. Using the run ID
prevents a delayed command from accidentally extending the next queued task.

Extensions apply to fresh Harness-owned root tasks that used the session's
default timeout. Explicit or serialized task timeouts, delegated tasks, handoff
continuations, and typed external-runtime timers cannot be extended. Inspection
explains a run's eligibility. An expired, completed, or cancelling run cannot
be revived, including while cancellation cleanup is pending.

**An extension changes only that outer task timer.** Model-request, context,
child, coordinator, and external-process caps stay in force and may end work
sooner. A Harness task wrapping an external agent can have an eligible outer
timer while the external runtime retains a fixed deadline. `/activity` shows
the remaining observed enclosing budgets. This feature does not reschedule
provider subprocess timers or Antigravity's `--print-timeout`.

The command remains usable with an active turn and queued prompts. It does not
invoke a model, change session defaults, refund usage or call counters, or
increase iteration/tool/delegation authority. A short confirmation identifies
the changed task budget. Models have no extension tool, and the operator API
refuses calls from a model/task execution context.

Embedding frontends can use the same operator entry point on the owning event
loop, outside a task's execution context:

```python
from harness.run_budgets import extend_execution
grant = extend_execution(kernel, run_id, 300)
```

The `task_budget_extended` intent records the run, task, previous/new total
timeout, and operator attribution before the live timer is rescheduled. An
append failure leaves the timer unchanged. Admission and rescheduling contain
no asynchronous yield; synchronous persistence may consume some newly granted
time. Changing a deadline does not count as observed task activity.

Grants are historical records after completion or restart. They do not restore
live timers, change the next task's default, or widen a handoff's captured task
limits. Portable task packages include the grants with their source sequence
numbers as evidence; importing a package does not apply them as authority.

## Continuity and stop causes

Settings are written to `execution_configured` events before a live change.
Resume retains stored values unless an operator explicitly supplies an
override; partial overrides keep the other stored fields. Legacy sessions use
the defaults. Invalid stored settings refuse resume instead of silently
resetting limits. `/clear` inherits the current settings, while `/resume`
restores the destination session's settings. TUI resume validates those records
before tearing down the current session.

Execution events allow additive fields from newer binaries: readers preserve
those fields in the event payload and restore the limits they understand.
Unknown limits are not enforced by an older binary. Missing or invalid known
limits still refuse resume; an extra field cannot hide corrupted known values.
Breaking schema changes require a new event type so older readers can preserve
and skip it under the normal unknown-event contract.

Changing settings does not refund already reserved work. Handoff narrows the
session to the source's captured limits and records that narrowing. The source
task's recorded timeout remains binding as well. Handoff's existing source
version and effect-reconciliation requirements still apply.

Model-attempt, tool-call and descendant admission counts are durable across
restart. Every admitted reservation is recorded in the root session before
execution; descendants share that owner and write ownership links in their own
logs. Restoring a tracked root requires no child logs. A retry consumes another
model reservation; a tool denied after admission still consumes its reservation.
A refusal at the count limit does not. Failure, cancellation and a crash before
execution do not refund admitted work. These are admission counts, not a claim
that every reserved operation executed successfully. Counters cover Harness
dispatch and coordination boundaries; unobserved provider-native calls and
tools remain outside this accounting.

Active child/coordinator slots, live activity and timers start empty on restart.
Their historical records do not prove that an external process stopped; normal
effect reconciliation and handoff requirements still apply. Restoring durable
counts does not authorize independently resuming a child as a new budget root.
The TUI checks ownership and stored count integrity before replacing its current
session. Malformed records, regressing totals and duplicate reservations refuse
resume before a new run boundary is appended.

A failed or rewritten accounting append prevents the operation from running and
holds further admission throughout the live tree. Restart reads the durable
prefix: if the reservation reached disk before the failure, it remains consumed.
Core writes use synchronous, fsynced intents on the owning event loop. Embedders
must share the session's execution scope; a second independent ledger cannot
claim the same live session. Pure in-memory budget objects without a session
have no persistence guarantee.

### Upgrading older sessions

Older root-only sessions use a conservative projection of recorded model starts,
retry announcements and tool proposals. This can overcount work that was refused
or never reached execution; `/execution` makes that distinction visible. Existing
coordinator starts count as descendants. A legacy child spawn makes total tree
consumption unknown: admission is held, and the operator can export the task and
start a new session. Increasing limits does not remove that accounting gap.
Legacy child sessions cannot resume independently as fresh roots.

New counts retain their source session, enclosing call ID where available, and
monotone totals in `execution_counts_recorded` intents. An attachment record
establishes the accounting boundary for each process lifetime; child logs carry
`execution_counts_linked`. Older binaries preserve and skip these new event
types, so they cannot enforce this ledger. If they resume and perform work, the
next upgrade projects that interval conservatively and holds untracked child
work instead of trusting the older saved totals. A handoff can retain a higher
captured count through a durable record; it cannot reduce consumed work.

The separate token/cost usage ledger remains durable and can only be tightened
on resume; see [usage stop limits](usage-budgets.md). Admission counts do not
estimate future tokens or reserve a monetary charge. Exact pre-call token/cost
reservations remain separate work.

A task timer expiring records an incomplete run with reason `deadline` and a
visible “task time budget exhausted” error. A nested operation timing out before
that timer expires records reason `timeout`. Neither means a hang was detected.
Cancellation remains cancellation. Cleanup and started file operations must
settle before the terminal record, which can take work past its nominal time
budget. Adapter-specific errors retain their own provider error reporting.

## Qualification boundary

Regression tests cover both CLI entry paths, actual terminal rendering and
rebuilds, persistence, malformed settings, failed writes, task and child caps,
source handoff, timeout/cancellation cleanup, and real subprocess adapters with
scripted CLI responses. Long budget propagation is checked by observing the
actual process timer configuration; it is not a live multi-hour provider run.

Live task, call, descendant and wait observations are now available through
[/activity](activity-supervision.md) and the TUI's persistent activity summary.
Inspection does not change budgets or classify silence as a hang.

`/activity cancel RUN_ID` requests an audited stop of one live agent run and
its descendants while independent siblings can continue. It waits for cleanup,
retains consumed counts and refuses time extensions for stopping runs. See
[run cancellation](activity-supervision.md#cancel-one-live-run) for ownership,
outcome and restart semantics.

Still pending: progress-sensitive supervision, suspected-stall
inspection and recovery, extending independent provider/child/coordinator timers, exact pre-call token/cost reservations,
and live mixed-provider qualification. A heartbeat, emitted token, or silence
alone does not establish useful progress or a hang.
