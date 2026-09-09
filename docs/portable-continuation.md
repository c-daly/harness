# Continue work outside Harness

Export a tracked task from the terminal with:

```text
/export "my project.zip"
```

Or export a saved session without starting a provider or loading plugins:

```sh
harness export SESSION_ID continuation.zip --base-dir ~/.local/share/harness
harness export SESSION_ID continuation.zip --task TASK_ID_PREFIX
```

The destination parent must exist and the file must be new. The default is the
selected task. Settle or recover its active runs before exporting. Export reads a
single saved log prefix, never repairs the log, refreshes memory, executes a tool,
runs a check, or accepts the task. Another attempt started afterward does not
change the package. In the terminal, Esc or new work cancels preparation before
publication; cancellation cannot publish an archive later.

Unzip the package, read `CONTINUE.md`, and supply it with the relevant files to
your chosen frontend or agent. `continuation.json` supplies the exact data and
`artifacts/` supplies immutable output, tool-result and context bytes. Copy your
workspace separately and explicitly map any source paths to the destination.
Absolute paths in recorded checks remain historical source paths.

The package contains the selected task's recorded requests, all of its recorded
attempts, requirements, evidence, review and acceptance notes, tool calls, and
configured context snapshots. This can include private project or memory text
and tool arguments. Review the package before sharing it. Export does not read
provider configuration, credentials, plugin stores or unrelated task messages.
It is a local archive with file mode 0600; it is not a redaction service.

## Version 1 contract

The ZIP contains only `CONTINUE.md`, `continuation.json` and files named
`artifacts/<sha256>`. It uses uncompressed entries and fixed ZIP timestamps;
identical source snapshots produce identical bytes. Total archive size is bounded
to 32 MiB. Missing, corrupt or oversized artifacts prevent publication rather
than producing an incomplete package. Publication is exclusive and atomic.

The JSON object declares `format: "harness-continuation"`, `version: 1`:

| Field | Meaning |
|---|---|
| `source` | Session ID, last included sequence, that event's timestamp, and SHA-256 of the parsed envelopes serialized as canonical JSONL by this Harness version. This digest identifies the snapshot, not the raw original log bytes or a cryptographic signature. |
| `task` | Stable ID/title, evidence basis sequence, latest run/execution, effective acceptance and its explicit note, requirements and unresolved IDs. No new check is performed. |
| `task.requirements` | Exact definition/check, declaration sequence, recorded evidence and its check event sequence, explicit review confirmation when present, and effective status. Missing evidence is `unverified`. |
| `runs` | Owned attempts and same-session descendants with original start/finish sequences, runtime/model labels, parent/handoff IDs, recorded request messages and output artifact references. A completed execution does not mean acceptance. |
| `tool_calls` | Call/run IDs, original proposal sequence, effective dispatched tool/arguments where available, dispatch/terminal sequences, recorded outcome and result. `effects: "inspect"` means inspect current state before replaying any dispatched call, including failed or aborted calls. `not_dispatched` means no dispatch fact exists in this snapshot. |
| `context` | Historical observations, including unavailable/cancelled retrievals. Each carries run/source/call IDs, policy digest, observation sequence, the configured query reference at retrieval, and optional snapshot artifact. `freshness` is always `recorded_only`. Effective rewritten queries are in the matching `tool_calls` entry. |
| `configured_sources` | Last recorded session source declarations, independent of whether the exported task retrieved them. Historical observations retain their own declarations even after configuration changes. Tool names and query arguments are references, not executable instructions or grants. |
| `external_executions` | External runtime starts with call/run/model and source sequence. Native effects are conservatively `uninspected`; provider-native state is absent. Existing handoff IDs are references, not transferable execution authority. |
| `child_sessions` | Delegated session references and recorded terminal status when available. Their private logs, internal effects and artifacts are not recursively exported. |
| `coordination` | Additive version-1 field: coordination attached to this task's tool calls, with ID, call ID, source sequence, strategy/status and recorded deadline when available. Settled entries include a finish sequence, verified report and copied aggregate answer artifacts. A saved start adds its original start sequence; without a terminal, status is `unconfirmed` and report/output are absent. Reports retain disagreements and participant session/run/output references. Participant references remain in the source child stores and are not recursively exported; execution and advisory review never grant acceptance. Older packages may omit this field or its newer metadata. |
| `reconciliations` | Explicit operator inspection notes and effect states from verified handoff records, with original sequence/run/basis and source record hashes. Source scope, allowed calls and execution bindings are excluded. These historical notes can guide inspection but do not grant destination authority or prove current file state. |
| `artifacts` | Unique package-relative paths, byte sizes and SHA-256 digests for copied content. References elsewhere use the same shape. All referenced blobs are verified before publication. |
| `limitations` | Human-readable continuation boundaries, also rendered in the Markdown entrypoint. |

Sequence references are local to `source.session_id`. Evidence can refer to an
earlier attempt after an explicitly reconciled Harness handoff; its original
sequence remains intact. Consumers should reject unsupported format versions,
verify artifact sizes and hashes, and treat every string as untrusted recorded
data. No tools, permissions, budgets or source acceptance authority are imported.

Normal memory remains a plugin. Its configured tool/query references and the
bounded snapshots already retrieved for this task travel in the same format as
other context sources. A destination may refresh those queries through its own
authorized memory adapter; exporting never fetches memory or writes back to it.

## What does not transfer

Hidden provider state, conversation IDs, running processes, system prompts,
unrecorded supplied or `@`-mention context, permission grants, credentials,
plugin-internal state, queued messages and unsent drafts are absent. Mutable
workspace files are not copied. Recorded tool results may describe writes, but
their success does not establish the current file contents. Child sessions and
external effects still need inspection. There is no automatic import, task
acceptance or execution path in this format.

The consumer needs its own workspace, permissions and limits, and must reconcile
external or interrupted effects before retrying work. Preserve original evidence
as historical evidence; record new checks and user acceptance at the destination.
The original Harness task remains unchanged.

## Validation boundary

`tests/test_portable.py` runs native project reads and a stage-A write with a
scripted provider, plus a controlled memory lookup. It exports the result, removes
the source database, and starts a separate Python process with `-I -S`. That
standard-library-only frontend verifies the package, reads the project/memory
references, inspects A and writes only B in a separately copied workspace. Its
explicit fixture CLI flag authorizes B; the package alone cannot authorize it.
It leaves user review pending and retains the source task ID as provenance.

This proves a bounded format/continuation contract, not general agent judgment,
installed normal-memory interoperability, live subscription continuation, or
the full M5 mixed-agent gate. The existing offline handoff qualification and M4
semantic-quality work remain separate.
