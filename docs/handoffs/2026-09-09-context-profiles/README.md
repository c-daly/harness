# Public context-selection profile comparison

PR36 merged at `bfe76e3`. The previous 8B prompt and schema experiments failed
their fixed development gates. This experiment compares the two installed model
profiles with the unchanged builtin prompt and core eligibility filtering.

The [protocol](protocol.json) freezes inputs, labels, profiles, run order, limits,
and gates before inference. Run order is 8B, 4B, 4B, 8B. Each block gets a fresh
isolated runtime/session, one unscored warmup, and all six public cases. Both
paired repetitions must pass every correctness and latency gate. All case data
remains public development data, including the case labelled `held_out`.

The [driver](../../../scripts/measure_context_profile.py) verifies pinned weights,
requires bounded offline execution, preserves exact commands and source hashes,
and refuses an existing output directory. It records real core observations and
raw model events, retaining scoped input/prompt blobs. Its comparison function
regrades responses and refuses mismatched profiles, inputs, prompts or sources.
It cannot adopt a model or change a user session.

The smaller model uses full GPU offload; 8B keeps the previously measured
28-layer placement. This compares usable profiles on a shared host. It does not
isolate model size, training version, placement or cache effects. No fresh
confirmation data will be authored during this comparison, and M4 stays open.

Results will be recorded after the protocol and driver freeze is committed.
