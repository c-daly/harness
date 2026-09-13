import argparse
import hashlib
import json
from pathlib import Path

import pytest

from harness.catalog import Catalog
from harness.events import SubagentSpawned
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn


def test_verified_runner_without_supervisor_limits_allows_multiple_attempts(tmp_path, monkeypatch):
    # Load modules directly from plugin dir to avoid path surprises
    directory = Path(__file__).parents[1] / "plugins/agent-swarm-runner"
    monkeypatch.syspath_prepend(str(directory))
    import run_verified
    import run_one

    # Prepare a clean git workspace
    workspace = tmp_path / "work"
    workspace.mkdir()
    import subprocess, sys

    subprocess.run(["git", "init", "-qb", "task/test", str(workspace)], check=True)
    subprocess.run([
        "git", "-C", str(workspace),
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "initial"
    ], check=True)

    output = tmp_path / "out"
    output.mkdir()
    script = tmp_path / "check.py"
    script.write_text("from pathlib import Path\nprint(Path('result.txt').exists())\n")
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({
        "files": {str(script): hashlib.sha256(script.read_bytes()).hexdigest()},
        "checks": [{"id": "exists", "description": "file exists", "argv": [sys.executable, str(script)]}]
    }))

    # Catalog and provider
    catalog = Catalog(entries={"test": {"route": "openai/test"}})
    monkeypatch.setattr(run_one.Catalog, "load", lambda path: catalog)
    provider = FakeProvider([
        text_turn("first"),
        text_turn("second"),
        tool_call_turn("", "write_file", {"file_path": "result.txt", "content": "ok"}),
        text_turn("third"),
    ])
    monkeypatch.setattr(run_one, "CatalogProvider", lambda catalog: provider)

    # Capture queue interaction but don't fail on it
    queue = []
    monkeypatch.setattr(run_verified, "plugin_command", lambda args, *parts: queue.append(parts))

    args = argparse.Namespace(
        output=output,
        checks=checks,
        catalog=tmp_path / "catalog",
        model="test",
        max_model_calls=20,
        max_attempts=None,  # omitted supervisor limit
        timeout=None,
        pause_after=None,
    )

    state = pytest.run(async_fn=run_verified.execute, args=(args, {
        "task_name": "t",
        "branch_name": "task/test",
        "worktree_dir": str(workspace),
        "prompt": "Create result.txt",
    }))
    # must have allowed more than one worker attempt before success
    assert state.attempts >= 2
    events = list(read_session(output / "journal", state.history[0].parent_session_id)) if state.history else []
    # and should not have failed kernel construction due to timeout None
    assert any(isinstance(e.event, SubagentSpawned) for e in read_session(output / "journal", state.root_session_id))
