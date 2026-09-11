# Live activity inspection and run controls

During a running turn, the TUI shows elapsed time, time since the most recent
observed activity, and a phase or explicit wait. It refreshes once a second even
when the provider is silent. Use `/activity` for the full view, including while
the turn is running or follow-ups are queued.

Inspection is a core interface operation. Plain `/activity` does not invoke a
model, write to the session log, change the prompt queue, or require a network connection. A
plugin command named `activity` cannot replace it. The existing F2 panel and
`/task` continue to show recorded execution outcomes and task evidence.

## What is observed

The shared core tracker covers native tasks, typed external agent tasks, model
dispatch, tool dispatch, and coordinators across the live session tree. Each
active operation has an immutable snapshot containing:

- Its kind, label, broad phase, elapsed time, and time in that phase.
- Time since observed activity and the last signal category.
- The number of stream events delivered through the dispatcher; their contents
  are not retained by this tracker.
- Session, task, run, and call identifiers when available, plus its parent
  operation's identifier. These associate observations with durable task and
  dispatch records.
- Remaining time in the earliest observed enclosing task, coordinator, or
  model-call budget. A stricter adapter or context-source timeout may stop work
  sooner. `/execution` shows the configured defaults.

Permission decisions, local capacity admission, local readiness (including its
lock, probe and startup), and retry delays appear as separate wait operations.
Concurrent waits remain visible when a sibling streams or finishes. Root
activity includes descendant activity; inspect each row to see which branch is
quiet. Normal memory acquisition remains an ordinary context tool operation.

Timing uses a monotonic clock. Merely reading a snapshot does not count as
activity. Operation boundaries, phase changes, and delivered stream events do.
The compact summary shows an observed phase or a waiting reason; `/activity`
shows all active operations up to 50 rows and reports any omitted count.

The same data is available to an embedding frontend without the TUI:

```python
tracker = kernel.loop.dispatcher.scope.budget.activity
rows = tracker.snapshot()  # tuple of frozen ActivityStatus records
```

Use this on the kernel's owning event loop. This is process-local observation,
not a service for polling a different process. Only active operations are
retained. Prompt text, reasoning, output chunks, tool arguments, and permission
reasons are not copied into activity records. Rendered identifiers and labels
have terminal escape sequences removed.

## Cancel one live run

`/activity` also lists the live native and typed external agent runs in the
session tree, with full run IDs and copyable cancellation commands:

```text
/activity cancel RUN_ID
```

Use a full run ID or an unambiguous prefix of at least eight characters. This
requests cancellation of that run and its dependent work. Independent siblings
can continue: stopping one ensemble member returns a typed cancelled result to
the coordinator, which can retain the other members' output. Stopping the root
unwinds its descendants and pauses queued follow-ups after cleanup. Draft input,
queued prompts, task obligations and delivered partial reply text are retained.
Partial reply text remains visible in this terminal; cancellation does not
publish it as a completed agent response.

The request is written to the root log as `agent_run_cancel_requested` before
delivery. The target's `agent_run_finished` records its eventual outcome in its
own session; an explicit stop normally has reason `operator_cancelled`. An
overlapping parent interruption or deadline still determines its own outcome.
The request itself is intent, not proof of completed cleanup. Failed or rewritten
request writes do not send cancellation. Completed, ambiguous, foreign and
already stopping runs are refused. Requests do not extend budgets or refund
admission counts, and a stopping run cannot receive a time extension.

Cancellation is cooperative. Provider stream cleanup and started native file
operations settle before a cancelled result is published or child capacity is
released. The TUI continues to show pending cleanup. A provider that suppresses
cancellation cannot admit new model, tool or descendant work through core, or
publish a successful response for the stopped run. This is not a force-kill
facility for an unresponsive runtime; provider-native effects still require
inspection before continuation.

For another frontend on the owning event loop:

```python
from harness.run_controls import cancel_run

rows = kernel.loop.dispatcher.scope.budget.controls.snapshot()
requested = cancel_run(kernel, rows[0].run_id)
```

This is a root operator API, unavailable inside model/tool execution or from a
child kernel. There is no model cancellation tool. Run controls accept agent
run IDs; activity IDs, model call IDs, individual tools and pure coordinators
are not separately cancellable through this command. Esc retains its existing
whole-turn behavior. Restart restores neither live controls nor executable stop
requests; [portable exports](portable-continuation.md) retain request metadata
only as historical evidence.

## Interpretation and limits

**Activity does not verify progress toward the user's goal. Silence does not
establish a hang.** A stream event can be reasoning, output, tool-call data,
usage, or a stop marker. A quiet provider can still be doing useful work beyond
Harness's visibility. A broad phase is the most recent instrumented phase;
provider-internal steps and cleanup are not inferred from silence.

Existing finite execution budgets remain enforced. Activity inspection and run
controls do not cancel work for inactivity, extend deadlines automatically, or
run semantic classification. An operation stays visible until its wrapped execution and
cleanup unwind. An elapsed observed budget is labeled accordingly while work
or cleanup is still active.

Cancellation, failure and successful completion remove live operations. A
late callback cannot restore a finished operation. Resuming a session starts
with an empty live tracker; saved run records alone never imply that a process
is still working. The existing event log remains the durable authority.

Eligible root task timers can now receive an explicit operator extension through
[/execution extend](execution-controls.md#extend-a-live-root-task). The live
observation updates its deadline without counting the grant as task activity;
other observed enclosing caps remain visible and binding.

Live provider qualification, meaningful-progress assessment, suspected-stall
recovery, and extension of independent provider/child/coordinator timers remain
subsequent work.
