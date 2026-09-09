# Reconcile interrupted external work

An external agent can change files through Harness tools and through its own
built-ins. A disconnect, cancellation, or incomplete response does not prove that
nothing happened. Harness therefore holds another attempt on the same tracked
task until an operator reconciles the effects and explicitly starts a bounded
continuation.

This is a core control. It works without plugins. Memory and agent-swarm remain
plugins, with their normal context and workflow contracts.

## Operator workflow

1. Stop the external agent and inspect its effects. Confirm that its process and
   any independent work have stopped. Inspect files and any external systems it
   could have changed; Harness cannot enumerate provider-native effects.
2. Use `/handoff inspect` in the existing session. It lists the task, snapshot
   digest, recorded calls, completed effects, and uncertain effects. For complete
   arguments and artifact references, use the read-only CLI:

   ```sh
   harness handoff inspect SESSION_ID --json > checkpoint.json
   ```

3. Prepare a JSON specification using the current digest and effect IDs. Resolve
   every uncertain effect exactly once, with an inspection note. Specify only the
   remaining assignment, an explicit configured inference alias, and up to 16
   exact native file-tool calls. The example below is a shape reference; its
   digest, IDs and paths must come from the actual checkpoint.

   ```json
   {
     "version": 1,
     "snapshot_sha256": "<64-character snapshot digest>",
     "model": "local-small",
     "continuation": "Write the remaining stage B artifact, then summarize it.",
     "process_stopped": true,
     "resolutions": [
       {
         "effect_id": "native:<recorded call ID>",
         "status": "completed",
         "note": "Inspected the external changes and confirmed the process exited."
       }
     ],
     "allowed_calls": [
       {
         "tool": "write_file",
         "args": {
           "file_path": "/absolute/project/B.txt",
           "content": "stage B\n"
         }
       }
     ],
     "required_tags": []
   }
   ```

   Status is `completed`, `not_applied`, or `uncertain`. Keeping any effect
   `uncertain` records the inspection but holds execution. `process_stopped` is
   an operator attestation; a model cannot supply it through a Harness tool.
4. Run `/handoff record FILE.json`, then `/handoff show ID` to review the saved
   specification and checkpoint. Select its destination with `/model local-small`
   and run `/handoff run ID`. These controls require an idle session. Esc cancels;
   new prompts wait for cancellation to settle. An already-started file operation
   finishes before the interrupted attempt is recorded, because a worker thread
   cannot safely be preempted. The terminal shows that wait.
5. Use `/task check` and inspect the resulting files. Earlier successful tool-result
   evidence can retain its original event and artifact through an explicit
   handoff. Output checks and operator review still require fresh evidence.
   Completion and reconciliation do not accept the task; explicit review and
   acceptance remain separate controls.

If an attempt fails after starting, the terminal and headless controls report
`Handoff failed` or `Handoff incomplete` and explain that the record was used.
This includes a required-memory failure before the first model call. Restore the
source, resume the same session with its original catalog/workspace/MCP settings,
inspect the new checkpoint, and record a fresh handoff for only the remaining
work. A preflight refusal is labelled `Handoff refused`; that command did not
start a new attempt. Restoring a service does not automatically retry work.

For headless recording and execution, use the same catalog, workspace, native
tools, and permission configuration as the source session:

```sh
harness handoff record SESSION_ID handoff.json --model local-small \
  --catalog models.toml --native-tools --workspace /absolute/project
harness handoff show SESSION_ID HANDOFF_ID
harness handoff run SESSION_ID HANDOFF_ID --model local-small \
  --catalog models.toml --native-tools --workspace /absolute/project
```

All commands accept `--base-dir` for a non-default session store. `inspect` and
`show` do not instantiate a provider or execute inference. The headless mutation
commands do not configure MCP servers; use the existing TUI session when its
context profile requires plugin sources. A missing or changed context adapter
holds the continuation.

## Execution contract

The saved checkpoint includes task identity, criteria, unresolved requirements,
artifact references, effect provenance, source authority, and budgets. The fresh
model context contains the checkpoint, inspection notes, remaining assignment,
exact allowlist, and configured project/context sources. The old conversation is
retained in the event log but excluded from that continuation's working context.
The checkpoint and notes are labelled as historical data.

Each allowed call is attempted at most once, even if the model repeats it or
proposes calls in parallel. Arguments must match exactly, use canonical absolute
paths, and contain no ignored fields. Supported tools are `read_file`, `write_file`,
`edit_file`, `glob`, and `grep`; shell, delegation, and arbitrary MCP actions cannot
be authorized. A completed write/edit target cannot be changed by this tranche,
even with different contents or a relative source path. Finish separate remaining
artifacts. Reads of completed files can be allowed explicitly.

Normal dispatch hooks and permissions still apply. The source permission layers,
including its session grants, form an additional floor after current routing and
rewrites. A current allow cannot override a source deny. Source and current limits
are intersected, and cumulative call reservations are conservatively restored
from recorded attempts across restart. The destination alias and its declaration
are pinned; it must resolve to native inference and satisfy any required tags.
Context policy and declared adapter bindings must match. Configured context reads
still use ordinary dispatch; they cannot enable native mutations or delegation.
Declared MCP bindings are hashed without copying environment credential values.
This does not attest remote server code or ambient credential identity.

A record becomes single-use when its run starts, including cancellation or crash.
A further attempt needs a new checkpoint and reconciliation. A chained handoff
retains earlier completed effects, inspection provenance, artifacts, and authority.
Replay never launches inference. Models have no reconciliation or acceptance tool.

## Current limits and evidence

The first implementation supports external-to-native continuation after explicit
inspection. External-to-external continuation, portable workflow graphs, child
session accounting, arbitrary source policy hooks, and automatic reconciliation
remain unqualified. Sessions with active or subsequently spawned child sessions
are held. Legacy attempts without a captured scope, or attempts from a different
core source version, are also held. An application upgrade requires a separately
reviewed migration contract; it cannot silently reinterpret old authority.

The source implementation fingerprint covers the shipped core Python files.
Native workspace bindings and declared MCP context configuration are checked
again at dispatch. This is application-level enforcement, not operating-system
isolation from other processes or a sandbox for provider-native actions. Historical
tool success proves the recorded result, not that another process has left the
artifact unchanged. Operator inspection and explicit acceptance remain necessary.

The [original evidence record](handoffs/2026-09-08-external-handoff/README.md) separates
regressions, terminal rendering, a controlled external subprocess with real MCP,
and an offline Qwen3-8B continuation. The subprocess is a public Codex-compatible
fault fixture, not a live subscription agent. The
[normal-memory recovery record](handoffs/2026-09-09-handoff-memory-recovery/README.md)
adds an offline terminal journey with required-memory loss, a refused reused
record, and newly reconciled continuation after session restart. Read that record
for observed outcomes and limits; it does not establish general heterogeneous
handoff or live-provider qualification.
