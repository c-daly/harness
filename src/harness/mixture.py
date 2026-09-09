"""Layer C: Mixture-of-Models — coordinate several models on one task.

The unifying primitive is router/gate + expert models + a combiner. Experts are
dispatched through the existing SubagentRunner (per-expert model + FilteredRegistry
tool scoping + child sessions + event logging); cross-endpoint experts work because
Layer A resolves each alias to its own endpoint. Fan-out is concurrent via
asyncio.gather, the same pattern the loop uses for sibling tool calls.

Four strategies, exposed both model-driven (native tools, register_mixture_tools)
and config-driven (a coordination AgentDef with `strategy`/`experts`):
  - ensemble / best-of-N : run N experts, combine by vote or judge synthesis
  - panel (adversarial)  : proposer + independent advisory critics
  - draft_refine         : cheap/local drafts -> strong refines (staged)
  - escalate (cost-aware): cheap first -> verify gate -> premium only on failure
"""

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from harness.agent import DelegationResult
from harness.callctx import current_call_id
from harness.coordination import CoordinationReport, MAX_REPORT_BYTES
from harness.execution import current_scope
from harness.session import Session
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName

if TYPE_CHECKING:
    from harness.subagent import SubagentRunner

MAX_COORDINATION_OUTPUT = 1024 * 1024


@dataclass(frozen=True)
class Expert:
    model: str            # catalog alias
    agent: str | None = None  # optional AgentDef for role/tools/system prompt


def majority_vote(answers: list[str]) -> str:
    """Deterministic combiner: the most common answer by normalized text; ties
    resolve to the earliest occurrence. Callers filter by execution status."""
    counts = Counter(a.strip() for a in answers)
    order = {key: i for i, key in enumerate(counts)}  # O(1) lookups; built once
    winner_norm, _ = max(counts.items(), key=lambda kv: (kv[1], -order[kv[0]]))
    for a in answers:
        if a.strip() == winner_norm:
            return a
    return answers[0]


def _is_veto(critique: str) -> bool:
    """Exact first-line advisory protocol, never user acceptance evidence."""
    lines = critique.strip().splitlines()
    return not lines or lines[0].strip().casefold() != "approve"


_CRITIC_PROMPT = (
    "You are an independent reviewer. Judge whether the PROPOSED answer correctly "
    "and completely addresses the TASK.\n\nRespond with APPROVE on the first line if "
    "it is correct, otherwise VETO on the first line followed by the reason.\n\n"
    "TASK:\n{task}\n\nPROPOSED:\n{proposal}\n"
)
_REFINE_PROMPT = "Improve the DRAFT answer to the TASK.\n\nTASK:\n{task}\n\nDRAFT:\n{draft}\n"
_SYNTH_PROMPT = (
    "Several models answered the TASK. Synthesize the single best answer.\n\n"
    "TASK:\n{task}\n\nCANDIDATE ANSWERS:\n{answers}\n"
)
_VERIFY_PROMPT = (
    "Does the ANSWER correctly address the TASK? Reply PASS on the first line if so, "
    "otherwise FAIL.\n\nTASK:\n{task}\n\nANSWER:\n{answer}\n"
)


def _usable(result):
    return result.status == "completed" and not result.truncated


async def _fanout(calls):
    tasks = [asyncio.create_task(call) for call in calls]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _coordinate(strategy, runner, parent, prompt, experts, *, judge=None):
    """Preserve participant facts even when a coordinator is interrupted."""
    from harness.events import CoordinationFinished
    scope = current_scope.get()
    parent = scope.session if scope is not None else parent
    members = []
    report = {"version": 1, "id": uuid4().hex, "strategy": strategy,
              "acceptance": "unverified", "members": members, "disagreement": False,
              "gate": "none", "unresolved": [], "source_session_id": parent.id if parent else None}

    async def run(expert, role, text=prompt):
        member = {"role": role, "model": expert.model, "agent": expert.agent,
                  "result": {"status": "cancelled", "reason": "no delivered outcome"}}
        members.append(member)

        def observe(result):
            member["result"] = result.model_dump(mode="json", exclude={"text"})

        try:
            result = await runner.run_result(prompt=text, model=ModelId(expert.model),
                                             parent=parent, agent=expert.agent, on_result=observe)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            observe(DelegationResult(status="failed", reason=type(exc).__name__))
            raise
        result = DelegationResult.model_validate(result.model_dump())
        observe(result)
        return result

    async def execute():
        if not experts or len(experts) > 16:
            return DelegationResult(status="blocked", reason="coordination requires 1 to 16 experts")
        if (strategy == "draft_refine" and len(experts) > 2 or
                strategy == "escalate" and len(experts) > 3):
            return DelegationResult(status="blocked", reason="too many experts for this strategy")
        if any(not isinstance(e.model, str) or not 1 <= len(e.model) <= 128 or
               e.agent is not None and (not isinstance(e.agent, str) or not 1 <= len(e.agent) <= 128)
               for e in [*experts, *([judge] if judge else [])]):
            return DelegationResult(status="blocked", reason="expert aliases and agent names require 1 to 128 characters")
        if strategy == "ensemble":
            results = await _fanout([run(e, "expert") for e in experts])
            usable = [r for r in results if _usable(r)]
            if not usable:
                return DelegationResult(status="failed", reason="all experts failed or returned partial output")
            report["disagreement"] = len({r.text.strip() for r in usable}) > 1
            report["gate"] = "synthesis" if judge else "text_vote"
            if judge:
                answers = "\n\n".join(f"[{i + 1}] {r.text}" for i, r in enumerate(usable))
                return await run(judge, "judge", _SYNTH_PROMPT.format(task=prompt, answers=answers))
            return DelegationResult(status="completed", text=majority_vote([r.text for r in usable]))
        if strategy == "panel":
            proposal = await run(experts[0], "proposer")
            if not _usable(proposal):
                return proposal
            critiques = await _fanout([run(c, "critic", _CRITIC_PROMPT.format(task=prompt, proposal=proposal.text))
                                       for c in experts[1:]])
            report["gate"] = "advisory_review" if critiques else "unreviewed"
            vetoes = [c for c in critiques if not _usable(c) or _is_veto(c.text)]
            if not vetoes:
                return proposal
            report["disagreement"] = any(_usable(c) and _is_veto(c.text) for c in critiques)
            return DelegationResult(status="incomplete", reason="review did not unanimously approve",
                text=f"REJECTED by {len(vetoes)}/{len(critiques)} critics.\n\nPROPOSAL:\n{proposal.text}\n\nVETOES:\n" +
                     "\n\n".join(f"- {v.render()}" for v in vetoes))
        if strategy == "draft_refine":
            draft = await run(experts[0], "drafter")
            if not _usable(draft):
                return draft
            return await run(experts[1] if len(experts) > 1 else experts[0], "refiner",
                             _REFINE_PROMPT.format(task=prompt, draft=draft.text))
        if strategy == "escalate":
            answer = await run(experts[0], "cheap")
            passed = _usable(answer)
            report["gate"] = "execution_only"
            if passed and len(experts) > 2:
                verdict = await run(experts[2], "verifier", _VERIFY_PROMPT.format(task=prompt, answer=answer.text))
                lines = verdict.text.strip().splitlines()
                passed = _usable(verdict) and bool(lines) and lines[0].strip().casefold() == "pass"
                report["gate"] = "advisory_review"
                report["disagreement"] = _usable(verdict) and not passed
            if passed:
                return answer
            return await run(experts[1] if len(experts) > 1 else experts[0], "premium")
        return DelegationResult(status="blocked", reason=f"unknown strategy {strategy!r}")

    def finish(result):
        if any(m["result"]["status"] != "completed" or m["result"].get("truncated") for m in members):
            report["unresolved"].append("one or more participants did not deliver a complete result")
            if result.status == "completed" and strategy != "escalate":
                result = result.model_copy(update={"status": "incomplete", "reason": "partial participant results"})
        if report["disagreement"]:
            report["unresolved"].append("participants disagreed; inspect the alternatives")
        if result.truncated and result.status == "completed":
            result = result.model_copy(update={"status": "incomplete", "reason": "truncated participant output"})
        encoded = result.text.encode()
        if len(encoded) > MAX_COORDINATION_OUTPUT:
            result = result.model_copy(update={"status": "incomplete", "reason": "coordination output limit",
                "text": encoded[:MAX_COORDINATION_OUTPUT].decode("utf-8", errors="ignore"), "truncated": True})
        output = parent.blobs.put(result.text.encode()) if parent is not None else None
        report["result"] = result.model_dump(mode="json", exclude={"text"})
        report["output"] = output.model_dump() if output else None
        if parent is not None:
            CoordinationReport.model_validate(report)
            data = json.dumps(report, sort_keys=True).encode()
            if len(data) > MAX_REPORT_BYTES:
                raise ValueError("coordination report exceeds 1 MiB")
            ref = parent.blobs.put(data)
            parent.append(CoordinationFinished(id=report["id"], call_id=current_call_id(),
                strategy=strategy, status=result.status, report=ref))
            result = result.model_copy(update={"report": ref, "report_session_id": parent.id})
        return result

    try:
        result = await execute()
    except asyncio.CancelledError:
        finish(DelegationResult(status="cancelled", reason="cancelled"))
        raise
    except Exception as exc:
        finish(DelegationResult(status="failed", reason=type(exc).__name__))
        raise
    return finish(result)


async def ensemble(
    runner: "SubagentRunner",
    parent: Session,
    prompt: str,
    experts: list[Expert],
    *,
    judge: Expert | None = None,
) -> str:
    """Run every expert on the same prompt concurrently; combine by judge
    synthesis when a judge is given, otherwise by majority vote."""
    return (await _coordinate("ensemble", runner, parent, prompt, experts, judge=judge)).render()


async def panel(
    runner: "SubagentRunner",
    parent: Session,
    prompt: str,
    *,
    proposer: Expert,
    critics: list[Expert],
) -> str:
    """Proposer drafts; independent critics (ideally on different models) review
    concurrently. Return the proposal only with complete affirmative reviews.
    These opinions never accept the user's task."""
    return (await _coordinate("panel", runner, parent, prompt, [proposer, *critics])).render()


async def draft_refine(
    runner: "SubagentRunner",
    parent: Session,
    prompt: str,
    *,
    drafter: Expert,
    refiner: Expert,
) -> str:
    """Cheap/local expert drafts, then a strong expert refines (sequential)."""
    return (await _coordinate("draft_refine", runner, parent, prompt, [drafter, refiner])).render()


async def escalate(
    runner: "SubagentRunner",
    parent: Session,
    prompt: str,
    *,
    cheap: Expert,
    premium: Expert,
    verify: Expert | None = None,
) -> str:
    """Cheap expert first; a verify gate decides whether to escalate to premium.
    The optional verifier is advisory. With no verifier, only execution status
    and truncation determine escalation. Neither policy verifies acceptance."""
    return (await _coordinate("escalate", runner, parent, prompt,
                              [cheap, premium, *([verify] if verify else [])])).render()


async def run_strategy(
    strategy: str,
    runner: "SubagentRunner",
    parent: Session,
    prompt: str,
    experts: list[Expert],
) -> str:
    """Config-driven entry: positional experts by convention per strategy
    (ensemble: all; panel: proposer + critics; draft_refine: drafter, refiner;
    escalate: cheap, premium[, verify])."""
    return (await run_strategy_result(strategy, runner, parent, prompt, experts)).render()


async def run_strategy_result(strategy, runner, parent, prompt, experts):
    return await _coordinate(strategy, runner, parent, prompt, experts)


# --- model-driven native tools ---

def _experts(models: Any) -> list[Expert]:
    if isinstance(models, str):
        models = (models,)  # a bare scalar means a one-expert list, not chars
    return [Expert(model=str(m)) for m in (models or [])]


@dataclass
class EnsembleTool:
    runner: "SubagentRunner"
    parent: Session
    spec: ToolSpec = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.spec = ToolSpec(
            name=ToolName("ensemble"),
            description=(
                "Run several models on the same prompt and combine their answers. "
                "Args: prompt (required), models (required list of catalog aliases), "
                "judge (optional alias to synthesize; default = majority vote). "
                "Execution results and disagreement are recorded; this does not accept the task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "models": {"type": "array", "minItems": 1, "maxItems": 16, "items": {"type": "string"}},
                    "judge": {"type": "string"},
                },
                "required": ["prompt", "models"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        judge = Expert(model=args["judge"]) if args.get("judge") else None
        return await ensemble(
            self.runner, self.parent, args["prompt"], _experts(args.get("models")), judge=judge
        )


@dataclass
class ConsultPanelTool:
    runner: "SubagentRunner"
    parent: Session
    spec: ToolSpec = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.spec = ToolSpec(
            name=ToolName("consult_panel"),
            description=(
                "Advisory review: a proposer drafts, independent critics on other "
                "models approve or veto. Args: prompt (required), proposer (required "
                "alias), critics (required list of aliases)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "proposer": {"type": "string"},
                    "critics": {"type": "array", "maxItems": 15, "items": {"type": "string"}},
                },
                "required": ["prompt", "proposer", "critics"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        return await panel(
            self.runner,
            self.parent,
            args["prompt"],
            proposer=Expert(model=args["proposer"]),
            critics=_experts(args.get("critics")),
        )


@dataclass
class EscalateTool:
    runner: "SubagentRunner"
    parent: Session
    spec: ToolSpec = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.spec = ToolSpec(
            name=ToolName("escalate"),
            description=(
                "Cost-aware: try a cheap model, escalate to a premium model only if an "
                "execution or advisory verify gate fails. This does not verify task acceptance. "
                "Args: prompt (required), cheap (required alias), "
                "premium (required alias), verify (optional alias gating escalation)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "cheap": {"type": "string"},
                    "premium": {"type": "string"},
                    "verify": {"type": "string"},
                },
                "required": ["prompt", "cheap", "premium"],
            },
        )

    async def __call__(self, args: dict[str, Any]) -> str:
        verify = Expert(model=args["verify"]) if args.get("verify") else None
        return await escalate(
            self.runner,
            self.parent,
            args["prompt"],
            cheap=Expert(model=args["cheap"]),
            premium=Expert(model=args["premium"]),
            verify=verify,
        )


def register_mixture_tools(registry, *, runner: "SubagentRunner", parent: Session) -> None:
    """Register the coordination natives. Call right after the SubagentRunner is
    built (alongside DispatchAgentTool) so experts dispatch through it."""
    registry.register(EnsembleTool(runner=runner, parent=parent))
    registry.register(ConsultPanelTool(runner=runner, parent=parent))
    registry.register(EscalateTool(runner=runner, parent=parent))
