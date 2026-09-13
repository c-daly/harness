"""Independent checks of verified-runner restart controls and cumulative work."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from harness.catalog import Catalog
from harness.events import SubagentSpawned, ExecutionCountsRecorded
from harness.log import read_session
from harness.provider import FakeProvider, text_turn, tool_call_turn


def setup_run(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path.cwd() / "plugins/agent-swarm-runner"))
    import run_one
    import run_verified

    workspace = tmp_path / "work"
    subprocess.run(["git", "init", "-qb", "task/restart", str(workspace)], check=True)
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
    script = tmp_path / "independent_check.py"
    script.write_text(
        "from pathlib import Path\nassert Path('answer.txt').read_text() == 'verified'\n"
    )
    checks = tmp_path / "checks.json"
    checks.write_text(
        json.dumps(
            {
                "files": {str(script): hashlib.sha256(script.read_bytes()).hexdigest()},
                "checks": [
                    {
                        "id": "answer",
                        "description": "answer.txt contains verified",
                        "argv": [sys.executable, str(script)],
                    }
                ],
            }
        )
    )
    catalog = Catalog(entries={"test": {"route": "openai/test"}})
    provider = FakeProvider(
        [
            text_turn("Partial work; not finished"),
            tool_call_turn("", "write_file", {"file_path": "answer.txt", "content": "verified"}),
            text_turn("Done"),
        ]
    )
    monkeypatch.setattr(run_one.Catalog, "load", lambda _: catalog)
    monkeypatch.setattr(run_one, "CatalogProvider", lambda _: provider)
    queue = []
    monkeypatch.setattr(run_verified, "plugin_command", lambda args, *parts: queue.append(parts))
    args = argparse.Namespace(
        output=output,
        checks=checks,
        catalog=tmp_path / "catalog",
        model="test",
        max_model_calls=10,
        max_attempts=None,
        timeout=None,
        worker_timeout=43.0,
        pause_after=1,
    )
    request = {
        "task_name": "restart",
        "branch_name": "task/restart",
        "worktree_dir": str(workspace),
        "prompt": "Create answer.txt containing verified",
    }
    return run_verified, args, request, provider, queue


async def test_pause_restart_retains_attempts_and_native_execution_counts(tmp_path, monkeypatch):
    runner, args, request, provider, queue = setup_run(tmp_path, monkeypatch)
    first = await runner.execute(args, request)
    assert first.status == "paused" and first.attempts == 1
    root = json.loads((args.output / "result.json").read_text())["root_session_id"]
    resumed = await runner.execute(args, request, resume=root)
    assert resumed.status == "checks_passed" and resumed.attempts == 2
    rows = [e.event for e in read_session(args.output / "journal", root, repair=False)]
    assert len([e for e in rows if isinstance(e, SubagentSpawned)]) == 2
    restored = [e for e in rows if isinstance(e, ExecutionCountsRecorded) and e.kind == "attach"]
    assert any(e.model_calls == 1 and e.children == 1 for e in restored)
    assert len(provider.calls) == 3
    assert [c[0] for c in queue] == ["spawned"]
    assert not json.loads((args.output / "result.json").read_text())["queue_completion_recorded"]


async def test_resume_rejects_changed_worker_timeout_before_more_inference(tmp_path, monkeypatch):
    runner, args, request, provider, queue = setup_run(tmp_path, monkeypatch)
    assert (await runner.execute(args, request)).status == "paused"
    root = json.loads((args.output / "result.json").read_text())["root_session_id"]
    args.worker_timeout = 44.0
    with pytest.raises(ValueError, match="configuration differs"):
        await runner.execute(args, request, resume=root)
    assert len(provider.calls) == 1
    assert [c[0] for c in queue] == ["spawned"]
    assert not (Path(request["worktree_dir"]) / "answer.txt").exists()


@pytest.mark.parametrize(
    "mode, expected",
    [("verified_default", 600.0), ("verified_explicit", 43.0), ("legacy_run_one", 12.0)],
)
async def test_worker_timeout_is_distinct_from_overall_deadline(
    tmp_path, monkeypatch, mode, expected
):
    runner, args, request, provider, queue = setup_run(tmp_path, monkeypatch)
    import run_one

    args.timeout = 12.0
    if mode == "legacy_run_one":
        del args.worker_timeout
    else:
        args.worker_timeout = 43.0 if mode == "verified_explicit" else None
    kernel, _ = run_one.build_native_kernel(args, Path(request["worktree_dir"]))
    try:
        assert (
            kernel.runner.scope_for(kernel.session).budget.limits.task_timeout_seconds == expected
        )
    finally:
        await kernel.resources.close(emit=kernel.session.append)
        kernel.session.close()
