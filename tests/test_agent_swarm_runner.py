"""Native runner wiring checks; model capability needs a separate live run."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("explicit_env", [False, True])
@pytest.mark.parametrize("override", ["repository", "index", "objects", "configuration"])
def test_git_checks_use_selected_workspace_despite_environment(tmp_path, monkeypatch, explicit_env, override):
    runner = load_runner()
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    clean_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(root, *args):
        return subprocess.run(["git", *args], cwd=root, env=clean_env, check=True,
                              capture_output=True, text=True).stdout.strip()

    workspace, decoy = tmp_path / "workspace", tmp_path / "decoy"
    for root in (workspace, decoy):
        root.mkdir()
        git(root, "init", "-b", root.name)
        (root / "tracked.txt").write_text(root.name)
        git(root, "add", "tracked.txt")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", root.name)
    expected_head = git(workspace, "rev-parse", "HEAD")
    (decoy / "decoy-only.txt").write_text("unrelated work")
    decoy_index = (decoy / ".git/index").read_bytes()
    overrides = {
        "repository": {"GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy),
                       "GIT_COMMON_DIR": str(decoy / ".git")},
        "index": {"GIT_INDEX_FILE": str(decoy / ".git/index")},
        "objects": {"GIT_OBJECT_DIRECTORY": str(tmp_path / "missing-objects"),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(decoy / ".git/objects")},
        "configuration": {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "status.showUntrackedFiles",
                          "GIT_CONFIG_VALUE_0": "no"},
    }[override]
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    supplied = dict(os.environ, **overrides) if explicit_env else None
    if not explicit_env:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
    before = dict(os.environ) if supplied is None else supplied.copy()

    def check(*args):
        return runner.command(["git", *args], cwd=workspace, env=supplied).strip()

    assert Path(check("rev-parse", "--show-toplevel")) == workspace
    assert check("branch", "--show-current") == "workspace"
    assert check("rev-parse", "HEAD") == expected_head
    assert check("status", "--porcelain") == ""
    (workspace / "selected-only.txt").write_text("selected work")
    assert check("status", "--porcelain") == "?? selected-only.txt"
    assert (decoy / ".git/index").read_bytes() == decoy_index
    assert (dict(os.environ) if supplied is None else supplied) == before


@pytest.mark.parametrize("explicit_env", [False, True])
def test_non_git_commands_keep_their_environment(tmp_path, monkeypatch, explicit_env):
    runner = load_runner()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "caller-repository"))
    monkeypatch.setenv("ORCHESTRATION_STATE_DIR", str(tmp_path / "queue"))
    supplied = dict(os.environ) if explicit_env else None
    if supplied is not None:
        supplied.update(GIT_DIR=str(tmp_path / "explicit-repository"),
                        ORCHESTRATION_STATE_DIR=str(tmp_path / "explicit-queue"))
    expected = supplied if supplied is not None else os.environ
    result = runner.command([sys.executable, "-c", "import json, os; "
                             "print(json.dumps([os.environ['GIT_DIR'], os.environ['ORCHESTRATION_STATE_DIR']]))"],
                            env=supplied)
    assert json.loads(result) == [expected["GIT_DIR"], expected["ORCHESTRATION_STATE_DIR"]]


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
