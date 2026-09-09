# Normal-memory handoff failure and recovery

PR37 merged at `216762c`. This increment advances the independent M4 handoff
track while semantic context selection remains advisory and unqualified.

The [protocol](protocol.json) fixes two one-shot offline terminal journeys before
local inference: a plugins-absent control and a required normal-memory failure
followed by explicit reconciliation and session restart. The source process is
a controlled Codex-compatible fixture using real MCP, not a live subscription
agent. The destination is the installed Qwen3-8B model. The memory case uses the
installed memory plugin and normal vault through read-only mounts.

The shared operator controls now distinguish a preflight refusal from failure
after an attempt starts. The latter explains that its record was used and directs
the operator to inspect and record a new handoff. A returned incomplete outcome
gets the same guidance. Provider error bodies remain suppressed, and cancellation
retains its existing interruption path.

Every declared check is critical. A handoff cannot pass on caught exceptions or
a partially populated report. The memory failure must precede inference, preserve
artifacts and the draft, leave task acceptance outstanding, refuse the used
record, and settle its intents. The resumed continuation must retrieve fresh
normal memory, finish exactly B, preserve A/native effects and task identity,
retain original tool evidence, display failure and recovery, and stop its owned
processes. The 45-second task limit remains fixed.

Only metadata is retained. Temporary sessions containing normal memory are
deleted; no private payloads, generated prose, raw logs or terminal screenshots
are exported. Results will be recorded after the protocol/implementation freeze.
