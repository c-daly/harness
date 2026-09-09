"""Public process/HTTP/MCP/terminal faults; real model evidence is opt-in."""

import socket
import sys
from pathlib import Path

import pytest

from harness.catalog import Catalog
from harness.mcp_config import McpServerSpec
from harness.provider_litellm import CatalogProvider
from scripts.qualify_handoff_destination import MODES, expected_checks, journey


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("memory_enabled", [False, True])
async def test_destination_fault_and_explicit_recovery(tmp_path, mode, memory_enabled):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    memory = None
    if memory_enabled:
        server = tmp_path / "memory_fixture.py"
        server.write_text('''from mcp.server.fastmcp import FastMCP
mcp = FastMCP("memory")
@mcp.tool()
def memory_list(subject: str) -> str:
    return "- type: public fixture\\n  subject: " + subject
mcp.run()
''')
        memory = McpServerSpec(name="memory", transport="stdio", command=sys.executable,
            args=("-B", str(server)), tools_allow=("memory_list",), restart="never", tool_timeout_s=10)
    models = Catalog({"external": {"backend": "codex", "route": "codex/default"}, "local-small": {
        "route": "openai/local-small", "api_base": f"http://127.0.0.1:{port}/v1",
        "local": {"auto_start": True, "startup_seconds": 5, "command": [sys.executable,
            str(Path(__file__).parent / "fixtures/handoff_destination_http.py"),
            str(tmp_path / "project"), mode, str(port)]}}})
    result = await journey(tmp_path, CatalogProvider(models), mode, memory)
    assert result["passed"], {"failed": sorted(expected_checks(mode) - {k for k, v in result["checks"].items() if v}),
                              "error_type": result.get("error_type")}
