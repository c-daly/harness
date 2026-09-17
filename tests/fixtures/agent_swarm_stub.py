"""Stub agent-swarm workflow MCP server for tests.

In-memory only: state does not survive a process restart. That is
deliberate -- it is what lets tests prove that a workflow started before a
plugin restart is not resumable by core after that restart (see
docs/plugin-reconciliation.md). This server exists only under
tests/fixtures and must never be pointed at real work.

Tools:
  workflow__workflow_start(workflow_id, phase="start") -- create a workflow
  workflow__workflow_advance_phase(workflow_id, phase) -- move to a new phase
  workflow__workflow_get_state(workflow_id) -- report the current phase and status
  workflow__workflow_stop(workflow_id) -- mark a workflow finished

Every tool returns a JSON string. A workflow_id the store does not know
returns {"error": "..."} rather than raising; errors are values here too.
Run: python3 agent_swarm_stub.py (stdio transport)
"""

import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "agent_swarm_stub",
    instructions="Stub agent-swarm workflow server (tests only, in-memory).",
)

_WORKFLOWS: dict[str, dict] = {}


def _not_found(workflow_id: str) -> str:
    return json.dumps({"error": f"workflow_id {workflow_id} not found"})


@mcp.tool()
def workflow__workflow_start(workflow_id: str, phase: str = "start") -> str:
    """Start a new workflow. Returns an error if workflow_id is already in use."""
    if workflow_id in _WORKFLOWS:
        return json.dumps({"error": f"workflow_id {workflow_id} already started"})
    _WORKFLOWS[workflow_id] = {"phase": phase, "status": "active"}
    return json.dumps({"workflow_id": workflow_id, "phase": phase, "status": "active"})


@mcp.tool()
def workflow__workflow_advance_phase(workflow_id: str, phase: str) -> str:
    """Move a workflow to a new phase without changing its status."""
    workflow = _WORKFLOWS.get(workflow_id)
    if workflow is None:
        return _not_found(workflow_id)
    workflow["phase"] = phase
    return json.dumps({"workflow_id": workflow_id, "phase": phase, "status": workflow["status"]})


@mcp.tool()
def workflow__workflow_get_state(workflow_id: str) -> str:
    """Report the current phase and status of a workflow."""
    workflow = _WORKFLOWS.get(workflow_id)
    if workflow is None:
        return _not_found(workflow_id)
    return json.dumps(
        {"workflow_id": workflow_id, "phase": workflow["phase"], "status": workflow["status"]}
    )


@mcp.tool()
def workflow__workflow_stop(workflow_id: str) -> str:
    """Mark a workflow finished."""
    workflow = _WORKFLOWS.get(workflow_id)
    if workflow is None:
        return _not_found(workflow_id)
    workflow["phase"] = "stopped"
    workflow["status"] = "finished"
    return json.dumps({"workflow_id": workflow_id, "phase": "stopped", "status": "finished"})


if __name__ == "__main__":
    mcp.run("stdio")
