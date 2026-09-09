# Destination loss and interruption during handoff

Implementation and protocol are frozen before the real-model journeys. Results
are pending. The protocol runs busy, destination-loss and Esc cases first without
plugins, then with the installed normal memory plugin and read-only normal vault.

The public process test reproduced false completion: after a successful B write,
killing the HTTP destination during its next response produced a fabricated
`end_turn`, followed by a completed handoff. LiteLLM's OpenAI stream wrapper can
synthesize a finish reason at EOF. The adapter now requires evidence that the
wrapper received a provider finish reason before emitting a terminal chunk.
Truncated text and complete-looking tool arguments fail with `MalformedStreamError`.
Provider exception bodies remain suppressed and owned HTTP clients close.

Busy preflight and started failure have different recovery rules. A busy refusal
does not consume the handoff record; after settling the other request, a still
current record can be used. A started loss or cancellation consumes the record.
The new checkpoint must retain completed B and reject another write to B, then a
fresh reconciliation authorizes only C. Task acceptance remains explicit.

The source process is a controlled Codex-compatible fixture with real MCP, not a
live subscription agent. The CLI runs actual destination inference; the pytest
fixture instead uses scripted HTTP responses. The long numeric response is a
fault trigger, not a semantic or task-quality benchmark. These checks qualify one
offline CUDA profile and do not establish provider parity or general portability.
