# Live activity inspection

During a running turn, the TUI shows elapsed time, time since the most recent
observed activity, and a phase or explicit wait. It refreshes once a second even
when the provider is silent. Use `/activity` for the full view, including while
the turn is running or follow-ups are queued.

Inspection is a core interface operation. It does not invoke a model, write to
the session log, change the prompt queue, or require a network connection. A
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

## Interpretation and limits

**Activity does not verify progress toward the user's goal. Silence does not
establish a hang.** A stream event can be reasoning, output, tool-call data,
usage, or a stop marker. A quiet provider can still be doing useful work beyond
Harness's visibility. A broad phase is the most recent instrumented phase;
provider-internal steps and cleanup are not inferred from silence.

Existing finite execution budgets remain enforced. This feature does not
cancel work for inactivity, extend deadlines automatically, or run semantic
classification. An operation stays visible until its wrapped execution and
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
