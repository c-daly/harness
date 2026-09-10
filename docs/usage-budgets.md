# Shared usage stop limits

Harness records model attempts in one ledger owned by the root session. Native
agents, delegated agents, coordinators, internal inference, retries, and external
agent responses use the same ledger. This is core behavior with plugins absent.

To stop admitting new work after a cumulative usage threshold:

```sh
harness --model metered --budget-input-tokens 50000 --budget-output-tokens 10000 --budget-cost-usd 2
```

Each flag is optional; omitted limits default to unbounded for a new session.
Zero prevents any model attempt. Negative values, non-finite costs, and invalid
token counts are rejected. The same flags work with `-p`, `--resume`, and
`--continue`. There is no model-call tool for raising or clearing these limits.

Limits apply to **new-call admission against settled usage**. A completed call
can cross a limit; calls already running can also finish and contribute usage.
Their outputs remain available, and no further model call is admitted after the
crossing is recorded. Existing call, depth, child, coordination deadline, and
per-response limits still apply. These stop limits are not reservations of exact
future input tokens or a guarantee about a provider's final bill.

## Accounting declarations and prices

A finite limit requires the effective catalog alias to explicitly declare its
usage reporting. For example, add this to an alias whose responses you have
checked:

```toml
[models.metered]
route = "openai/my-installed-model"
api_base = "http://127.0.0.1:8080/v1"
usage_accounting = "reported"
input_cost_per_token = 0.0
output_cost_per_token = 0.0
```

Here zero prices describe a local model with no per-token provider charge;
electricity and hardware costs are excluded. Use the applicable rates for a
paid route. `usage_accounting` defaults to `"unknown"`; the only other accepted
value is `"reported"`. This is a declared contract, not a capability measurement.
Third-party providers can expose `usage_accounting = "reported"` or a method
`usage_accounting(model)` at the provider boundary. Routing is resolved before
checking the declaration or choosing prices, and rates are copied before work.

A cost limit requires finite, nonnegative input and output prices. The estimate
uses reported input tokens times the recorded input rate plus output tokens
times the recorded output rate, using decimal arithmetic. As with existing
telemetry, cache discounts, separate cache-write charges, subscriptions, and
other billing items are not modeled. No dollar billing-cap claim follows.

Missing reports remain unknown. Known input usage with unknown output usage can
still satisfy an input-only budget. An output or cost budget holds further work
when its required accounting is unknown. Failure, cancellation, a malformed
response, or an interrupted attempt retains any observed high-water token counts
as a lower bound and leaves completeness unknown. A later successful call does
not erase that uncertainty. Under finite limits this can hold an automatic retry
or fallback; the failed attempt cannot be assumed free.

External agents are metered at the Harness response boundary using their
aggregate reported usage. Harness cannot stop an external runtime's private
internal model calls against these limits. Unknown declarations block bounded
dispatch before the runtime starts. Declaring reported usage does not establish
live external-runtime accounting conformance.

## Inspection and restart

Use `/budget` in the TUI or:

```sh
harness budget SESSION_ID --base-dir /path/to/harness-data
```

Both show the root session, configured limits, reported totals, unknown and
pending attempts, and the last admission refusal. Inspection is read-only,
does not call a model, and does not accept a task. `/budget` remains a core
command when a plugin defines a command with that name.

The attempt ledger includes reported usage from unsuccessful attempts. Existing
`/stats` response totals can differ after retries or failures because those
views account for completed responses rather than these attempt lower bounds.

Every provider attempt has a durable start before provider entry and a terminal
usage record after cleanup, including retries and cancellations. Child sessions
point to the same root ledger; totals do not depend on scanning child logs.
Resuming the root restores its limits and totals. Omission retains saved limits;
an explicit smaller limit narrows them, and an increase is refused before a
resumed boundary is appended. Directly resuming a linked child is refused with
the owning root's session ID, so it cannot obtain a fresh independent allowance.
A new session starts a new accounting scope.

Pending attempts after restart become aborted with unknown usage. Older sessions
with previously untracked model or child work remain usable without finite
usage limits; adding a finite limit holds execution because the prior usage is
unknown. A ledger write failure also holds further model calls until restart
reconciles the pending record. Inspection does not repair records.

Handoff retains the root accounting and cannot widen the captured usage limits.
Portable continuation includes a `usage_budget` snapshot with the root session
and event boundary in `continuation.json`. This snapshot describes source usage;
it does not enforce limits in another frontend or transfer spending authority.

General runtime configuration files, exact pre-call token reservations,
provider billing caps, cross-process shared execution, and live accounting
qualification remain further work. Existing call/depth counters retain their
current live-tree semantics; this change makes the usage ledger durable.
