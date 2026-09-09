"""Offline terminal handoff with normal memory loss and explicit resumed recovery.

The external process is controlled; destination inference and memory MCP are real
in the opt-in CLI. Export only metadata: temporary sessions may contain private
memory and are removed. This does not qualify a live subscription provider.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input

from harness.agent import TaskLimits
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.errors import ProviderError
from harness.fold import fold
from harness.handoff import ExactCall, HandoffSpec, Resolution, read_handoffs, snapshot
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider_codex import CodexProvider
from harness.provider_litellm import CatalogProvider
from harness.tasks import project_tasks
from harness.tui import HarnessApp
from scripts.qualify_handoff import controlled_source, empty_auth_home, process_stopped
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, close, isolation, sha256, until
from scripts.qualify_m3 import mounted, profile, screen

ROOT = Path(__file__).resolve().parents[1]
COMMON_CHECKS = frozenset({"controlled_source_failed", "source_process_stopped", "source_artifacts_exact",
    "unaccepted_before_restart", "source_ui_resources_stopped", "restart_preserves_task", "continuation_deadline",
    "continuation_completed", "remaining_artifact_exact", "earlier_artifacts_unchanged", "evidence_preserved",
    "review_still_required", "exactly_two_writes", "fresh_context_ready", "completion_visible",
    "memory_recovery_visible", "intents_settled", "runtime_stopped", "memory_host_stopped", "replay_preserves_evidence"})
MEMORY_CHECKS = frozenset({"memory_child_stopped", "draft_preserved", "context_failure_recorded", "failure_before_inference",
    "failure_preserves_artifacts", "failure_settled", "memory_failure_visible", "reconciliation_guidance_visible",
    "consumed_record_refused", "normal_memory_nonempty"})


def make_kernel(root, provider, memory_spec, session_id=None):
    rules = [PermissionRule("allow", tool) for tool in
             ("model:external", "model:local-small", "read_file", "write_file")]
    rules.append(PermissionRule("allow", "mcp__memory__memory_list", {"subject": "harness"}))
    return build_kernel(base_dir=root / "sessions", provider=provider,
        model="local-small" if session_id else "external", native_tools=True, workspace_root=root / "project",
        permissions=PermissionEngine([RuleSet(rules=rules, default="deny")]),
        mcp=(memory_spec,) if memory_spec else (), resume_session_id=session_id,
        context_policy=None if session_id else profile(memory_spec))


async def command(app, pilot, text, *, draft=None):
    composer = app.query_one("#prompt", Input)
    composer.value = text
    await pilot.press("enter")
    await pilot.pause(.05)
    if draft is not None:
        composer.value = draft
    worker = app._semantic_worker
    if text.startswith("/handoff ") and worker is not None:
        await asyncio.wait_for(worker.wait(), 60)
    await pilot.pause(.05)


async def finish_app(app):
    """Mirror run_tui's outer cleanup; run_test only calls on_unmount."""
    kernel = app.kernel
    connections = list(kernel.mcp.connections.values()) if kernel.mcp else []
    try:
        if kernel.mcp:
            await kernel.mcp.stop()
            kernel.mcp.flush_events()
    finally:
        if app._mcp_errlog is not None:
            app._mcp_errlog.close()
        kernel.session.close()
    return all(not conn.is_alive for conn in connections)


def specification(kernel, project):
    checkpoint = snapshot(kernel.session)
    return HandoffSpec(snapshot_sha256=checkpoint.digest, model="local-small", process_stopped=True,
        continuation="Write B.txt with exactly 'stage B' followed by a newline using the allowed write_file call. "
                     "A.txt and native.txt are complete. Then briefly confirm stage B.",
        resolutions=tuple(Resolution(effect_id=e.id, status="completed",
            note="Inspected A.txt and native.txt; the controlled source process has stopped.")
            for e in checkpoint.effects if e.state == "uncertain"),
        allowed_calls=(ExactCall(tool="write_file", args={"file_path": str(project / "B.txt"),
                                                       "content": "stage B\n"}),))


def run_events(kernel, run_id):
    return [e.event for e in read_session(kernel.session.base, kernel.session.id)
            if getattr(e.event, "run_id", None) == run_id]


async def record(app, pilot, root):
    path = root / "reconciliation.json"
    path.write_text(specification(app.kernel, root / "project").model_dump_json())
    before = set(read_handoffs(app.kernel.session))
    await command(app, pilot, f"/handoff record {path}")
    records = read_handoffs(app.kernel.session)
    created = set(records) - before
    if len(created) != 1:
        raise RuntimeError("terminal did not record exactly one reconciliation")
    return records[created.pop()]


async def journey(root, provider, memory_spec=None):
    project = root / "project"
    project.mkdir()
    (project / "PROJECT.md").write_text("Complete stages A and B, preserving completed artifacts.\n")
    provider.codex = CodexProvider(binary=str(controlled_source(root)))
    catalog_path = root / "models.toml"
    lines = []
    for name, entry in provider.catalog.entries.items():
        for suffix, table in [("", entry), *((f".{key}", value) for key, value in entry.items() if isinstance(value, dict))]:
            lines.append(f"[models.{name}{suffix}]")
            lines.extend(f"{key} = {json.dumps(value)}" for key, value in table.items() if not isinstance(value, dict))
    catalog_path.write_text("\n".join(lines) + "\n")
    kernel = make_kernel(root, provider, memory_spec)
    report = {"memory_enabled": memory_spec is not None, "fault": "owned_memory_process_stopped" if memory_spec else None,
              "checks": {}, "passed": False, "memory_payload_exported": False}
    checks = report["checks"]
    try:
        app = HarnessApp(kernel, native_tools=True, workspace_root=project, catalog_path=catalog_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await mounted(app, pilot, memory_spec)
            task_id = kernel.tasks.create("Complete stages A and B").id
            for letter in ("A", "B"):
                args = {"file_path": str(project / f"{letter}.txt"), "content": f"stage {letter}\n"}
                expected = f"Created {project / f'{letter}.txt'} (1 lines)."
                kernel.tasks.add_requirement({"id": letter, "description": f"Stage {letter} recorded write",
                    "check": {"kind": "tool_result", "tool": "write_file", "args": args,
                              "sha256": hashlib.sha256(expected.encode()).hexdigest()}})
            kernel.tasks.add_requirement({"id": "review", "description": "Operator inspects all effects"})
            task = kernel.tasks.prepare("Complete both stages, including native work.").model_copy(update={
                "limits": TaskLimits(max_iterations=8, timeout_seconds=45)})
            with patch("harness.provider_codex._scratch_codex_home", empty_auth_home):
                try:
                    await kernel.loop.run_task(task)
                except ProviderError:
                    checks["controlled_source_failed"] = True
            checks["source_process_stopped"] = process_stopped(root)
            checks["source_artifacts_exact"] = ((project / "A.txt").read_text() == "stage A\n"
                and (project / "native.txt").read_text() == "external native effect" and not (project / "B.txt").exists())
            await command(app, pilot, "/model local-small")
            await until(lambda: kernel.loop.model == "local-small", seconds=5)
            initial = await record(app, pilot, root)
            session_id = kernel.session.id
            if memory_spec:
                conn = kernel.mcp.connections["memory"]
                await conn.stop()  # Only the memory child owned by this isolated session.
                checks["memory_child_stopped"] = not conn.is_alive
                before = read_session(kernel.session.base, session_id)[-1].seq
                await command(app, pilot, f"/handoff run {initial.id}", draft="keep this recovery draft")
                checks["draft_preserved"] = app.query_one("#prompt", Input).value == "keep this recovery draft"
                failed = kernel.tasks.selected()
                checks["context_failure_recorded"] = failed.execution == "failed" and failed.handoff_id == initial.id
                later = [e.event for e in read_session(kernel.session.base, session_id) if e.seq > before]
                checks["failure_before_inference"] = not any(e.type == "model_call_started" for e in later)
                checks["failure_preserves_artifacts"] = not (project / "B.txt").exists()
                state = fold(read_session(kernel.session.base, session_id))
                checks["failure_settled"] = not (state.open_intents or state.open_agent_runs or state.open_model_intents)
                await command(app, pilot, "/status")
                visible = " ".join(screen(app).split())
                checks["memory_failure_visible"] = "Context normal-memory: unavailable" in visible
                checks["reconciliation_guidance_visible"] = ("Handoff failed:" in visible
                    and "record a new handoff before another attempt" in visible)
                count = len(read_session(kernel.session.base, session_id))
                await command(app, pilot, f"/handoff run {initial.id}")
                checks["consumed_record_refused"] = ("already attempted" in " ".join(screen(app).split())
                    and not any(e.event.type == "agent_run_started"
                                for e in read_session(kernel.session.base, session_id)[count:]))
            checks["unaccepted_before_restart"] = not kernel.tasks.selected().accepted
        # Reopen the same durable session with the same MCP declaration and policy.
        checks["source_ui_resources_stopped"] = await finish_app(app)
        kernel = make_kernel(root, provider, memory_spec, session_id)
        app = HarnessApp(kernel, native_tools=True, workspace_root=project, catalog_path=catalog_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await mounted(app, pilot, memory_spec)
            checks["restart_preserves_task"] = kernel.tasks.selected().definition.id == task_id
            continuation = await record(app, pilot, root) if memory_spec else initial
            started = time.monotonic()
            await command(app, pilot, f"/handoff run {continuation.id}")
            report["continuation_seconds"] = time.monotonic() - started
            checks["continuation_deadline"] = report["continuation_seconds"] <= 45
            completed = kernel.tasks.selected()
            checks["continuation_completed"] = completed.execution == "completed" and completed.handoff_id == continuation.id
            checks["remaining_artifact_exact"] = (project / "B.txt").read_text() == "stage B\n"
            checks["earlier_artifacts_unchanged"] = ((project / "A.txt").read_text() == "stage A\n"
                and (project / "native.txt").read_text() == "external native effect")
            checked = kernel.tasks.check()
            checks["evidence_preserved"] = (checked.evidence["A"].status == checked.evidence["B"].status == "passed"
                and checked.evidence["A"].source_seq < checked.run_started_seq < checked.evidence["B"].source_seq)
            checks["review_still_required"] = checked.unresolved == ("review",) and not checked.accepted
            log = read_session(kernel.session.base, session_id)
            resolved = {e.event.call_id: e.event for e in log if e.event.type == "dispatch_resolved"}
            checks["exactly_two_writes"] = sum(e.event.type == "tool_call_completed" and not e.event.is_error
                and resolved[e.event.call_id].tool == "write_file" for e in log) == 2
            sources = [e for e in run_events(kernel, completed.run_id) if e.type == "context_source_observed" and e.status != "fetching"]
            report["final_sources"] = [{"id": e.source_id, "status": e.status, "bytes": e.byte_count,
                "sha256": e.result.sha256 if e.result else None} for e in sources]
            checks["fresh_context_ready"] = len(sources) == (2 if memory_spec else 1) and all(e.status == "ready" for e in sources)
            if memory_spec:
                memory = next(e for e in sources if e.source_id == "normal-memory")
                checks["normal_memory_nonempty"] = kernel.session.blobs.get(memory.result).decode().startswith("- type:")
            else:
                checks["plugins_absent"] = kernel.mcp is None
            await command(app, pilot, "/status")
            visible = " ".join(screen(app).split())
            checks["completion_visible"] = "Handoff completed" in visible and "Context project-record: ready" in visible
            checks["memory_recovery_visible"] = not memory_spec or "Context normal-memory: ready" in visible
            state = fold(log)
            checks["intents_settled"] = not (state.open_intents or state.open_agent_runs or state.open_model_intents)
            report["session_id"], report["task_id"] = str(session_id), task_id
            report["event_count"] = len(log)
        checks["memory_host_stopped"] = await finish_app(app)
        checks["runtime_stopped"] = not kernel.resources._owned
        replay = project_tasks(read_session(root / "sessions", session_id))
        checks["replay_preserves_evidence"] = replay.items[task_id].evidence == checked.evidence
        expected = COMMON_CHECKS | (MEMORY_CHECKS if memory_spec else {"plugins_absent"})
        report["passed"] = set(checks) == expected and all(checks.values())
    except Exception as exc:
        report["error_type"] = type(exc).__name__  # Never export memory-derived prose or exception bodies.
    finally:
        if not kernel.session.closed:
            if kernel.loop._ended:
                await finish_app(app)
            else:
                await close(kernel)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, required=True)
    parser.add_argument("--model-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir()
    models = catalog(args.model_file, "qwen3-8b")
    models = Catalog({**models.entries, "external": {"route": "codex/default", "backend": "codex"}})
    weights = MODEL_PROFILES["qwen3-8b"]
    report = {"suite": "handoff-memory-recovery-v1", "observed_at": datetime.now(timezone.utc).isoformat(),
        "image": IMAGE, "weights": {k: v for k, v in weights.items() if k != "runtime_args"},
        "catalog": models.entries, "journeys": [], "passed": False,
        "context_policies": {"project": profile(None).model_dump(mode="json"),
                             "memory": profile(True).model_dump(mode="json")},
        "source": "controlled Codex-compatible process; real MCP; no subscription credentials",
        "destination": "real local Qwen3-8B", "memory": "installed normal memory plugin; readonly vault; no writes",
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            *sorted((ROOT / "src/harness").glob("*.py")), Path(__file__), ROOT / "scripts/qualify_handoff.py",
            ROOT / "scripts/qualify_local.py", ROOT / "scripts/qualify_m3.py",
            ROOT / "docs/examples/local-resident/project.toml", ROOT / "docs/examples/local-resident/memory.toml"]}}

    def save():
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    try:
        report["isolation"] = isolation()
        if args.model_file.stat().st_size != weights["bytes"] or sha256(args.model_file) != weights["sha256"]:
            raise ValueError("requires the pinned 8B weights")
        report["weights_verified"] = True
        report["memory_server_sha256"] = sha256(args.memory_root / "lib/server.py")
        report["runtime_version"] = subprocess.run(["/app/llama-server", "--version"], cwd="/app",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10, check=True).stdout.strip()
        report["hardware"] = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        memory = McpServerSpec(name="memory", transport="stdio", command=str(args.memory_root / ".venv/bin/python"),
            args=("-B", str(args.memory_root / "lib/server.py")), env={"MEMORY_VAULT_DIR": "MEMORY_VAULT_DIR"},
            tools_allow=("memory_list",), restart="never", tool_timeout_s=10)
        report["memory_spec"] = {"command": memory.command, "args": memory.args, "tools_allow": memory.tools_allow,
                                 "restart": memory.restart, "tool_timeout_s": memory.tool_timeout_s}
        save()

        async def run():
            for spec in (None, memory):
                with tempfile.TemporaryDirectory(prefix="harness-handoff-memory-") as directory:
                    result = await journey(Path(directory), CatalogProvider(models), spec)
                    report["journeys"].append(result)
                save()

        asyncio.run(run())
        report["passed"] = len(report["journeys"]) == 2 and all(r["passed"] for r in report["journeys"])
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        save()
    print(json.dumps({"passed": report["passed"], "journeys": [r["passed"] for r in report["journeys"]]}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
