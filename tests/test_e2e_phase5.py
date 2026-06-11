# tests/test_e2e_phase5.py
"""Phase 5 milestone: MCP servers configured, hosted, enforced, observed -- over a real stdio subprocess."""

import sys

from harness.blobs import INLINE_THRESHOLD
from harness.cli import build_kernel, run_once
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import FakeProvider, text_turn, tool_call_turn
from harness.telemetry import rebuild_index, render_stats, stats_summary
from harness.types import ModelId, ToolName
from tests.conftest import FIXTURE_SERVER_PATH


def fixture_stdio_spec(**overrides) -> McpServerSpec:
    """Build a McpServerSpec pointing at the stdio fixture server subprocess."""
    defaults = dict(
        name="fixture", transport="stdio",
        command=sys.executable, args=(str(FIXTURE_SERVER_PATH),),
    )
    defaults.update(overrides)
    return McpServerSpec(**defaults)


def read_envelopes(base_dir, session_id):
    """Read all log envelopes for a session."""
    return read_session(base_dir, session_id)


# ---------------------------------------------------------------------------
# Test 1: Full kernel run over real stdio MCP server
# ---------------------------------------------------------------------------

async def test_full_kernel_run_over_real_mcp_server(tmp_path):
    """build_kernel with a real stdio MCP subprocess: add tool called, instructions injected,
    custom server_started/server_stopped events logged, ToolCallCompleted result_text=42."""
    provider = FakeProvider([
        tool_call_turn("calling add", ToolName("mcp__fixture__add"), {"a": 19, "b": 23}),
        text_turn("done"),
    ])
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
    )
    reply = await run_once(kernel, "add the numbers")
    assert reply == "done"

    # Instructions from server injected into system prompt
    assert "Fixture server: use `add` for arithmetic." in kernel.loop.system_prompt

    envelopes = read_envelopes(tmp_path, kernel.session.id)

    # First envelope must be session_started
    assert envelopes[0].event.type == "session_started"

    # MCP server lifecycle events in the log
    customs = [e.event for e in envelopes if getattr(e.event, "type", "") == "custom"]
    assert any(c.namespace == "mcp" and c.name == "server_started" for c in customs)
    assert any(c.namespace == "mcp" and c.name == "server_stopped" for c in customs)

    # Tool call completed with result 42
    completed = [e.event for e in envelopes if getattr(e.event, "type", "") == "tool_call_completed"]
    assert any(e.result_text == "42" for e in completed)

# ---------------------------------------------------------------------------
# Test 2: Permission glob over MCP tools
# ---------------------------------------------------------------------------

async def test_permission_deny_blocks_mcp_tool(tmp_path):
    """A deny rule for mcp__fixture__die blocks the call (is_error + blocked by policy),
    while a subsequent mcp__fixture__add call succeeds, proving the server is still alive."""
    engine = PermissionEngine([
        RuleSet(
            rules=[PermissionRule(action="deny", tool="mcp__fixture__die")],
            default="allow",
        )
    ])
    provider = FakeProvider([
        tool_call_turn("dying", ToolName("mcp__fixture__die"), {}),
        tool_call_turn("adding", ToolName("mcp__fixture__add"), {"a": 1, "b": 1}),
        text_turn("survived"),
    ])
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
        permissions=engine,
    )
    reply = await run_once(kernel, "try to die then add")
    assert reply == "survived"

    envelopes = read_envelopes(tmp_path, kernel.session.id)
    completed = [e.event for e in envelopes if getattr(e.event, "type", "") == "tool_call_completed"]

    # Find results by tool from the ToolCallProposed events
    proposed = [e.event for e in envelopes if getattr(e.event, "type", "") == "tool_call_proposed"]
    die_call_ids = {p.call_id for p in proposed if str(p.tool) == "mcp__fixture__die"}
    add_call_ids = {p.call_id for p in proposed if str(p.tool) == "mcp__fixture__add"}

    die_results = [c for c in completed if c.call_id in die_call_ids]
    add_results = [c for c in completed if c.call_id in add_call_ids]

    # die must be blocked with policy error
    assert die_results, "Expected a ToolCallCompleted for the die call"
    die_result = die_results[0]
    assert die_result.is_error, "die call should have is_error=True"
    assert "blocked by policy" in die_result.result_text

    # add must succeed with result 2
    assert add_results, "Expected a ToolCallCompleted for the add call"
    add_result = add_results[0]
    assert not add_result.is_error
    assert add_result.result_text == "2"

# ---------------------------------------------------------------------------
# Test 3: Telemetry end-to-end
# ---------------------------------------------------------------------------

async def test_telemetry_mcp_origin_and_stats(tmp_path):
    """After a run calling mcp__fixture__add, rebuild_index shows origin=fixture;
    render_stats contains mcp servers and fixture."""
    provider = FakeProvider([
        tool_call_turn("calling add", ToolName("mcp__fixture__add"), {"a": 5, "b": 7}),
        text_turn("twelve"),
    ])
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
    )
    assert await run_once(kernel, "add") == "twelve"

    conn, warnings = rebuild_index(tmp_path)
    assert warnings == []

    # tool_calls row for the mcp call has origin fixture
    rows = conn.execute(
        "SELECT tool, origin FROM tool_calls WHERE tool LIKE 'mcp__%'",
    ).fetchall()
    assert rows, "Expected at least one mcp__ tool_call row in telemetry"
    origins = {r[1] for r in rows}
    assert "fixture" in origins

    # render_stats shows mcp servers section with fixture
    summary = stats_summary(conn)
    rendered = render_stats(summary)
    assert "mcp servers" in rendered
    assert "fixture" in rendered

# ---------------------------------------------------------------------------
# Test 4: Blob spill through MCP
# ---------------------------------------------------------------------------

async def test_blob_spill_through_mcp(tmp_path):
    """Calling mcp__fixture__big with n > INLINE_THRESHOLD causes result_blob to be set
    and result_text to be None on the ToolCallCompleted event."""
    n = INLINE_THRESHOLD * 2  # comfortably above the threshold
    provider = FakeProvider([
        tool_call_turn("big call", ToolName("mcp__fixture__big"), {"n": n}),
        text_turn("done"),
    ])
    kernel = build_kernel(
        provider=provider,
        base_dir=tmp_path,
        model=ModelId("fake:echo"),
        mcp=[fixture_stdio_spec()],
    )
    assert await run_once(kernel, "get big output") == "done"

    envelopes = read_envelopes(tmp_path, kernel.session.id)
    completed = [e.event for e in envelopes if getattr(e.event, "type", "") == "tool_call_completed"]

    # The big tool call should have spilled to blob
    big_results = [c for c in completed if c.result_blob is not None]
    assert big_results, "Expected at least one ToolCallCompleted with result_blob set"
    blob_result = big_results[0]
    assert blob_result.result_blob is not None
    assert blob_result.result_text is None
