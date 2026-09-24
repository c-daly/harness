"""Opt-in controls using the actual installed memory plugin over real MCP.

HARNESS_MEMORY_PLUGIN points at that plugin's root. The vault is always an
isolated pytest directory; the user's normal memories are never test fixtures.
"""

import json
import os
from pathlib import Path
import sys

import pytest

from harness.agent import AgentTask
from harness.capture import capture_state
from harness.cli import build_kernel
from harness.context import ContextPolicy
from harness.hooks import ProposedToolCall
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, RuleSet
from harness.provider import FakeProvider, text_turn
from harness.types import ModelId, ToolName, new_call_id


PLUGIN = os.environ.get("HARNESS_MEMORY_PLUGIN")
pytestmark = pytest.mark.skipif(not PLUGIN, reason="set HARNESS_MEMORY_PLUGIN for installed-plugin controls")
PREFIX = "mcp__resident-memory__"
CORRECTION = "Use staging for this project. Never target production without my explicit request."


@pytest.fixture
def setup(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "10-projects" / "outing").mkdir(parents=True)
    monkeypatch.setenv("HARNESS_CAPTURE_TEST_VAULT", str(vault))
    server = Path(__file__).parents[1] / "plugins/resident-memory/server.py"
    spec = McpServerSpec(name="resident-memory", transport="stdio", command=sys.executable,
        args=(str(server), "--memory-plugin", PLUGIN, "--project", "outing"),
        env={"MEMORY_VAULT_DIR": "HARNESS_CAPTURE_TEST_VAULT"}, restart="never")
    return tmp_path, vault, spec


async def launch(setup, *, source=None):
    root, _, spec = setup
    policy = ContextPolicy.model_validate({"capture": dict(project="outing", workspace=str(root),
        prepare_tool=PREFIX + "capture_prepare", write_tool=PREFIX + "capture_write", model="recorder"),
        "sources": [dict(id="record", tool=PREFIX + "memory_read",
                         args=dict(name=source, type="project", max_bytes=8192), max_bytes=8192)] if source else []})
    kernel = build_kernel(base_dir=root / "state", workspace_root=root, native_tools=True,
        provider=FakeProvider([text_turn("Understood."), text_turn(CORRECTION)]),
        model=ModelId("fake"), context_policy=policy, mcp=[spec],
        permissions=PermissionEngine([RuleSet(default="allow")]))
    warnings = await kernel.mcp.start()
    assert not warnings
    await kernel.loop.start()
    kernel.mcp.flush_events()
    return kernel


async def close(kernel):
    await kernel.mcp.stop()
    kernel.session.close()


def saved(kernel):
    events = read_session(kernel.session.base, kernel.session.id)
    requests, records, observed = capture_state(events)
    request = next(iter(requests.values()))
    observation = observed[request.id]
    assert observation.status == "saved", observation
    return request, records[request.id].record, json.loads(kernel.session.blobs.get(observation.receipt))


async def write(kernel, args):
    return await kernel.loop.dispatcher.dispatch_tool(
        ProposedToolCall(new_call_id(), ToolName(PREFIX + "capture_write"), args), purpose="capture")


async def test_real_recorder_writer_and_fresh_session_normal_reader(setup):
    first = await launch(setup)
    try:
        await first.loop.run_task(AgentTask(prompt=CORRECTION))
        _, _, receipt = saved(first)
        original_id = first.session.id
    finally:
        await close(first)
    second = await launch(setup, source=receipt["name"])
    try:
        await second.loop.run_task(AgentTask(prompt="Which environment should I use?"))
        assert second.session.id != original_id
        assert CORRECTION in "\n".join(m.text() for m in second.provider.calls[0])
        assert not any(CORRECTION in m.text() for m in second.loop.history)
        assert len(list(setup[1].rglob("*harness-capture*.md"))) == 2
    finally:
        await close(second)


async def test_real_writer_reconciles_repeated_write_and_refuses_changed_content(setup):
    kernel = await launch(setup)
    try:
        await kernel.loop.run_task(AgentTask(prompt=CORRECTION))
        request, record, original = saved(kernel)
        args = dict(capture_id=request.id, project="outing", record=kernel.session.blobs.get(record).decode(),
                    destination=original["destination"])
        repeated = await write(kernel, args)
        assert not repeated.is_error and json.loads(repeated.read_text()) == original
        changed = await write(kernel, dict(args, record="different record"))
        assert changed.is_error
        wrong_project = await write(kernel, dict(args, project="another-project"))
        assert wrong_project.is_error
        wrong_destination = await write(kernel, dict(args, destination="f" * 64))
        assert wrong_destination.is_error
        assert len(list(setup[1].rglob("*harness-capture*.md"))) == 1
    finally:
        await close(kernel)


async def test_real_writer_repairs_put_before_index_crash_window(setup):
    kernel = await launch(setup)
    try:
        await kernel.loop.run_task(AgentTask(prompt=CORRECTION))
        request, record, original = saved(kernel)
        index = setup[1] / "MEMORY.md"
        # Simulate a process dying after the memory file landed but before its
        # index bullet did. The existing plugin's recovery must repair this.
        index.write_text("\n".join(line for line in index.read_text().splitlines()
                                   if original["name"] not in line) + "\n")
        repeated = await write(kernel, dict(capture_id=request.id, project="outing",
                                            record=kernel.session.blobs.get(record).decode(),
                                            destination=original["destination"]))
        assert not repeated.is_error and json.loads(repeated.read_text()) == original
        assert index.read_text().count(original["name"]) == 2  # path and link label
        assert len(list(setup[1].rglob("*harness-capture*.md"))) == 1
    finally:
        await close(kernel)
