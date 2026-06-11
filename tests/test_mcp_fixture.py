"""Smoke-test the fixture server over in-memory streams (no subprocess)."""

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from tests.conftest import load_fixture_server


async def test_fixture_lists_and_calls():
    async with create_connected_server_and_client_session(load_fixture_server()) as session:
        page = await session.list_tools()
        names = {t.name for t in page.tools}
        assert {"add", "fail", "big", "env_probe", "die"} <= names
        result = await session.call_tool("add", {"a": 2, "b": 3})
        assert not result.isError
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert texts == ["5"]


async def test_fixture_tool_error_is_iserror_not_exception():
    async with create_connected_server_and_client_session(load_fixture_server()) as session:
        result = await session.call_tool("fail", {"message": "boom"})
        assert result.isError
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert any("boom" in t for t in texts)
