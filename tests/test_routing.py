"""Layer B: context routing as a dispatch hook (explicit pins win)."""

from harness.dispatcher import Dispatcher
from harness.events import ModelCallCompleted
from harness.hooks import (
    Allow,
    HookBus,
    ProposedModelCall,
    ProposedToolCall,
    Rewrite,
)
from harness.interaction import HeadlessResolver
from harness.log import read_session
from harness.provider import FakeProvider, text_turn
from harness.routing import (
    RoutingContext,
    RoutingEngine,
    RoutingRule,
    RoutingRuleSet,
    load_routing,
)
from harness.session import Session
from harness.tools import ToolRegistry
from harness.types import CallId, ModelId, SessionId, ToolName


def _engine(ruleset, ctx):
    return RoutingEngine(ruleset, lambda: ctx)


def _model_call(model="base", pinned=False):
    return ProposedModelCall(call_id=CallId("c1"), model=ModelId(model), pinned=pinned)


# --- RoutingRule.matches ---

def test_rule_tag_match():
    rule = RoutingRule(target="local", tags=("tests",))
    assert rule.matches(RoutingContext(tags=("tests", "ci")))
    assert not rule.matches(RoutingContext(tags=("docs",)))


def test_rule_path_glob_match():
    rule = RoutingRule(target="local", path_globs=("*/tests/*",))
    assert rule.matches(RoutingContext(paths=("/repo/tests/test_x.py",)))
    assert not rule.matches(RoutingContext(paths=("/repo/src/x.py",)))


def test_rule_prompt_contains_is_case_insensitive():
    rule = RoutingRule(target="local", prompt_contains="REFACTOR")
    assert rule.matches(RoutingContext(prompt="please refactor this"))
    assert not rule.matches(RoutingContext(prompt="write docs"))


def test_rule_signals_are_anded():
    rule = RoutingRule(target="local", tags=("tests",), prompt_contains="flaky")
    assert rule.matches(RoutingContext(tags=("tests",), prompt="flaky test"))
    assert not rule.matches(RoutingContext(tags=("tests",), prompt="all good"))


# --- RoutingEngine.__call__ ---

async def test_tool_calls_pass_through():
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("tests",)))
    decision = await eng(ProposedToolCall(call_id=CallId("c1"), tool=ToolName("bash"), args={}))
    assert isinstance(decision, Allow)


async def test_pinned_model_is_never_rewritten():
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("tests",)))
    assert isinstance(await eng(_model_call(pinned=True)), Allow)


async def test_matching_rule_rewrites_to_target():
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("tests",)))
    decision = await eng(_model_call(model="gpt"))
    assert isinstance(decision, Rewrite)
    assert str(decision.action.model) == "local"
    assert decision.action.pinned is False


async def test_no_match_no_leeway_keeps_baseline():
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("docs",)))
    assert isinstance(await eng(_model_call(model="gpt")), Allow)


async def test_leeway_falls_back_to_router():
    rs = RoutingRuleSet(rules=[], leeway=True, router="cheap")
    eng = _engine(rs, RoutingContext(tags=("docs",)))
    decision = await eng(_model_call(model="gpt"))
    assert isinstance(decision, Rewrite)
    assert str(decision.action.model) == "cheap"


async def test_rewrite_to_same_model_is_a_noop_allow():
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("tests",)))
    assert isinstance(await eng(_model_call(model="local")), Allow)


# --- end-to-end through the dispatcher ---

async def test_routing_rewrites_effective_model_in_event_log(tmp_path):
    session = Session(tmp_path, SessionId("s1"))
    session.start()
    hooks = HookBus()
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("tests",)))
    hooks.register_dispatch(eng.name, eng, priority=eng.priority)
    dispatcher = Dispatcher(
        session=session, registry=ToolRegistry(), hooks=hooks, resolver=HeadlessResolver()
    )
    await dispatcher.dispatch_model(
        provider=FakeProvider([text_turn("hi")]),
        model=ModelId("gpt"),
        messages=[],
        tools=(),
    )
    completed = [e.event for e in read_session(tmp_path, SessionId("s1"), repair=True)
                 if isinstance(e.event, ModelCallCompleted)]
    assert [str(c.model) for c in completed] == ["local"]


async def test_pricing_for_stamps_effective_model(tmp_path):
    # routing rewrites gpt -> local; the stamped pricing must be local's, not gpt's
    session = Session(tmp_path, SessionId("s3"))
    session.start()
    hooks = HookBus()
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("tests",)))
    hooks.register_dispatch(eng.name, eng, priority=eng.priority)
    dispatcher = Dispatcher(
        session=session, registry=ToolRegistry(), hooks=hooks, resolver=HeadlessResolver()
    )
    prices = {"local": {"input_cost_per_token": 0.1, "output_cost_per_token": 0.2}, "gpt": {}}
    await dispatcher.dispatch_model(
        provider=FakeProvider([text_turn("hi")]),
        model=ModelId("gpt"),
        messages=[],
        tools=(),
        pricing_for=lambda m: prices.get(str(m), {}),
    )
    completed = [e.event for e in read_session(tmp_path, SessionId("s3"), repair=True)
                 if isinstance(e.event, ModelCallCompleted)]
    assert completed[0].pricing == {"input_cost_per_token": 0.1, "output_cost_per_token": 0.2}


async def test_pinned_model_survives_dispatch(tmp_path):
    session = Session(tmp_path, SessionId("s2"))
    session.start()
    hooks = HookBus()
    eng = _engine(RoutingRuleSet(rules=[RoutingRule(target="local", tags=("tests",))]),
                  RoutingContext(tags=("tests",)))
    hooks.register_dispatch(eng.name, eng, priority=eng.priority)
    dispatcher = Dispatcher(
        session=session, registry=ToolRegistry(), hooks=hooks, resolver=HeadlessResolver()
    )
    await dispatcher.dispatch_model(
        provider=FakeProvider([text_turn("hi")]),
        model=ModelId("gpt"),
        messages=[],
        tools=(),
        pinned=True,
    )
    completed = [e.event for e in read_session(tmp_path, SessionId("s2"), repair=True)
                 if isinstance(e.event, ModelCallCompleted)]
    assert [str(c.model) for c in completed] == ["gpt"]


# --- load_routing layering ---

def test_load_routing_layers_project_over_user(tmp_path):
    user = tmp_path / "user"
    proj = tmp_path / "proj"
    (user).mkdir()
    (proj / ".harness").mkdir(parents=True)
    (user / "routing.toml").write_text(
        'default = "user-default"\nrouter = "user-router"\n'
        '[[rules]]\ntarget = "u"\ntags = ["docs"]\n'
    )
    (proj / ".harness" / "routing.toml").write_text(
        'default = "proj-default"\nleeway = true\n'
        '[[rules]]\ntarget = "p"\ntags = ["tests"]\n'
    )
    rs = load_routing(project_dir=proj, config_home=user)
    assert rs is not None
    assert rs.default == "proj-default"   # project shadows user
    assert rs.router == "user-router"     # user fills what project omits
    assert rs.leeway is True
    assert {r.target for r in rs.rules} == {"p", "u"}


def test_load_routing_absent_returns_none(tmp_path):
    assert load_routing(project_dir=tmp_path, config_home=tmp_path) is None


# --- build_kernel wiring: the signals closure reads the live turn prompt ---

async def test_build_kernel_routes_unpinned_turn_by_prompt(tmp_path):
    from harness.cli import build_kernel

    rules = RoutingRuleSet(rules=[RoutingRule(target="local", prompt_contains="route-me")])
    kernel = build_kernel(
        provider=FakeProvider([text_turn("done")]),
        base_dir=tmp_path,
        model=ModelId("baseline"),
        routing_rules=rules,
        model_pinned=False,
    )
    await kernel.loop.start()
    await kernel.loop.run_turn("please route-me now")
    await kernel.loop.end()
    kernel.session.close()
    completed = [e.event for e in read_session(tmp_path, kernel.session.id, repair=True)
                if isinstance(e.event, ModelCallCompleted)]
    assert [str(c.model) for c in completed] == ["local"]


async def test_build_kernel_does_not_route_pinned_top_level(tmp_path):
    from harness.cli import build_kernel

    rules = RoutingRuleSet(rules=[RoutingRule(target="local", prompt_contains="route-me")])
    kernel = build_kernel(
        provider=FakeProvider([text_turn("done")]),
        base_dir=tmp_path,
        model=ModelId("baseline"),
        routing_rules=rules,
        model_pinned=True,  # explicit --model
    )
    await kernel.loop.start()
    await kernel.loop.run_turn("please route-me now")
    await kernel.loop.end()
    kernel.session.close()
    completed = [e.event for e in read_session(tmp_path, kernel.session.id, repair=True)
                if isinstance(e.event, ModelCallCompleted)]
    assert [str(c.model) for c in completed] == ["baseline"]
