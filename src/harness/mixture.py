"""Layer C: Mixture-of-Models — coordinate several models on one task.

The unifying primitive is router/gate + expert models + a combiner. Experts are
dispatched through the existing SubagentRunner (per-expert model + FilteredRegistry
tool scoping + child sessions + event logging); cross-endpoint experts work because
Layer A resolves each alias to its own endpoint. Fan-out is concurrent via
asyncio.gather, the same pattern the loop uses for sibling tool calls.

Four strategies, exposed both model-driven (native tools, register_mixture_tools)
and config-driven (a coordination AgentDef with `strategy`/`experts`):
  - ensemble / best-of-N : run N experts, combine by vote or judge synthesis
  - panel (adversarial)  : proposer + independent critics; accept iff no veto
  - draft_refine         : cheap/local drafts -> strong refines (staged)
  - escalate (cost-aware): cheap first -> verify gate -> premium only on failure
"""

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harness.session import Session
from harness.tools import ToolSpec
from harness.types import ModelId, ToolName

if TYPE_CHECKING:
    from harness.subagent import SubagentRunner

_SUBAGENT_ERROR = "[subagent error]"


@dataclass(frozen=True)
class Expert:
    model: str            # catalog alias
    agent: str | None = None  # optional AgentDef for role/tools/system prompt


def _is_error(text: str) -> bool:
    return text.startswith(_SUBAGENT_ERROR)


def majority_vote(answers: list[str]) -> str:
    """Deterministic combiner: the most common answer by normalized text; ties
    resolve to the earliest occurrence. Errors are excluded unless all failed."""
    usable = [a for a in answers if not _is_error(a)] or answers
    counts = Counter(a.strip() for a in usable)
    winner_norm, _ = max(counts.items(), key=lambda kv: (kv[1], -list(counts).index(kv[0])))
    for a in usable:
        if a.strip() == winner_norm:
            return a
    return usable[0]


def _is_veto(critique: str) -> bool:
    """A critic vetoes unless it clearly approves. Conservative: anything that
    isn't an explicit APPROVE (and isn't an error) counts as a veto signal."""
    head = critique.strip().lower()
    if _is_error(critique):
        return True
    if head.startswith("approve"):
        return False
    return head.startswith("veto") or head.startswith("reject") or "veto" in head


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


async def _run(runner: "SubagentRunner", parent: Session, prompt: str, expert: Expert) -> str:
    return await runner.run(
        prompt=prompt, model=ModelId(expert.model), parent=parent, agent=expert.agent
    )


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
    if not experts:
        return f"{_SUBAGENT_ERROR} ensemble needs at least one expert"
    results = list(await asyncio.gather(*[_run(runner, parent, prompt, e) for e in experts]))
    if judge is None:
        return majority_vote(results)
    answers = "\n\n".join(f"[{i + 1}] {a}" for i, a in enumerate(results))
    return await _run(runner, parent, _SYNTH_PROMPT.format(task=prompt, answers=answers), judge)


async def panel(
    runner: "SubagentRunner",
    parent: Session,
    prompt: str,
    *,
    proposer: Expert,
    critics: list[Expert],
) -> str:
    """Proposer drafts; independent critics (ideally on different models) review
    concurrently. Accept iff no critic vetoes; otherwise return the critiques."""
    proposal = await _run(runner, parent, prompt, proposer)
    if not critics:
        return proposal
    review_prompt = _CRITIC_PROMPT.format(task=prompt, proposal=proposal)
    critiques = list(await asyncio.gather(*[_run(runner, parent, review_prompt, c) for c in critics]))
    vetoes = [c for c in critiques if _is_veto(c)]
    if not vetoes:
        return proposal
    joined = "\n\n".join(f"- {v}" for v in vetoes)
    return f"REJECTED by {len(vetoes)}/{len(critics)} critics.\n\nPROPOSAL:\n{proposal}\n\nVETOES:\n{joined}"


async def draft_refine(
    runner: "SubagentRunner",
    parent: Session,
    prompt: str,
    *,
    drafter: Expert,
    refiner: Expert,
) -> str:
    """Cheap/local expert drafts, then a strong expert refines (sequential)."""
    draft = await _run(runner, parent, prompt, drafter)
    return await _run(runner, parent, _REFINE_PROMPT.format(task=prompt, draft=draft), refiner)


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
    With no verify expert the gate is 'did the cheap call error?'."""
    answer = await _run(runner, parent, prompt, cheap)
    if verify is None:
        passed = not _is_error(answer)
    else:
        verdict = await _run(runner, parent, _VERIFY_PROMPT.format(task=prompt, answer=answer), verify)
        passed = verdict.strip().lower().startswith("pass")
    if passed:
        return answer
    return await _run(runner, parent, prompt, premium)


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
    if not experts:
        return f"{_SUBAGENT_ERROR} strategy {strategy!r} needs experts"
    if strategy == "ensemble":
        return await ensemble(runner, parent, prompt, experts)
    if strategy == "panel":
        return await panel(runner, parent, prompt, proposer=experts[0], critics=experts[1:])
    if strategy == "draft_refine":
        refiner = experts[1] if len(experts) > 1 else experts[0]
        return await draft_refine(runner, parent, prompt, drafter=experts[0], refiner=refiner)
    if strategy == "escalate":
        premium = experts[1] if len(experts) > 1 else experts[0]
        verify = experts[2] if len(experts) > 2 else None
        return await escalate(runner, parent, prompt, cheap=experts[0], premium=premium, verify=verify)
    return f"{_SUBAGENT_ERROR} unknown strategy {strategy!r}"


# --- model-driven native tools ---

def _experts(models: Any) -> list[Expert]:
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
                "judge (optional alias to synthesize; default = majority vote)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "models": {"type": "array", "items": {"type": "string"}},
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
                "Adversarial review: a proposer drafts, independent critics on other "
                "models approve or veto. Args: prompt (required), proposer (required "
                "alias), critics (required list of aliases)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "proposer": {"type": "string"},
                    "critics": {"type": "array", "items": {"type": "string"}},
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
                "Cost-aware: try a cheap model, escalate to a premium model only if a "
                "verify gate fails. Args: prompt (required), cheap (required alias), "
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
