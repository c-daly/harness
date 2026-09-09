"""Real MCP/process/terminal recovery with scripted destination inference."""

import sys

import pytest

from harness.catalog import Catalog
from harness.mcp_config import McpServerSpec
from harness.provider import text_turn, tool_call_turn
from harness.provider_litellm import CatalogProvider
from harness.types import ToolName
from scripts.qualify_handoff_memory import journey


@pytest.mark.parametrize("memory_enabled", [False, True])
async def test_memory_failure_requires_new_reconciliation_and_recovers_in_same_task(tmp_path, memory_enabled):
    class ScriptedDestination(CatalogProvider):
        requests = 0

        async def infer(self, request):
            self.requests += 1
            chunks = text_turn("Stage B completed") if self.requests > 1 else tool_call_turn("", ToolName("write_file"),
                {"file_path": str(tmp_path / "project/B.txt"), "content": "stage B\n"})
            for chunk in chunks:
                yield chunk

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
    provider = ScriptedDestination(Catalog({"external": {"backend": "codex", "route": "codex/default"},
                                            "local-small": {"route": "openai/local-small"}}))
    result = await journey(tmp_path, provider, memory)
    assert result["passed"], {"failed": [key for key, value in result["checks"].items() if not value],
                              "error_type": result.get("error_type")}
    assert provider.requests == 2
    assert (tmp_path / "project/A.txt").read_text() == "stage A\n"
    assert (tmp_path / "project/B.txt").read_text() == "stage B\n"
