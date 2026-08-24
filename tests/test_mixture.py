"""Layer C: Mixture-of-Models strategies, combiners, native tools, config-driven."""

from dataclasses import dataclass, field

from harness.frontmatter import AgentDef
from harness.hooks import HookBus
from harness.interaction import HeadlessResolver
from harness.mixture import (
    ConsultPanelTool,
    EnsembleTool,
    EscalateTool,
    Expert,
    _experts,
    _is_veto,
    draft_refine,
    ensemble,
    escalate,
    majority_vote,
    panel,
    register_mixture_tools,
    run_strategy,
)
from harness.provider import FakeProvider, text_turn
from harness.session import Session
from harness.subagent import SubagentRunner
from harness.tools import ToolName, ToolRegistry
from harness.types import ModelId, SessionId


@dataclass
class FakeRunner:
    """Returns a scripted response per model alias; records (model, prompt)."""

    responses: dict  # alias -> str | callable(prompt)->str
    calls: list = field(default_factory=list)

    async def run(self, *, prompt, model, parent, agent=None):
        self.calls.append((str(model), prompt))
        r = self.responses[str(model)]
        return r(prompt) if callable(r) else r


# --- combiners ---

def test_majority_vote_picks_most_common():
    assert majority_vote(["A", "A", "B"]) == "A"


def test_majority_vote_excludes_errors_unless_all_failed():
    assert majority_vote(["[subagent error] x", "ok", "ok"]) == "ok"
    assert majority_vote(["[subagent error] x"]).startswith("[subagent error]")


def test_majority_vote_tie_breaks_to_earliest_occurrence():
    # A and B are tied 2-2; A is seen first, so A wins the tie-break.
    assert majority_vote(["A", "B", "A", "B"]) == "A"


# --- ensemble ---

async def test_ensemble_votes_without_judge():
    runner = FakeRunner({"a": "yes", "b": "yes", "c": "no"})
    out = await ensemble(runner, None, "Q", [Expert("a"), Expert("b"), Expert("c")])
    assert out == "yes"
    assert {m for m, _ in runner.calls} == {"a", "b", "c"}


async def test_ensemble_uses_judge_to_synthesize():
    runner = FakeRunner({"a": "x", "b": "y", "j": lambda p: f"SYNTH::{('x' in p and 'y' in p)}"})
    out = await ensemble(runner, None, "Q", [Expert("a"), Expert("b")], judge=Expert("j"))
    assert out == "SYNTH::True"  # judge prompt carried both candidate answers


async def test_ensemble_judge_path_filters_errors_from_candidates():
    runner = FakeRunner({
        "a": "[subagent error] boom",
        "b": "y",
        "j": lambda p: f"SYNTH::{('boom' not in p and 'y' in p)}",
    })
    out = await ensemble(runner, None, "Q", [Expert("a"), Expert("b")], judge=Expert("j"))
    assert out == "SYNTH::True"  # the errored answer never reached the judge's prompt


async def test_ensemble_judge_not_called_when_all_experts_error():
    runner = FakeRunner({"a": "[subagent error] x", "b": "[subagent error] y", "j": "SYNTH"})
    out = await ensemble(runner, None, "Q", [Expert("a"), Expert("b")], judge=Expert("j"))
    assert out.startswith("[subagent error]")
    assert "j" not in {m for m, _ in runner.calls}  # judge never invoked


# --- _is_veto semantics (fail-closed; APPROVE-first-line protocol) ---

def test_is_veto_neutral_text_vetoes():
    assert _is_veto("looks reasonable to me")


def test_is_veto_no_substring_scan_false_positive():
    # "veto" appears as a substring, but the critique doesn't start with
    # APPROVE, so it still vetoes -- just not because of a substring scan.
    assert _is_veto("I would not veto this proposal")


def test_is_veto_approve_first_line_does_not_veto():
    assert not _is_veto("APPROVE -- ship it")


def test_is_veto_error_vetoes_fail_closed():
    assert _is_veto("[subagent error] boom")


# --- panel ---

async def test_panel_accepts_when_no_veto():
    runner = FakeRunner({"p": "proposal", "c1": "APPROVE", "c2": "approve, looks right"})
    out = await panel(runner, None, "Q", proposer=Expert("p"), critics=[Expert("c1"), Expert("c2")])
    assert out == "proposal"


async def test_panel_rejects_on_veto():
    runner = FakeRunner({"p": "wrong proposal", "c1": "APPROVE", "c2": "VETO: it is wrong"})
    out = await panel(runner, None, "Q", proposer=Expert("p"), critics=[Expert("c1"), Expert("c2")])
    assert out.startswith("REJECTED by 1/2")
    assert "VETO: it is wrong" in out


async def test_panel_proposer_error_short_circuits_critics():
    runner = FakeRunner({"p": "[subagent error] boom", "c1": "APPROVE"})
    out = await panel(runner, None, "Q", proposer=Expert("p"), critics=[Expert("c1")])
    assert out == "[subagent error] boom"
    assert [m for m, _ in runner.calls] == ["p"]  # critics never fanned out


# --- draft_refine ---

async def test_draft_refine_feeds_draft_to_refiner():
    runner = FakeRunner({"d": "rough draft", "r": lambda p: f"refined({'rough draft' in p})"})
    out = await draft_refine(runner, None, "Q", drafter=Expert("d"), refiner=Expert("r"))
    assert out == "refined(True)"
    assert [m for m, _ in runner.calls] == ["d", "r"]


async def test_draft_refine_draft_error_short_circuits_refiner():
    runner = FakeRunner({"d": "[subagent error] boom", "r": "refined"})
    out = await draft_refine(runner, None, "Q", drafter=Expert("d"), refiner=Expert("r"))
    assert out == "[subagent error] boom"
    assert [m for m, _ in runner.calls] == ["d"]  # refiner never invoked


# --- escalate ---

async def test_escalate_keeps_cheap_when_it_succeeds():
    runner = FakeRunner({"cheap": "good answer", "premium": "expensive"})
    out = await escalate(runner, None, "Q", cheap=Expert("cheap"), premium=Expert("premium"))
    assert out == "good answer"
    assert [m for m, _ in runner.calls] == ["cheap"]  # premium never consulted


async def test_escalate_promotes_on_cheap_error():
    runner = FakeRunner({"cheap": "[subagent error] boom", "premium": "fixed"})
    out = await escalate(runner, None, "Q", cheap=Expert("cheap"), premium=Expert("premium"))
    assert out == "fixed"
    assert [m for m, _ in runner.calls] == ["cheap", "premium"]


async def test_escalate_verify_gate_fail_then_premium():
    runner = FakeRunner({"cheap": "meh", "v": "FAIL: incomplete", "premium": "better"})
    out = await escalate(
        runner, None, "Q", cheap=Expert("cheap"), premium=Expert("premium"), verify=Expert("v")
    )
    assert out == "better"
    assert [m for m, _ in runner.calls] == ["cheap", "v", "premium"]


async def test_escalate_verify_gate_pass_keeps_cheap():
    runner = FakeRunner({"cheap": "meh", "v": "PASS", "premium": "better"})
    out = await escalate(
        runner, None, "Q", cheap=Expert("cheap"), premium=Expert("premium"), verify=Expert("v")
    )
    assert out == "meh"
    assert [m for m, _ in runner.calls] == ["cheap", "v"]


async def test_escalate_cheap_error_skips_verify_gate():
    runner = FakeRunner({"cheap": "[subagent error] boom", "v": "PASS", "premium": "fixed"})
    out = await escalate(
        runner, None, "Q", cheap=Expert("cheap"), premium=Expert("premium"), verify=Expert("v")
    )
    assert out == "fixed"
    assert [m for m, _ in runner.calls] == ["cheap", "premium"]  # verify never consulted


# --- run_strategy positional convention ---

async def test_run_strategy_maps_names():
    runner = FakeRunner({"a": "one", "b": "two"})
    assert await run_strategy("ensemble", runner, None, "Q", [Expert("a")]) == "one"
    assert (await run_strategy("nope", runner, None, "Q", [Expert("a")])).startswith(
        "[subagent error]"
    )


# --- native tool arg parsing / delegation ---

async def test_ensemble_tool_delegates():
    runner = FakeRunner({"a": "same", "b": "same"})
    tool = EnsembleTool(runner=runner, parent=None)
    assert tool.spec.name == ToolName("ensemble")
    out = await tool({"prompt": "Q", "models": ["a", "b"]})
    assert out == "same"


def test_experts_scalar_string_is_one_expert_not_chars():
    # a bare string from an LLM tool call must not iterate into characters
    assert _experts("gpt-4") == [Expert(model="gpt-4")]


async def test_ensemble_tool_scalar_models_string_is_one_expert():
    runner = FakeRunner({"gpt-4": "ok"})
    tool = EnsembleTool(runner=runner, parent=None)
    out = await tool({"prompt": "Q", "models": "gpt-4"})
    assert out == "ok"
    assert [m for m, _ in runner.calls] == ["gpt-4"]  # one call, not five


async def test_escalate_tool_delegates():
    runner = FakeRunner({"cheap": "[subagent error] x", "premium": "ok"})
    tool = EscalateTool(runner=runner, parent=None)
    out = await tool({"prompt": "Q", "cheap": "cheap", "premium": "premium"})
    assert out == "ok"


async def test_consult_panel_tool_delegates():
    runner = FakeRunner({"p": "draft", "c": "APPROVE"})
    tool = ConsultPanelTool(runner=runner, parent=None)
    out = await tool({"prompt": "Q", "proposer": "p", "critics": ["c"]})
    assert out == "draft"


# --- registration + config-driven dispatch through a real SubagentRunner ---

def test_register_mixture_tools_adds_three(tmp_path):
    registry = ToolRegistry()
    runner = SubagentRunner(
        base=tmp_path, provider=FakeProvider([]), registry=ToolRegistry(),
        hooks=HookBus(), resolver=HeadlessResolver(), default_model=ModelId("fake"),
    )
    register_mixture_tools(registry, runner=runner, parent=Session(tmp_path, SessionId("p")))
    names = {str(s.name) for s in registry.specs()}
    assert {"ensemble", "consult_panel", "escalate"} <= names


async def test_coordination_agentdef_fans_out_via_real_runner(tmp_path):
    parent = Session(tmp_path, SessionId("parent"))
    parent.start()
    coord = AgentDef(
        name="panel", description="coordination", strategy="ensemble", experts=("m",)
    )
    runner = SubagentRunner(
        base=tmp_path,
        provider=FakeProvider([text_turn("the answer")]),  # single expert -> one child call
        registry=ToolRegistry(),
        hooks=HookBus(),
        resolver=HeadlessResolver(),
        default_model=ModelId("fake"),
        agents={"panel": coord},
    )
    out = await runner.run(prompt="solve", model=None, parent=parent, agent="panel")
    parent.close()
    assert out == "the answer"  # majority vote of a single expert answer
