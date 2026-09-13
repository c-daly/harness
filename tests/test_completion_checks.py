import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness.blobs import BlobStore
from harness.completion_checks import CommandCheck, CommandChecks, CommandVerifier, workspace_digest


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
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
    return root


def verifier(tmp_path, workspace, source, *, timeout=10):
    script = tmp_path / "operator_check.py"
    script.write_text(source)
    specification = CommandChecks(
        checks=(
            CommandCheck(
                id="behavior",
                description="Independent behavior check",
                argv=(sys.executable, str(script)),
                timeout_seconds=timeout,
            ),
        ),
        files={str(script): hashlib.sha256(script.read_bytes()).hexdigest()},
    )
    return CommandVerifier(
        specification, workspace=workspace, blobs=BlobStore(tmp_path / "blobs")
    ), script


async def test_real_command_checks_current_workspace_and_retains_output(workspace, tmp_path):
    check, _ = verifier(
        tmp_path,
        workspace,
        "from pathlib import Path\nassert Path('result').read_text() == 'ready'\n",
    )
    workspace.joinpath("result").write_text("wrong")
    failed = await check()
    workspace.joinpath("result").write_text("ready")
    passed = await check()
    assert failed.checks[0].status == "failed" and passed.checks[0].status == "passed"
    assert failed.workspace_sha256 != passed.workspace_sha256
    assert "AssertionError" in json.loads(check.blobs.get(failed.checks[0].artifact))["stderr"]


async def test_verifier_drift_refuses_replacement_and_symlink(workspace, tmp_path):
    check, script = verifier(tmp_path, workspace, "raise SystemExit(1)\n")
    original = script.read_bytes()
    script.write_text("raise SystemExit(0)\n")
    with pytest.raises(ValueError, match="verifier changed"):
        await check()
    script.unlink()
    target = tmp_path / "other.py"
    target.write_bytes(original)
    script.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic links"):
        await check()


def test_dotdot_cannot_put_a_pinned_verifier_inside_the_candidate(workspace, tmp_path):
    workspace.joinpath("check.py").write_text("pass\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = outside / ".." / "work" / "check.py"
    specification = CommandChecks(
        checks=(CommandCheck(id="x", description="d", argv=(sys.executable, str(alias))),),
        files={str(alias): hashlib.sha256(alias.read_bytes()).hexdigest()},
    )
    with pytest.raises(ValueError, match="outside the candidate"):
        CommandVerifier(specification, workspace=workspace, blobs=BlobStore(tmp_path / "blobs"))


async def test_workspace_identity_includes_untracked_and_modes_ignores_cache(workspace):
    before = await workspace_digest(workspace)
    workspace.joinpath(".gitignore").write_text("cache/\n")
    workspace.joinpath("result").write_text("ready")
    changed = await workspace_digest(workspace)
    assert changed != before
    workspace.joinpath("cache").mkdir()
    workspace.joinpath("cache/output").write_text("ignored")
    assert await workspace_digest(workspace) == changed
    workspace.joinpath("result").chmod(0o755)
    assert await workspace_digest(workspace) != changed


@pytest.mark.parametrize("stop", ["timeout", "output", "cancel"])
async def test_check_process_group_is_reaped_on_every_stop(workspace, tmp_path, stop):
    marker = tmp_path / "child-pid"
    # A concrete marker synchronizes cancellation with the subprocess. The
    # forked child closes pipes so success cannot hide a still-running worker.
    source = (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.close(1)\n    os.close(2)\n    time.sleep(60)\n"
        "else:\n"
        f"    open({str(marker)!r}, 'w').write(str(pid))\n"
        + ("    print('x' * 100000, flush=True)\n" if stop == "output" else "")
        + "    time.sleep(60)\n"
    )
    check, _ = verifier(tmp_path, workspace, source, timeout=1 if stop == "timeout" else 10)
    running = asyncio.create_task(check())
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        if stop == "cancel":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            assert (await running).checks[0].status == "error"
        pid = int(marker.read_text())
        async with asyncio.timeout(3):
            while Path(f"/proc/{pid}/stat").exists():
                if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                    break
                await asyncio.sleep(0.01)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), 9)
            except ProcessLookupError:
                pass
