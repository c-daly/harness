# Inference HTTP-client ownership

PR17's expanded local evaluation emitted unclosed aiohttp-session warnings even
though model-process and MCP teardown checks passed. A closed response stream
does not imply a closed HTTP client. The installed LiteLLM cache includes timeout
values in its client keys; Harness supplies changing remaining deadlines.

For `openai/` routes with an explicit endpoint and credential (including the
placeholder credential used by local catalogs), the adapter now supplies a
request-owned SDK client. Its HTTP transport closes after its response stream,
on success, failure, output rejection, deadline or cancellation. Cleanup stays
inside the request and is shielded from AnyIO cancellation. A stream-close error
does not skip transport cleanup. The legacy `complete()` wrappers also close
their inner generators synchronously rather than relying on garbage collection.

The adapter reuses LiteLLM's transport factory to preserve TLS/proxy behavior,
but does not place these clients in the SDK's global cache or close other
requests' clients. Connection pooling lasts for one generation, not across
generations. An explicitly supplied `litellm.aclient_session` stays caller-owned.
Other provider types and routes relying on ambient endpoint/credential discovery
retain their existing SDK ownership. This is not a claim that every provider's
transport lifecycle has been qualified. The internal transport-factory integration
is exercised against the locked SDK by real local HTTP regression tests.

`tests/test_inference_clients.py` uses the real LiteLLM/OpenAI/HTTP stack against
a local fixture. It checks repeated varying deadlines, server errors, oversized
output, cancellation before headers and during streaming, AnyIO cancellation,
concurrent calls, borrowed transports and legacy-wrapper closure. The initial
reproduction failed nine tests; a caller-owned transport test already passed.

## Real local-model reproduction

`scripts/check_local_clients.py` sends 24 bounded synthetic requests with varying
deadlines, cancels a real model stream, then sends another request. It observes
HTTP clients and aiohttp sessions after every settled call, verifies terminal
session state and owned-process shutdown, and checks that requests add no SDK
cache entries. Import-created SDK clients are counted separately. Observation retains references so garbage collection cannot conceal
missing explicit cleanup; the observer never closes a client itself.

Use the [pinned offline container recipe](local-model-qualification.md), omitting
the memory-plugin/vault mounts and their environment variable. Replace its Python
arguments with:

```sh
-B -m scripts.check_local_clients --output /reports/client-lifecycle.json
```

The fixture remains the existing 4B quantization. No model is downloaded or
activated by this probe. Its report contains source hashes, dependency versions,
counts and timings; session data is temporary. A passing lifecycle check does
not establish tool accuracy, fallback quality or successful self-improvement.

The [final real-model report](handoffs/2026-09-06-core-agency/local-client-lifecycle.json)
passed in **5.90 seconds**: 26 model streams, including one cancellation, left
**zero open observed HTTP clients or aiohttp sessions at every checkpoint**.
There were 63 observed HTTP clients in total, including readiness probes, and
26 aiohttp sessions. The SDK cache contained four import-created clients before
and after the probe, with **zero new cached clients**. The session settled and
the owned runtime stopped.

The [initial report](handoffs/2026-09-06-core-agency/local-client-lifecycle-initial.json)
failed its cache-total check despite all observed request resources closing.
Inspection reproduced the four entries immediately after importing LiteLLM,
before making any requests. The corrected observer records the starting cache
and compares new client identities; it still checks every observed client's
closed state. The earlier report and its distinct source hashes remain preserved.
The legacy direct-provider path also had a reproduced credential omission;
`complete()` now resolves its configured key environment variable like `infer()`.
