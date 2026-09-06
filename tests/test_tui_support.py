"""Pure TUI logic: history ring, slash parsing, TuiResolver."""

from harness.hooks import ProposedModelCall, ProposedToolCall
from harness.interaction import PermissionRequest
from harness.tui_support import (
    HistoryRing,
    SlashCommand,
    TuiResolver,
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
    assert ring.prev("one") == "one"  # clamped at oldest
    assert ring.next("one") == "two"
    assert ring.next("two") == ""  # past newest -> empty draft


def test_history_ring_skips_blank_and_duplicate_neighbors():
    ring = HistoryRing()
    ring.remember("a")
    ring.remember("")  # blanks not stored
    ring.remember("a")  # consecutive duplicate not stored
    ring.remember("b")
    assert ring.prev("") == "b"
    assert ring.prev("b") == "a"
    assert ring.prev("a") == "a"


def test_history_ring_remember_resets_walk():
    ring = HistoryRing()
    ring.remember("a")
    ring.remember("b")
    assert ring.prev("") == "b"
    ring.remember("c")  # new submission resets the walk
    assert ring.prev("") == "c"


def test_history_navigation_restores_unsent_draft():
    ring = HistoryRing()
    ring.remember("old prompt")
    assert ring.prev("work in progress") == "old prompt"
    assert ring.next("old prompt") == "work in progress"


def test_parse_slash_command():
    assert parse_slash_command("hello") is None
    assert parse_slash_command("/help") == SlashCommand(name="help", arg="")
    assert parse_slash_command("/model sonnet") == SlashCommand(name="model", arg="sonnet")
    assert parse_slash_command("/model  spaced  arg ") == SlashCommand(
        name="model", arg="spaced  arg"
    )
    assert parse_slash_command("/") is None  # bare slash is just text


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
    # grant_pattern now returns (tool, match) tuple (Task 7)
    assert grant_pattern(tool_req) == ("mcp__x__y", {})
    assert grant_pattern(model_req) == ("model:openai/gpt", {})


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
    # "dangerous" with empty args -> empty match (allow-all-tool); C1 forces persist=False
    # so a single keystroke never writes an unconstrained rule to grants.toml.
    assert grants == [("dangerous", False)]


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


async def test_always_on_empty_command_bash_grants_session_only_allow_all(tmp_path):
    # An empty bash command yields an empty match -> allow-all bash, but session-only:
    # never persisted to grants.toml from a single keystroke (C1).
    from harness.permissions import PermissionEngine

    grants_path = tmp_path / "grants.toml"
    engine = PermissionEngine([], grants_path=grants_path)

    async def ask(request):
        return "always"

    resolver = TuiResolver(ask=ask, engine=engine)
    req = PermissionRequest(
        call_id=CallId("c1"),
        action=ProposedToolCall(call_id=CallId("c1"), tool=ToolName("bash"), args={"command": ""}),
        reason="ask",
    )
    assert await resolver.resolve(req) is True
    # session grant exists and is unconstrained (allow-all bash)
    assert engine.decide("bash", {"command": "anything"}) == "allow"
    # but nothing was persisted
    assert not grants_path.exists()


async def test_always_on_flat_path_write_does_not_persist_unconstrained(tmp_path):
    # A flat (no-slash) file_path has no parent dir -> empty match. The unconstrained
    # allow-all write_file rule must NOT be persisted (C1).
    from harness.permissions import PermissionEngine

    grants_path = tmp_path / "grants.toml"
    engine = PermissionEngine([], grants_path=grants_path)

    async def ask(request):
        return "always"

    resolver = TuiResolver(ask=ask, engine=engine)
    req = PermissionRequest(
        call_id=CallId("c2"),
        action=ProposedToolCall(
            call_id=CallId("c2"), tool=ToolName("write_file"), args={"file_path": "notes.txt"}
        ),
        reason="ask",
    )
    assert await resolver.resolve(req) is True
    assert not grants_path.exists()
