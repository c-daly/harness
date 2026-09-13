"""Native runner wiring checks; model capability needs a separate live run."""

import argparse
import importlib.util
import json
from pathlib import Path

from harness.catalog import Catalog
from harness.events import AgentRunFinished, SubagentSpawned
from harness.log import read_session
from harness.provider import FakeProvider, Usage, text_turn, tool_call_turn


def load_runner():
    path = Path(__file__).parents[1] / "plugins/agent-swarm-runner/run_one.py"
    spec = importlib.util.spec_from_file_location("swarm_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_manifest_request_reaches_native_child_and_never_completes_queue(
    tmp_path, monkeypatch
):
    runner = load_runner()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    output = tmp_path / "evidence"
    output.mkdir()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("project: test\n")
    catalog = Catalog(
        entries={
            "test": {
                "route": "openai/test",
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
                "usage_accounting": "reported",
            }
        }
    )
    monkeypatch.setattr(runner.Catalog, "load", lambda path: catalog)
    usage = Usage(input_tokens=10, output_tokens=10)
    provider = FakeProvider(
        [
            tool_call_turn(
                "",
                "dispatch_agent",
                {"agent": "wrong", "model": "unapproved", "prompt": "substituted task"},
                usage=usage,
            ),
            tool_call_turn(
                "",
                "write_file",
                {"file_path": "result.txt", "content": "child effect"},
                usage=usage,
            ),
            text_turn("child completed", usage=usage),
            tool_call_turn("", "dispatch_agent", {"prompt": "duplicate dispatch"}, usage=usage),
            text_turn("coordinator finished", usage=usage),
        ]
    )
    provider.usage_accounting = lambda model: "reported"
    monkeypatch.setattr(runner, "CatalogProvider", lambda value: provider)
    monkeypatch.setattr(
        runner,
        "command",
        lambda argv, **kwargs: (
            "task/test\n" if "--show-current" in argv else "abc123\n" if "rev-parse" in argv else ""
        ),
    )
    queue_calls = []
    monkeypatch.setattr(runner, "plugin_command", lambda args, *parts: queue_calls.append(parts))
    args = argparse.Namespace(
        output=output,
        catalog=tmp_path / "catalog.toml",
        model="test",
        max_model_calls=8,
        timeout=30,
        manifest=manifest,
    )
    await runner.execute(
        args,
        {
            "task_name": "test",
            "worktree_dir": str(workspace),
            "branch_name": "task/test",
            "prompt": "The actual plugin-selected task",
        },
    )
    report = json.loads((output / "result.json").read_text())
    assert (workspace / "result.txt").read_text() == "child effect"
    events = list(read_session(output / "journal", report["root_session_id"]))
    assert sum(isinstance(e.event, SubagentSpawned) for e in events) == 1
    child_id = next(
        e.event.child_session_id for e in events if isinstance(e.event, SubagentSpawned)
    )
    child_events = list(read_session(output / "journal", child_id))
    assert child_events[0].event.parent_session_id == report["root_session_id"]
    assert any(
        isinstance(e.event, AgentRunFinished) and e.event.result.status == "completed"
        for e in child_events
    )
    child_messages = provider.calls[1]
    assert any("The actual plugin-selected task" in m.text() for m in child_messages)
    assert not any("substituted task" in m.text() for m in child_messages)
    assert len(queue_calls) == 1 and queue_calls[0][0] == "spawned"
    assert report["queue_completion_recorded"] is False


async def test_dirty_task_refused_before_queue_mutation_or_inference(tmp_path, monkeypatch):
    import pytest

    runner = load_runner()
    monkeypatch.setattr(
        runner,
        "command",
        lambda argv, **kwargs: "task/test\n" if "--show-current" in argv else " M existing.py\n",
    )
    queue_calls = []
    monkeypatch.setattr(runner, "plugin_command", lambda *args: queue_calls.append(args))
    with pytest.raises(ValueError, match="dirty"):
        await runner.execute(
            argparse.Namespace(), {"worktree_dir": str(tmp_path), "branch_name": "task/test"}
        )
    assert queue_calls == []


async def test_verified_binding_continues_native_worker_and_withholds_release(
    tmp_path, monkeypatch
):
    import hashlib
    import subprocess
    import sys

    directory = Path(__file__).parents[1] / "plugins/agent-swarm-runner"
    monkeypatch.syspath_prepend(str(directory))
    import run_verified
    import run_one

    workspace = tmp_path / "work"
    workspace.mkdir()
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
    output = tmp_path / "output"
    output.mkdir()
    script = tmp_path / "check.py"
    script.write_text(
        "from pathlib import Path\nassert Path('result.txt').read_text() == 'ready'\n"
    )
    checks = tmp_path / "checks.json"
    checks.write_text(
        json.dumps(
            {
                "files": {str(script): hashlib.sha256(script.read_bytes()).hexdigest()},
                "checks": [
                    {
                        "id": "result",
                        "description": "result is ready",
                        "argv": [sys.executable, str(script)],
                    }
                ],
            }
        )
    )
    catalog = Catalog(entries={"test": {"route": "openai/test"}})
    monkeypatch.setattr(run_one.Catalog, "load", lambda path: catalog)
    provider = FakeProvider(
        [
            text_turn("All done!"),
            tool_call_turn("", "write_file", {"file_path": "result.txt", "content": "ready"}),
            text_turn("Now implemented"),
        ]
    )
    monkeypatch.setattr(run_one, "CatalogProvider", lambda catalog: provider)
    queue_calls = []
    monkeypatch.setattr(
        run_verified, "plugin_command", lambda args, *parts: queue_calls.append(parts)
    )
    args = argparse.Namespace(
        output=output,
        checks=checks,
        catalog=tmp_path / "catalog",
        model="test",
        max_model_calls=10,
        max_attempts=3,
        timeout=30,
        pause_after=None,
    )
    state = await run_verified.execute(
        args,
        {
            "task_name": "test",
            "branch_name": "task/test",
            "worktree_dir": str(workspace),
            "prompt": "Create result.txt with ready",
        },
    )
    assert state.status == "checks_passed" and state.attempts == 2
    report = json.loads((output / "result.json").read_text())
    assert report["queue_completion_recorded"] is False
    assert [call[0] for call in queue_calls] == ["spawned"]
    assert (
        len(
            [
                e
                for e in read_session(output / "journal", report["root_session_id"])
                if isinstance(e.event, SubagentSpawned)
            ]
        )
        == 2
    )
