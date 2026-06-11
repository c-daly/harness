"""Pure TUI logic: history ring, slash parsing, @file expansion, TuiResolver."""

from harness.hooks import ProposedModelCall, ProposedToolCall
from harness.interaction import PermissionRequest
from harness.tui_support import (
    HistoryRing,
    SlashCommand,
    TuiResolver,
    expand_file_mentions,
    grant_pattern,
    parse_slash_command,
)
from harness.types import CallId, ModelId, ToolName


def test_history_ring_up_down_walk():
    ring = HistoryRing()
    ring.remember("one")
    ring.remember("two")
    assert ring.prev("") == "two"
    assert ring.prev("two") == "one"
    assert ring.prev("one") == "one"      # clamped at oldest
    assert ring.next("one") == "two"
    assert ring.next("two") == ""         # past newest -> empty draft


def test_history_ring_skips_blank_and_duplicate_neighbors():
    ring = HistoryRing()
    ring.remember("a")
    ring.remember("")        # blanks not stored
    ring.remember("a")       # consecutive duplicate not stored
    ring.remember("b")
    assert ring.prev("") == "b"
    assert ring.prev("b") == "a"
    assert ring.prev("a") == "a"


def test_history_ring_remember_resets_walk():
    ring = HistoryRing()
    ring.remember("a")
    ring.remember("b")
    assert ring.prev("") == "b"
    ring.remember("c")               # new submission resets the walk
    assert ring.prev("") == "c"


def test_parse_slash_command():
    assert parse_slash_command("hello") is None
    assert parse_slash_command("/help") == SlashCommand(name="help", arg="")
    assert parse_slash_command("/model sonnet") == SlashCommand(name="model", arg="sonnet")
    assert parse_slash_command("/model  spaced  arg ") == SlashCommand(
        name="model", arg="spaced  arg"
    )
    assert parse_slash_command("/") is None  # bare slash is just text


def test_expand_file_mentions_inlines_files(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("alpha\nbeta\n")
    text, attached, errors = expand_file_mentions(f"summarize @{f}", max_bytes=1024)
    assert errors == []
    assert attached == [str(f)]
    assert "alpha\nbeta" in text
    assert text.startswith("summarize")          # prompt kept; blocks appended
    assert "```" in text                          # fenced


def test_expand_file_mentions_missing_and_oversize(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2048)
    missing = tmp_path / "nope.txt"
    text, attached, errors = expand_file_mentions(
        f"@{missing} and @{big}", max_bytes=1024
    )
    assert attached == []
    assert len(errors) == 2
    assert any("nope.txt" in e for e in errors)
    assert any("big.bin" in e and "1024" in e for e in errors)


def test_expand_file_mentions_no_mentions_passthrough():
    text, attached, errors = expand_file_mentions("plain prompt", max_bytes=1024)
    assert (text, attached, errors) == ("plain prompt", [], [])


def test_grant_pattern_for_tool_and_model():
    tool_req = PermissionRequest(
        call_id=CallId("c1"),
        action=ProposedToolCall(call_id=CallId("c1"), tool=ToolName("mcp__x__y"), args={}),
        reason="ask",
    )
    model_req = PermissionRequest(
        call_id=CallId("c2"),
        action=ProposedModelCall(call_id=CallId("c2"), model=ModelId("openai/gpt")),
        reason="ask",
    )
    assert grant_pattern(tool_req) == "mcp__x__y"
    assert grant_pattern(model_req) == "model:openai/gpt"


async def test_tui_resolver_allow_deny_always():
    answers = iter(["allow", "deny", "always"])

    async def ask(request):
        return next(answers)

    grants: list = []

    class FakeEngine:
        def grant(self, tool, match=None, *, persist=False):
            grants.append((tool, persist))

    resolver = TuiResolver(ask=ask, engine=FakeEngine())
    req = PermissionRequest(
        call_id=CallId("c1"),
        action=ProposedToolCall(call_id=CallId("c1"), tool=ToolName("dangerous"), args={}),
        reason="ask",
    )
    assert await resolver.resolve(req) is True
    assert await resolver.resolve(req) is False
    assert await resolver.resolve(req) is True
    assert grants == [("dangerous", True)]


async def test_tui_resolver_always_without_engine_is_plain_allow():
    async def ask(request):
        return "always"

    resolver = TuiResolver(ask=ask, engine=None)
    req = PermissionRequest(
        call_id=CallId("c1"),
        action=ProposedToolCall(call_id=CallId("c1"), tool=ToolName("t"), args={}),
        reason="ask",
    )
    assert await resolver.resolve(req) is True
