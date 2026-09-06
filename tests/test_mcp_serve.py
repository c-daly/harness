"""McpToolServer: harness ToolSpecs served over streamable-HTTP MCP; every
call routes through the injected dispatch callable (the dispatcher seam)."""

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import httpx
from urllib.parse import urlsplit

from harness.dispatcher import ToolOutcome
from harness.blobs import BlobStore
from harness.mcp_serve import McpToolServer
from harness.tools import ToolSpec
from harness.types import ToolName

ECHO = ToolSpec(
    name=ToolName("echo"),
    description="Echo the text back.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
)


def _dispatch_recorder(outcome: ToolOutcome):
    calls: list = []

    async def dispatch(call):
        calls.append(call)
        return outcome

    return dispatch, calls


async def test_lists_given_specs_only():
    dispatch, _ = _dispatch_recorder(ToolOutcome(text="", blob=None, is_error=False))
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        assert names == ["echo"]
        assert tools.tools[0].description == "Echo the text back."
        assert tools.tools[0].inputSchema["properties"]["text"]["type"] == "string"
    finally:
        await server.stop()


async def test_call_routes_through_dispatch_and_returns_text():
    dispatch, calls = _dispatch_recorder(ToolOutcome(text="echoed: hi", blob=None, is_error=False))
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("echo", {"text": "hi"})
        assert result.isError is False
        assert result.content[0].text == "echoed: hi"
        assert len(calls) == 1
        assert str(calls[0].tool) == "echo"
        assert dict(calls[0].args) == {"text": "hi"}
        assert calls[0].call_id  # dispatcher requires a call id
    finally:
        await server.stop()


async def test_denied_outcome_surfaces_as_mcp_error():
    dispatch, _ = _dispatch_recorder(
        ToolOutcome(text="denied by permission engine", blob=None, is_error=True)
    )
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("echo", {"text": "hi"})
        assert result.isError is True
        assert "denied by permission engine" in result.content[0].text
    finally:
        await server.stop()


async def test_unknown_tool_is_error_without_dispatch():
    dispatch, calls = _dispatch_recorder(ToolOutcome(text="", blob=None, is_error=False))
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("not_a_tool", {})
        assert result.isError is True
        assert calls == []  # never reached the dispatcher
    finally:
        await server.stop()


async def test_large_blob_result_reaches_mcp_client(tmp_path):
    payload = "large result λ\n" * 2000
    blobs = BlobStore(tmp_path)
    ref = blobs.put(payload.encode())
    dispatch, _ = _dispatch_recorder(
        ToolOutcome(text=None, blob=ref, is_error=False, _blobs=blobs)
    )
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("echo", {"text": "hi"})
        assert result.isError is False
        assert result.content[0].text == payload
    finally:
        await server.stop()


async def test_http_requests_require_the_per_server_capability():
    dispatch, calls = _dispatch_recorder(ToolOutcome(text="secret", blob=None, is_error=False))
    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()
    try:
        address = urlsplit(server.url)
        base = f"{address.scheme}://{address.netloc}"
        async with httpx.AsyncClient() as client:
            response = await client.post(base + "/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
            assert response.status_code == 401
            response = await client.post(server.url, headers={"Origin": "https://untrusted.example"}, json={})
            assert response.status_code == 403
        assert calls == []
    finally:
        await server.stop()


async def test_shutdown_settles_an_active_tool_before_returning():
    entered, cleaned, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def dispatch(call):
        try:
            entered.set()
            await release.wait()
            return ToolOutcome(text="done", blob=None, is_error=False)
        finally:
            cleaned.set()

    server = McpToolServer(specs=(ECHO,), dispatch=dispatch)
    await server.start()

    async def request():
        async with streamablehttp_client(server.url) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.call_tool("echo", {"text": "wait"})

    client_task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(server.stop(), 1)
        assert cleaned.is_set()
    finally:
        release.set()
        client_task.cancel()
        await asyncio.gather(client_task, return_exceptions=True)
        await server.stop()
