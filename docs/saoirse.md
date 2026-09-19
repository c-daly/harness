# Saoirse: Harness resident and optional interfaces

Status: architecture direction agreed September 16, 2026. This document defines
ownership and acceptance criteria; it does not claim that the Session Desk
integration or a shared resident attachment interface is implemented.

Saoirse is the Harness resident agent. Her knowledge and capabilities must remain
usable through Harness when the voice/desktop adapter is absent. Session Desk is
one interface to that resident. Resident improvements belong primarily in Harness
and its existing plugin contracts, so terminal, headless, and future clients can
use them too.

## Ownership

| Responsibility | Owner |
| --- | --- |
| Resident identity, behavior, model selection, task execution and lifecycle | Harness |
| Project/session knowledge, context selection, evidence provenance and freshness | Harness, using configured sources and existing plugins |
| Memory storage, retrieval and write semantics | The configured memory plugin; Harness owns its integration and normal policy enforcement |
| Durable tasks, conversation history, requirements, recovery and action receipts | Harness event logs and existing continuation mechanisms |
| Reusable cross-provider session discovery and supported control operations | Harness or reusable Harness integrations |
| Microphone, local recognition, spoken rendering, global hotkey and desktop layout | Session Desk |
| Selected tab, visible filters, headset choice and presentation preferences | Session Desk; relevant selection is supplied as explicit client context |

Use the existing [resident workflow](resident-workflow.md),
[local assistant](local-assistant.md), and [core-agency roadmap](superpowers/plans/2026-09-06-core-agency-roadmap.md).
Memory remains a plugin. Do not create a competing adapter-owned knowledge store,
parallel task engine, or a separate Saoirse persona for each interface.

## Shared resident contract

Clients submit requests with explicit conversation/task and project context and
consume progress, results, source references, and interaction requests from
Harness. Expose the capabilities actually available under the configured profile.
Reuse Harness task, event, dispatcher and continuation contracts; choose any new
transport only after auditing the existing interfaces.

Keep machine, environment, project directory, provider, model, and session identity
when referring to other agents. Shared resident identity and knowledge do not mean
merging every project's conversation into one prompt. Preserve scoped histories
and resolve an ambiguous destination before acting.

UI selection is transient context, not automatically a durable fact about the
user's intent. A missing desktop client must not make project or memory retrieval
fail. UI-only actions such as opening a viewer tab are available only when that
client is connected. Generic knowledge and agent operations must not depend on
imports from the voice adapter or its HTTP server.

Harness owns session writer/lifecycle coordination. Multiple interfaces must not
open competing writers or replay a submitted action during reconnect. Closing
an interface must not implicitly cancel the resident's tasks; explicit task
cancellation and resident shutdown remain separate operations.

## Current implementation and migration

Session Desk currently constructs a dedicated Harness kernel inside its own
worker, supplies a desk-specific prompt and five tools, and keeps its Harness
session under `.session-desk/harness`. This uses real core mechanisms, but it is
not yet attachment to a Harness-owned resident shared with other interfaces.

First audit the existing resident capabilities and identify the smallest missing
Harness entrypoint. Implement reusable knowledge/context and session operations
there, including memory-plugin integration where configured. Then adapt Session
Desk to consume that entrypoint and supply optional desk context and UI actions.
Preserve existing session IDs, event logs, blob sidecars, source links and user
preferences during any storage or ownership migration. Do not infer live agent
status from transcript presence or advertise control that a provider cannot offer.

## Acceptance criteria

1. With Session Desk and its worker stopped, a Harness-only interface can use the
   configured project and normal-memory sources, perform supported resident work,
   and report unavailable optional capabilities accurately.
2. Restart/resume retains task identity and requirements and retrieves fresh source
   evidence. Changed source records supersede stale observations with provenance.
3. Connecting the desktop interface exposes that resident's state and capabilities
   without creating a separate knowledge store or competing session writer.
4. Disconnecting/reconnecting the adapter preserves resident work and does not
   duplicate submitted actions. Desktop preferences remain client-owned.
5. The same resident knowledge checks run through Harness alone and the adapter;
   voice/device behavior has its own tests. A subsequent two-provider exercise
   verifies exact targeting and supported session operations before scaling up.
