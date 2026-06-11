from harness.hooks import Allow, Ask, Block, ProposedModelCall, ProposedToolCall
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.types import CallId, ModelId, ToolName


def test_rule_matches_tool_glob():
    rule = PermissionRule(action="deny", tool="bash")
    assert rule.matches("bash", {})
    assert not rule.matches("read_file", {})
    assert PermissionRule(action="allow", tool="read_*").matches("read_file", {})


def test_rule_matches_args_glob():
    rule = PermissionRule(action="ask", tool="bash", match={"command": "git push*"})
    assert rule.matches("bash", {"command": "git push origin"})
    assert not rule.matches("bash", {"command": "git status"})


def test_missing_arg_matches_only_star():
    assert PermissionRule(action="allow", tool="bash", match={"command": "*"}).matches("bash", {})
    assert not PermissionRule(action="allow", tool="bash", match={"command": "git*"}).matches("bash", {})


def test_first_match_wins_within_layer():
    rs = RuleSet(rules=[
        PermissionRule(action="deny", tool="bash"),
        PermissionRule(action="allow", tool="*"),
    ])
    engine = PermissionEngine([rs])
    assert engine.decide("bash", {}) == "deny"
    assert engine.decide("echo", {}) == "allow"


def test_deny_is_absolute_even_over_session_grant():
    project = RuleSet(rules=[PermissionRule(action="deny", tool="bash")])
    engine = PermissionEngine([project])
    engine.grant("bash")
    assert engine.decide("bash", {}) == "deny"


def test_grant_overrides_ask():
    project = RuleSet(rules=[PermissionRule(action="ask", tool="bash")])
    engine = PermissionEngine([project])
    assert engine.decide("bash", {}) == "ask"
    engine.grant("bash")
    assert engine.decide("bash", {}) == "allow"


def test_default_from_first_layer_that_declares_one():
    engine = PermissionEngine([RuleSet(), RuleSet(default="deny"), RuleSet(default="allow")])
    assert engine.decide("anything", {}) == "deny"


def test_fallback_default_is_ask():
    assert PermissionEngine([]).decide("anything", {}) == "ask"


async def test_call_maps_verdicts_to_decisions():
    rs = RuleSet(rules=[
        PermissionRule(action="allow", tool="echo"),
        PermissionRule(action="deny", tool="bash"),
        PermissionRule(action="ask", tool="deploy"),
    ])
    engine = PermissionEngine([rs])
    allow = await engine(ProposedToolCall(call_id=CallId("c"), tool=ToolName("echo"), args={}))
    deny = await engine(ProposedToolCall(call_id=CallId("c"), tool=ToolName("bash"), args={}))
    ask = await engine(ProposedToolCall(call_id=CallId("c"), tool=ToolName("deploy"), args={}))
    assert isinstance(allow, Allow)
    assert isinstance(deny, Block) and "deny" in deny.reason
    assert isinstance(ask, Ask) and "deploy" in ask.reason


async def test_model_calls_addressable_as_pseudo_tool():
    rs = RuleSet(rules=[PermissionRule(action="deny", tool="model:expensive/*")],
                 default="allow")
    engine = PermissionEngine([rs])
    blocked = await engine(ProposedModelCall(call_id=CallId("c"), model=ModelId("expensive/gpt-x")))
    fine = await engine(ProposedModelCall(call_id=CallId("c"), model=ModelId("cheap/mini")))
    assert isinstance(blocked, Block)
    assert isinstance(fine, Allow)


def test_invalid_action_rejected():
    import pytest
    with pytest.raises(ValueError, match="action"):
        PermissionRule(action="maybe", tool="*")


def test_grant_persistence_survives_hostile_strings(tmp_path):
    grants = tmp_path / "grants.toml"
    engine = PermissionEngine([], grants_path=grants)
    hostile = 'we"ird\ntool'
    engine.grant(hostile, {"arg": 'va"lue'}, persist=True)
    reloaded = RuleSet.load(grants)  # must parse, not TOMLDecodeError
    assert reloaded.rules[0].tool == hostile
    assert reloaded.rules[0].match == {"arg": 'va"lue'}


def test_ruleset_rejects_invalid_default():
    import pytest
    with pytest.raises(ValueError, match="default"):
        RuleSet(default="maybe")
