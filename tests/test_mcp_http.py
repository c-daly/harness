"""Live streamable-HTTP e2e: fixture server in a subprocess on a free port."""

import socket
import subprocess
import sys
import time

import pytest
from mcp import types

from harness.mcp_config import McpServerSpec
from harness.mcp_host import McpServerError, ServerConnection
from tests.conftest import FIXTURE_SERVER_PATH


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"fixture server exited early: rc={proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("fixture server did not open its port in time")


@pytest.fixture
def http_fixture_url():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FIXTURE_SERVER_PATH), "--http", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port, proc)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_http_transport_roundtrip(http_fixture_url):
    spec = McpServerSpec(name="fixture", transport="http", url=http_fixture_url)
    conn = ServerConnection(spec)
    await conn.start()
    try:
        assert conn.instructions == "Fixture server: use `add` for arithmetic."
        result = await conn.call_tool("add", {"a": 5, "b": 7})
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert texts == ["12"]
    finally:
        await conn.stop()


async def test_http_connect_failure_is_loud_and_fast():
    spec = McpServerSpec(
        name="ghost", transport="http", url="http://127.0.0.1:9/mcp", restart="never"
    )
    conn = ServerConnection(spec)
    start = time.monotonic()
    with pytest.raises(McpServerError) as exc:
        await conn.start()
    assert "ghost" in str(exc.value)
    assert time.monotonic() - start < 35.0  # bounded by the start timeout, not hanging


async def test_http_transport_with_header_references(http_fixture_url, monkeypatch):
    monkeypatch.setenv("FIXTURE_HTTP_AUTH", "Bearer test-token")
    spec = McpServerSpec(
        name="fixture", transport="http", url=http_fixture_url,
        headers={"Authorization": "FIXTURE_HTTP_AUTH"},
    )
    conn = ServerConnection(spec)
    await conn.start()
    try:
        result = await conn.call_tool("add", {"a": 2, "b": 3})
        texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
        assert texts == ["5"]
    finally:
        await conn.stop()
