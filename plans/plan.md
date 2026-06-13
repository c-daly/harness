# Plan: Mixture-of-Models — a multi-model coordination layer for the harness

## Context

The harness was designed multi-model-first, but the current wiring binds **one provider with one endpoint** at kernel-build time (`LiteLLMProvider(api_base=resolved.api_base)` in `cli.py`). Consequences confirmed in the code this session:

- A **local** model (custom `api_base`) and a **cloud** model can't coexist in one session — the single baked-in `api_base` sends every call to one endpoint regardless of the model string.
- The only model-selection levers are the `--model` launch flag, the TUI `/model` switch, and per-subagent `AgentDef.model`. There's no *policy* for which model handles a given turn.
- There is **no reduce/combiner primitive**: a subagent returns its final text (`subagent.py:66,79`) and the *parent model* aggregates by reading results. No router/ensemble/voting/synthesis primitive exists anywhere.

What the user wants (from this brainstorm): (1) mix local + cloud freely in one session; (2) routing by **general rules**, **overridable by an explicit model request**, with **opt-in "agent leeway"** to let the model decide; (3) the four coordination patterns — adversarial panel, ensemble/best-of-N, draft→refine, cost-aware escalation — unified as a **"Mixture-of-Models"**: models as experts, with a router/gate and a combiner.

**Key enabling fact (grounded):** `dispatcher.dispatch_model` already calls `provider.complete(model=effective.model, …)` using the model *after* the hook chain (`dispatcher.py:182-198`), and the main loop dispatches through it (`loop.py:92`). So **context-based model routing is expressible as a dispatch-hook `Rewrite` with zero new dispatch plumbing.** The `Catalog` already stores `api_base`/`api_key_env`/cost/tags per model (`catalog.py`). This turns much of the work into wiring + reuse rather than new machinery.

Intended outcome: any catalog model reachable in one session, a layered routing policy, and a Mixture-of-Models coordination primitive offering the four patterns.

## Approach — three incremental layers (A → B → C)

Implement A→B→C in order; each is independently shippable and testable. C is the largest and is naturally its own follow-up once A+B land.

### Layer A — Catalog-aware provider (the enabler; everything depends on this)
Make the provider resolve **endpoint + key per call** from the model string instead of baking one in.
- New `CatalogProvider` wrapping LiteLLM (in `provider_litellm.py`, or a new `catalog_provider.py`): holds the `Catalog`; on `complete(model=X)` it resolves `Catalog.resolve(X)` → applies that model's `api_base` and dereferences `api_key_env` from the env → delegates with per-call `api_base`/`api_key`; falls back to ambient env when a field is unset.
- Reuse: `Catalog.resolve` (`catalog.py`) already returns `route`/`api_base`/`api_key_env`; `ResolvedModel.pricing_dict()` for telemetry.
- Result: a `local`-pinned subagent under a `gpt` parent (or vice versa) works in one session.

### Layer B — Layered routing policy (rules + explicit override + opt-in leeway)
A `RoutingEngine` registered as a dispatch hook on `ProposedModelCall` that `Rewrite`s `effective.model` by precedence:
1. **Explicit** (highest): an explicit per-turn / per-agent model request passes through untouched.
2. **Rules**: a `RoutingRuleSet` (TOML, layered user→project, modeled on `permissions.py`'s `RuleSet`/`PermissionEngine`) matching context signals (task/prompt tags, in-scope file paths, a cost ceiling, message size) → a target model alias.
3. **Agent leeway** (opt-in setting): if enabled and no rule matched, defer to a cheap "router" model (or heuristic) to pick — the "let the agent decide" mode.
- Register on the `HookBus` so routing runs **before** the permission engine, so the `model:<route>` permission check sees the final route.
- Reuse: the layered-precedence `RuleSet`/`PermissionEngine` design (`permissions.py`); the `Rewrite(ProposedModelCall(...))` path (`dispatcher.py`/`hooks.py`); the session fold for context signals. Composes with Layer A: routing rewrites the model string, the catalog-aware provider follows it to the right endpoint.

### Layer C — Mixture-of-Models coordination + combiner (the greenfield piece)
The unifying primitive: router/gate + expert models + a **combiner** (new reduce seam). Expose the four patterns as strategies, available both **model-driven** (native tools the orchestrating model convenes) and **config-driven** (declarative coordination agent-defs) — same precedence philosophy as routing.
- New `mixture.py` reusing `SubagentRunner` to fan out to expert models (concurrent via the loop's existing `asyncio.gather` over sibling calls), with strategies:
  - **ensemble / best-of-N** — run N experts, combine by deterministic vote or judge-model synthesis.
  - **adversarial panel** — proposer + independent critics on different models; accept iff no veto / majority approve (the exact pattern that built the harness).
  - **draft → refine** — cheap/local expert drafts → strong expert refines (staged).
  - **cost-aware escalation** — cheap first → confidence/verify gate → escalate to premium only on failure.
- Surface as native tools (e.g. `consult_panel`, `ensemble`, `escalate`) registered via `native_tools.register_native_tools`, and/or coordination agent-defs.
- Reuse: `SubagentRunner` (`subagent.py`) for expert dispatch + per-agent model + `FilteredRegistry` tool scoping; Layer A so experts can be cross-endpoint; the event log (`ModelCallCompleted` pricing) for cost-aware decisions and post-hoc "which expert won" telemetry.

## Critical files
- `src/harness/provider_litellm.py` (or new `catalog_provider.py`) + `src/harness/cli.py` (`build_kernel`/`_run_main`) — per-call endpoint/key. **[A]**
- new `src/harness/routing.py` + `cli.py` wiring — `RoutingEngine` hook + `RoutingRuleSet`, modeled on `permissions.py`. **[B]**
- new `src/harness/mixture.py` + `src/harness/native_tools.py` (register coordination tools) — combiner + the four strategies. **[C]**
- `catalog.py` — no change; already carries `api_base`/`api_key_env`/cost/tags. Tests under `tests/` per the repo's tests-first pattern.

## Verification (end-to-end, per layer)
- **A:** local server up + real `OPENAI_API_KEY`; one session where the parent runs `gpt` and a `local`-pinned subagent executes → event log shows two `ModelCallCompleted` with different models, one hitting localhost and one hitting OpenAI, both `ok`.
- **B:** a `tests→local` routing rule; a turn scoped as tests → log shows the model rewritten to `local`; `--model gpt` explicit overrides it; enabling leeway → unmatched turns get a router-chosen model.
- **C:** `consult_panel` with a deliberately wrong proposer → an independent critic on a different model vetoes; ensemble best-of-N returns the voted/synthesized answer; escalation runs cheap-then-premium only on failure — each asserted via the per-model call sequence in the event log.
- **Throughout:** full suite green after each layer (TDD), independent (different-model) review per task per the established loop, and `harness stats` to confirm per-model cost attribution.

## Notes
- Build-process caveat: read-only Explore/Plan subagents are blocked by the agent-swarm routing layer in this environment (they can't reach file tools); grounding/exploration was done directly. Implementation subagents (which use `mcp-call`) work, as proven across Phases 7–9.
- A natural stopping point is after A+B (mixing + routing fully usable); C can be a dedicated follow-up given its size.
