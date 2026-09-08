"""Public fault journey: controlled Codex-compatible process, real MCP, local continuation.

The source process is a fixture, not a live subscription agent. No credentials are
copied. The opt-in CLI requires the pinned, offline, resource-bounded container.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from harness.agent import TaskLimits
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.context import ContextPolicy
from harness.errors import ProviderError
from harness.fold import fold
from harness.handoff import ExactCall, HandoffSpec, Resolution, read_handoffs, snapshot
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider_codex import CodexProvider, _SCRATCH_CONFIG_TOML
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, close, isolation, sha256

ROOT = Path(__file__).resolve().parent.parent


def controlled_source(root):
    binary = root / "controlled-codex"
    binary.write_text(f"#!{sys.executable}\n" + '''
import asyncio, json, os, sys
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
root = Path(sys.argv[0]).parent
(root / "source.pid").write_text(str(os.getpid()))
assert not (Path(os.environ["CODEX_HOME"]) / "auth.json").exists()
assert sys.argv[1:3] == ["exec", "--json"]
sys.stdin.read()
url_arg = next(a for a in sys.argv if a.startswith("mcp_servers.harness.url="))
url = json.loads(url_arg.split("=", 1)[1])
async def run():
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("write_file", {
                "file_path": str(root / "project/A.txt"), "content": "stage A\\n"})
            assert not result.isError, result
    (root / "project/native.txt").write_text("external native effect")
asyncio.run(run())
print(json.dumps({"type": "turn.failed", "error": {"message": "controlled failure after writes"}}), flush=True)
sys.exit(1)
''')
    binary.chmod(0o700)
    return binary


def empty_auth_home():
    directory = tempfile.mkdtemp(prefix="harness-handoff-empty-auth-")
    (Path(directory) / "config.toml").write_text(_SCRATCH_CONFIG_TOML)
    return directory


def process_stopped(root):
    try:
        os.kill(int((root / "source.pid").read_text()), 0)
    except ProcessLookupError:
        return True
    return False


async def journey(root, provider):
    project = root / "project"
    project.mkdir()
    (project / "PROJECT.md").write_text("The two-stage task produces A.txt and B.txt. Keep completed files intact.\n")
    provider.codex = CodexProvider(binary=str(controlled_source(root)))
    engine = PermissionEngine([RuleSet(rules=[PermissionRule("allow", item) for item in
        ("model:external", "model:local-small", "read_file", "write_file")], default="deny")])
    profile = ContextPolicy.load(ROOT / "docs/examples/local-resident/project.toml")
    kernel = build_kernel(base_dir=root / "sessions", provider=provider, model=ModelId("external"),
        native_tools=True, workspace_root=project, permissions=engine, context_policy=profile)
    checks, report = {}, {"checks": {}, "passed": False}
    report["checks"] = checks
    try:
        await kernel.loop.start()
        task_id = kernel.tasks.create("Complete stages A and B").id
        for letter in ("A", "B"):
            args = {"file_path": str(project / f"{letter}.txt"), "content": f"stage {letter}\n"}
            expected = f"Created {project / f'{letter}.txt'} (1 lines)."
            kernel.tasks.add_requirement({"id": letter, "description": f"Stage {letter} written",
                "check": {"kind": "tool_result", "tool": "write_file", "args": args,
                          "sha256": hashlib.sha256(expected.encode()).hexdigest()}})
        kernel.tasks.add_requirement({"id": "review", "description": "Operator reviews all effects"})
        task = kernel.tasks.prepare("Complete A and B, including native work.").model_copy(update={
            "limits": TaskLimits(max_iterations=8, timeout_seconds=45)})
        append = kernel.session.append
        terminals = []

        def checked_append(event):
            if event.type == "agent_run_finished" and (root / "source.pid").exists():
                terminals.append(process_stopped(root))
            return append(event)

        kernel.session.append = checked_append
        with patch("harness.provider_codex._scratch_codex_home", empty_auth_home):
            try:
                await kernel.loop.run_task(task)
            except ProviderError:
                checks["source_failed"] = True
        checks["source_reaped_before_terminal"] = bool(terminals) and all(terminals)
        checkpoint = snapshot(kernel.session)
        checks["source_written_once"] = (project / "A.txt").read_text() == "stage A\n"
        checks["native_effect_exists_but_is_opaque"] = ((project / "native.txt").read_text() == "external native effect"
            and any(e.kind == "provider_native" and e.state == "uncertain" for e in checkpoint.effects))
        spec = HandoffSpec(snapshot_sha256=checkpoint.digest, model=ModelId("local-small"),
            continuation="Write B.txt with exactly 'stage B' followed by a newline, using the allowed write_file call. "
                         "A.txt and native.txt are complete. Then briefly confirm stage B.", process_stopped=True,
            resolutions=tuple(Resolution(effect_id=e.id, status="completed",
                note="Inspected native.txt and confirmed the controlled process exited.")
                for e in checkpoint.effects if e.state == "uncertain"),
            allowed_calls=(ExactCall(tool="write_file", args={"file_path": str(project / "B.txt"),
                "content": "stage B\n"}),))
        record = kernel.handoffs.record(spec)
        session_id = kernel.session.id
        await close(kernel)
        kernel = build_kernel(base_dir=root / "sessions", provider=provider, model=ModelId("local-small"),
            resume_session_id=session_id, native_tools=True, workspace_root=project, permissions=engine)
        started = time.monotonic()
        result = await kernel.handoffs.run(record.id)
        report["continuation_seconds"] = round(time.monotonic() - started, 3)
        checks["completed"] = result.status == "completed"
        checks["same_task"] = kernel.tasks.selected().definition.id == task_id
        checks["remaining_artifact_exact"] = (project / "B.txt").read_text() == "stage B\n"
        checks["completed_artifacts_unchanged"] = ((project / "A.txt").read_text() == "stage A\n"
            and (project / "native.txt").read_text() == "external native effect")
        checked = kernel.tasks.check()
        checks["evidence_preserved"] = (checked.evidence["A"].status == checked.evidence["B"].status == "passed"
            and checked.evidence["A"].source_seq < checked.run_started_seq < checked.evidence["B"].source_seq)
        checks["review_still_required"] = checked.unresolved == ("review",) and not checked.accepted
        log = read_session(kernel.session.base, session_id)
        outcomes = [e.event for e in log if e.event.type == "tool_call_completed" and not e.event.is_error]
        dispatched = {e.event.call_id: e.event for e in log if e.event.type == "dispatch_resolved"}
        checks["only_two_writes"] = sum(dispatched[e.call_id].tool == "write_file" for e in outcomes) == 2
        ready = [e.event for e in log if e.event.type == "context_source_observed"
                 and e.event.status == "ready" and e.event.run_id == result.run_id]
        checks["normal_project_context_ready"] = len(ready) == 1 and ready[0].source_id == "project-record"
        state = fold(log)
        checks["settled"] = not (state.open_intents or state.open_agent_runs or state.open_model_intents)
        await close(kernel)
        # Replay uses the durable record only, without rebuilding a provider or running inference.
        from harness.tasks import project_tasks
        replay = project_tasks(read_session(root / "sessions", session_id))
        checks["replay_preserves_evidence"] = replay.items[task_id].evidence == checked.evidence
        checks["record_replay"] = record.id in read_handoffs(kernel.session)
        report["passed"] = all(checks.values()) and len(checks) == 15
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    finally:
        if not kernel.session.closed:
            await close(kernel)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-file", type=Path, default=Path("/models/8b.gguf"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"suite": "external-handoff-public-v1", "timestamp": datetime.now(timezone.utc).isoformat(),
        "isolation": isolation(), "image": IMAGE, "source": "controlled Codex-compatible subprocess; real MCP",
        "destination": "Qwen3-8B Q4_K_M CUDA", "memory_plugin": "not exercised",
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in
            [*sorted((ROOT / "src/harness").glob("*.py")), Path(__file__).resolve()]},
        "weights_sha256": sha256(args.model_file), "passed": False}
    if report["weights_sha256"] != MODEL_PROFILES["qwen3-8b"]["sha256"]:
        raise ValueError("weights do not match the frozen 8B profile")
    report["hardware"] = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout.strip()
    report["runtime_version"] = subprocess.run(["/app/llama-server", "--version"], cwd="/app",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10).stdout.strip()
    models = catalog(args.model_file, "qwen3-8b")
    models = Catalog({**models.entries, "external": {"route": "codex/default", "backend": "codex"}})
    with tempfile.TemporaryDirectory(prefix="handoff-public-") as directory:
        report["journey"] = asyncio.run(journey(Path(directory), CatalogProvider(models)))
    report["passed"] = report["journey"]["passed"]
    report["cgroup_peak_bytes"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["journey"]))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
