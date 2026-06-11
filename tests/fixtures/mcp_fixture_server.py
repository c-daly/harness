"""Stdio/HTTP MCP fixture server for tests.

Run: python tests/fixtures/mcp_fixture_server.py [--http PORT]
Also imported as a module for in-memory client sessions.
"""

import os
import sys

from mcp.server.fastmcp import FastMCP

fixture = FastMCP("fixture", instructions="Fixture server: use `add` for arithmetic.")


@fixture.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@fixture.tool()
def fail(message: str) -> str:
    """Raise an error with the given message."""
    raise RuntimeError(message)


@fixture.tool()
def big(n: int) -> str:
    """Return n bytes of output."""
    return "x" * n


@fixture.tool()
def env_probe(name: str) -> str:
    """Return the value of an environment variable, or '<unset>'."""
    return os.environ.get(name, "<unset>")


@fixture.tool()
def die() -> None:
    """Exit the server process immediately (for restart tests).

    WARNING: only safe when the server runs as a subprocess. Calling this
    through the in-memory transport kills the host process via os._exit —
    no exception, no cleanup.
    """
    os._exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--http":
        fixture.settings.host = "127.0.0.1"
        fixture.settings.port = int(sys.argv[2])  # ValueError is acceptably loud
        fixture.run("streamable-http")
    elif len(sys.argv) == 2 and sys.argv[1] == "--http":
        sys.exit("usage: mcp_fixture_server.py --http PORT")
    else:
        fixture.run("stdio")
