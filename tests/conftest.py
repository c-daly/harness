"""Shared test helpers. The MCP fixture server is loaded by file path so tests
do not depend on tests/ being an importable package."""

import importlib.util
from pathlib import Path

FIXTURE_SERVER_PATH = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"


def load_fixture_server():
    """Import the fixture module fresh and return its FastMCP instance."""
    spec = importlib.util.spec_from_file_location("mcp_fixture_server", FIXTURE_SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fixture
