import argparse
import hashlib
import json
from pathlib import Path


from harness.catalog import Catalog
from harness.events import AgentRunStarted, SubagentSpawned
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn


async def test_verified_runner_without_supervisor_limits_allows_multiple_attempts(
    tmp_path, monkeypatch
):
    # Load modules directly from plugin dir to avoid path surprises
    directory = Path(__file__).parents[1] / "plugins/agent-swarm-runner"
    monkeypatch.syspath_prepend(str(directory))
    import run_verified
    import run_one

    # Prepare a clean git workspace
    workspace = tmp_path / "work"
    workspace.mkdir()
    import subprocess
    import sys

    subprocess.run(["git", "init", "-qb", "task/test", str(workspace)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )

    output = tmp_path / "out"
    output.mkdir()
    script = tmp_path / "check.py"
    script.write_text("from pathlib import Path\nassert Path('result.txt').read_text() == 'ok'\n")
    checks = tmp_path / "checks.json"
    description = "CLI contract: result.txt must contain exactly ok, without a trailing newline"
    checks.write_text(
        json.dumps(
            {
                "files": {str(script): hashlib.sha256(script.read_bytes()).hexdigest()},
                "checks": [
                    {
                        "id": "exists",
                        "description": description,
                        "argv": [sys.executable, str(script)],
                    }
                ],
            }
        )
    )

    # Catalog and provider
    catalog = Catalog(entries={"test": {"route": "openai/test"}})
    monkeypatch.setattr(run_one.Catalog, "load", lambda path: catalog)
    provider = FakeProvider(
        [
            text_turn("first"),
            text_turn("second"),
            tool_call_turn("", "write_file", {"file_path": "result.txt", "content": "ok"}),
            text_turn("third"),
        ]
    )
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
        worker_timeout=120,
        pause_after=None,
    )

    state = await run_verified.execute(
        args,
        {
            "task_name": "t",
            "branch_name": "task/test",
            "worktree_dir": str(workspace),
            "prompt": "Create result.txt with ok",
        },
    )
    # must have resulted in exactly three attempts per provider script (two partial texts then write then final text)
    assert state.status == "checks_passed" and state.attempts == 3
    assert state.deadline is None
    report = json.loads((output / "result.json").read_text())
    # three subagent spawns (one per attempt)
    events = list(read_session(output / "journal", report["root_session_id"]))
    assert sum(isinstance(e.event, SubagentSpawned) for e in events) == 3
    # no queue completion auto-recorded
    assert report["queue_completion_recorded"] is False
    launch = json.loads((output / "launch.json").read_text())
    assert launch["worker_timeout"] == 120
    assert launch["effective_execution_limits"]["task_timeout_seconds"] == 120
    for env in events:
        if isinstance(env.event, SubagentSpawned):
            child_events = read_session(output / "journal", env.event.child_session_id)
            started = next(e.event for e in child_events if isinstance(e.event, AgentRunStarted))
            assert started.limits["timeout_seconds"] == 120
    assert all(any(description in m.text() for m in call) for call in provider.calls)
    assert [call[0] for call in queue] == ["spawned"]
    # artifact exists with expected content
    assert (workspace / "result.txt").read_text() == "ok"
