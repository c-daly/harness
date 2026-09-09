"""Offline terminal handoffs through busy admission, destination loss and Esc.

The source is a controlled Codex-compatible process. The CLI uses real local
inference and optionally the installed normal memory plugin. Faults affect only
owned work. Export metadata, never private context or generated prose.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input

from harness.agent import TaskLimits
from harness.catalog import Catalog
from harness.errors import ProviderError
from harness.fold import fold
from harness.handoff import ExactCall, HandoffSpec, Resolution, read_handoffs, snapshot
from harness.inference import InferenceRequest
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.messages import Message
from harness.provider import TextDelta
from harness.provider_codex import CodexProvider
from harness.provider_litellm import CatalogProvider
from harness.tasks import project_tasks
from harness.tui import HarnessApp
from scripts.qualify_handoff import controlled_source, empty_auth_home, process_stopped
from scripts.qualify_handoff_memory import finish_app, make_kernel, run_events
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, close, isolation, sha256, until
from scripts.qualify_m3 import mounted, profile, screen

ROOT = Path(__file__).resolve().parents[1]
MODES = ("busy", "loss", "interrupt")
CHECKS = frozenset({"source_failed", "source_stopped", "source_artifacts_exact", "fault_observed",
    "draft_preserved", "recovery_guidance_visible", "record_use_correct", "partial_artifacts_exact",
    "fault_settled", "source_ui_stopped", "restart_preserves_task", "explicit_recovery", "recovery_deadline",
    "recovery_completed", "all_artifacts_exact", "exact_write_counts", "evidence_preserved",
    "review_still_required", "fresh_context_ready", "context_payload_valid", "completion_visible",
    "intents_settled", "admission_settled", "owned_processes_stopped", "replay_preserves_evidence"})
PARTIAL_CHECKS = frozenset({"completed_write_retained", "completed_target_refused", "stream_after_write",
                           "fault_deadline"})
BUSY_CHECKS = frozenset({"busy_stream_active", "preflight_did_not_start", "owner_not_preempted"})
LONG_REPLY = "Then list the integers from 1 to 10000, one per line, without skipping any."


def expected_checks(mode):
    return CHECKS | (BUSY_CHECKS if mode == "busy" else PARTIAL_CHECKS)


def log_events(kernel):
    return [e.event for e in read_session(kernel.session.base, kernel.session.id)]


def settled(kernel):
    state = fold(read_session(kernel.session.base, kernel.session.id))
    return not (state.open_intents or state.open_model_intents or state.open_agent_runs)


def write_args(project, letter):
    return {"file_path": str(project / f"{letter}.txt"), "content": f"stage {letter}\n"}


async def start_command(app, pilot, text, *, draft=None):
    composer = app.query_one("#prompt", Input)
    await pilot.click("#prompt")
    composer.value = text
    previous = app._semantic_worker
    await pilot.press("enter")
    # Input.Submitted clears the composer before processing the command. Wait
    # for that acknowledgement instead of overwriting an unhandled command.
    await until(lambda: composer.value == "", seconds=5)
    if draft is not None:
        composer.value = draft
    if text.startswith(("/handoff record ", "/handoff run ")):
        await until(lambda: app._semantic_worker is not previous, seconds=5)
        return app._semantic_worker


async def command(app, pilot, text, *, draft=None):
    worker = await start_command(app, pilot, text, draft=draft)
    if worker is not None:
        await asyncio.wait_for(worker.wait(), 60)
    await pilot.pause(.1)


def specification(kernel, project, letter, *, stream=False):
    checkpoint = snapshot(kernel.session)
    return HandoffSpec(snapshot_sha256=checkpoint.digest, model="local-small", process_stopped=True,
        continuation=f"Write {letter}.txt with exactly 'stage {letter}' followed by a newline using the allowed "
                     "write_file call. Earlier files are complete. " + (LONG_REPLY if stream else "Then briefly confirm."),
        resolutions=tuple(Resolution(effect_id=e.id, status="completed",
            note="Inspected all existing stage files and native.txt; the earlier work has stopped.")
            for e in checkpoint.effects if e.state == "uncertain"),
        allowed_calls=(ExactCall(tool="write_file", args=write_args(project, letter)),))


async def record(app, pilot, root, letter, *, stream=False):
    path = root / "reconciliation.json"
    path.write_text(specification(app.kernel, root / "project", letter, stream=stream).model_dump_json())
    before = set(read_handoffs(app.kernel.session))
    await command(app, pilot, f"/handoff record {path}")
    records = read_handoffs(app.kernel.session)
    created = set(records) - before
    if len(created) != 1:
        raise RuntimeError("terminal did not record exactly one reconciliation")
    return records[created.pop()]


async def journey(root, provider, mode, memory_spec=None):
    if mode not in MODES:
        raise ValueError("unknown destination fault")
    project = root / "project"
    project.mkdir()
    letters = ("A", "B") if mode == "busy" else ("A", "B", "C")
    (project / "PROJECT.md").write_text("Complete stages " + ", ".join(letters) + "; preserve completed artifacts.\n")
    provider.codex = CodexProvider(binary=str(controlled_source(root)))
    catalog_path = root / "models.toml"
    lines = []
    for name, entry in provider.catalog.entries.items():
        for suffix, table in [("", entry), *((f".{k}", v) for k, v in entry.items() if isinstance(v, dict))]:
            lines.append(f"[models.{name}{suffix}]")
            lines.extend(f"{k} = {json.dumps(v)}" for k, v in table.items() if not isinstance(v, dict))
    catalog_path.write_text("\n".join(lines) + "\n")
    kernel = make_kernel(root, provider, memory_spec)
    report = {"mode": mode, "memory_enabled": memory_spec is not None, "checks": {}, "passed": False,
              "memory_payload_exported": False}
    checks, owner, killed = report["checks"], None, []
    try:
        report["stage"] = "source"
        app = HarnessApp(kernel, native_tools=True, workspace_root=project, catalog_path=catalog_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await mounted(app, pilot, memory_spec)
            task_id = kernel.tasks.create("Complete stages " + ", ".join(letters)).id
            for letter in letters:
                expected = f"Created {project / f'{letter}.txt'} (1 lines)."
                kernel.tasks.add_requirement({"id": letter, "description": f"Stage {letter} recorded write",
                    "check": {"kind": "tool_result", "tool": "write_file", "args": write_args(project, letter),
                              "sha256": hashlib.sha256(expected.encode()).hexdigest()}})
            kernel.tasks.add_requirement({"id": "review", "description": "Operator inspects all effects"})
            task = kernel.tasks.prepare("Complete all stages, including native work.").model_copy(update={
                "limits": TaskLimits(max_iterations=8, timeout_seconds=45)})
            with patch("harness.provider_codex._scratch_codex_home", empty_auth_home):
                try:
                    await kernel.loop.run_task(task)
                except ProviderError:
                    checks["source_failed"] = True
            checks["source_stopped"] = process_stopped(root)
            checks["source_artifacts_exact"] = ((project / "A.txt").read_text() == "stage A\n"
                and (project / "native.txt").read_text() == "external native effect" and not (project / "B.txt").exists())
            report["stage"] = "destination_selection"
            await command(app, pilot, "/model local-small")
            await until(lambda: kernel.loop.model == "local-small", seconds=5)
            kernel.tasks.check()  # Include current evidence in the unused record's snapshot.
            report["stage"] = "initial_reconciliation"
            initial = await record(app, pilot, root, "B", stream=mode != "busy")
            streaming = asyncio.Event()
            if mode == "busy":
                report["stage"] = "busy_stream"
                def observe(chunk):
                    if isinstance(chunk, TextDelta) and chunk.text:
                        streaming.set()

                owner = asyncio.create_task(kernel.loop.dispatcher.dispatch_inference(provider=provider,
                    request=InferenceRequest(model="local-small", messages=(Message.user_text(
                        "List the integers from 1 to 10000, one per line. Do not skip any."),),
                        purpose="work", timeout_seconds=45, max_output_tokens=4096), on_chunk=observe))
                await until(streaming.is_set, seconds=35)
                checks["busy_stream_active"] = not owner.done() and kernel.resources.scheduler.activity("local") == ("local-small", 0)
                before = len(log_events(kernel))
                report["stage"] = "busy_preflight"
                await command(app, pilot, f"/handoff run {initial.id}", draft="keep this recovery draft")
                checks["fault_observed"] = "Handoff refused: handoff requires an idle session" in " ".join(screen(app).split())
                checks["preflight_did_not_start"] = not any(e.type == "agent_run_started" for e in log_events(kernel)[before:])
                # Completion can race the terminal redraw after refusal. Read
                # the actual outcome; done() alone is not proof of preemption.
                outcome = "active"
                if owner.done():
                    outcome = "cancelled" if owner.cancelled() else (type(owner.exception()).__name__
                        if owner.exception() is not None else "completed")
                report["busy_owner_outcome"] = outcome
                checks["owner_not_preempted"] = outcome in {"active", "completed"}
                checks["recovery_guidance_visible"] = "settle active or queued work first" in " ".join(screen(app).split())
                checks["record_use_correct"] = not any(e.type == "agent_run_started" and e.handoff_id == initial.id for e in log_events(kernel))
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
            else:
                report["stage"] = "post_write_stream"
                original = kernel.loop.on_chunk

                def observe(chunk):
                    original(chunk)
                    if isinstance(chunk, TextDelta) and chunk.text and (project / "B.txt").exists() and not streaming.is_set():
                        streaming.set()
                        if mode == "loss":
                            process = kernel.resources._owned["local-small"][0]
                            killed.append(process)
                            process.kill()  # The exact session-owned child; do not stop user servers.

                kernel.loop.on_chunk = observe
                await start_command(app, pilot, f"/handoff run {initial.id}", draft="keep this recovery draft")
                await until(streaming.is_set, seconds=40)
                checks["stream_after_write"] = (project / "B.txt").read_text() == "stage B\n"
                started = time.monotonic()
                report["stage"] = "fault_settlement"
                if mode == "interrupt":
                    await pilot.press("escape")
                else:
                    await asyncio.wait_for(killed[0].wait(), 2)
                await until(lambda: not kernel.handoffs._active, seconds=15 if mode == "loss" else 2)
                report["fault_seconds"] = time.monotonic() - started
                checks["fault_deadline"] = report["fault_seconds"] <= (15 if mode == "loss" else 2)
                await pilot.pause(.1)
                failed = kernel.tasks.selected()
                checks["fault_observed"] = failed.execution == ("failed" if mode == "loss" else "cancelled") and failed.handoff_id == initial.id
                visible = " ".join(screen(app).split())
                checks["recovery_guidance_visible"] = ("record a new handoff before another attempt" in visible if mode == "loss"
                    else "Handoff interrupted; reconcile its new effects before another attempt" in visible)
                checkpoint = snapshot(kernel.session)
                report["stage"] = "consumed_record_checks"
                checks["completed_write_retained"] = any(e.call and e.call.args == write_args(project, "B")
                    and e.state == "completed" for e in checkpoint.effects)
                try:
                    kernel.handoffs.record(specification(kernel, project, "B"))
                except ValueError as exc:
                    checks["completed_target_refused"] = "completed action" in str(exc)
                await command(app, pilot, f"/handoff run {initial.id}", draft="keep this recovery draft")
                checks["record_use_correct"] = "already attempted" in " ".join(screen(app).split())
                kernel.loop.on_chunk = original
            checks["draft_preserved"] = app.query_one("#prompt", Input).value == "keep this recovery draft"
            checks["partial_artifacts_exact"] = ((project / "A.txt").read_text() == "stage A\n"
                and (project / "native.txt").read_text() == "external native effect" and not (project / "C.txt").exists()
                and (not (project / "B.txt").exists() if mode == "busy" else (project / "B.txt").read_text() == "stage B\n"))
            checks["fault_settled"] = settled(kernel) and not kernel.tasks.selected().accepted
            before_evidence = kernel.tasks.check().evidence.copy()
            fault_calls = {e.call_id for e in log_events(kernel) if e.type == "model_call_proposed"
                           and e.agent_run_id == kernel.tasks.selected().run_id}
            report["fault_model_terminals"] = [{"type": e.type, "error_type": getattr(e, "error_type", None)}
                for e in log_events(kernel) if e.type in ("model_call_failed", "model_call_cancelled") and e.call_id in fault_calls]
            session_id = kernel.session.id
        report["stage"] = "restart"
        checks["source_ui_stopped"] = await finish_app(app) and not kernel.resources._owned
        kernel = make_kernel(root, provider, memory_spec, session_id)
        app = HarnessApp(kernel, native_tools=True, workspace_root=project, catalog_path=catalog_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await mounted(app, pilot, memory_spec)
            checks["restart_preserves_task"] = kernel.tasks.selected().definition.id == task_id
            continuation = initial if mode == "busy" else await record(app, pilot, root, "C")
            checks["explicit_recovery"] = (continuation.id == initial.id) == (mode == "busy")
            started = time.monotonic()
            report["stage"] = "recovery"
            await command(app, pilot, f"/handoff run {continuation.id}")
            report["recovery_seconds"] = time.monotonic() - started
            checks["recovery_deadline"] = report["recovery_seconds"] <= 45
            completed = kernel.tasks.selected()
            checks["recovery_completed"] = completed.execution == "completed" and completed.handoff_id == continuation.id
            checks["all_artifacts_exact"] = all((project / f"{letter}.txt").read_text() == f"stage {letter}\n" for letter in letters)
            checks["all_artifacts_exact"] &= (project / "native.txt").read_text() == "external native effect"
            checked = kernel.tasks.check()
            checks["evidence_preserved"] = (all(checked.evidence[letter].status == "passed" for letter in letters)
                and all(checked.evidence[letter] == before_evidence[letter] for letter in letters[:-1]))
            checks["review_still_required"] = checked.unresolved == ("review",) and not checked.accepted
            events = log_events(kernel)
            resolved = {e.call_id: e for e in events if e.type == "dispatch_resolved"}
            writes = [resolved[e.call_id].args for e in events if e.type == "tool_call_completed" and not e.is_error
                      and resolved[e.call_id].tool == "write_file"]
            checks["exact_write_counts"] = writes == [write_args(project, letter) for letter in letters]
            sources = [e for e in run_events(kernel, completed.run_id) if e.type == "context_source_observed" and e.status != "fetching"]
            report["final_sources"] = [{"id": e.source_id, "status": e.status, "bytes": e.byte_count,
                "sha256": e.result.sha256 if e.result else None} for e in sources]
            checks["fresh_context_ready"] = len(sources) == (2 if memory_spec else 1) and all(e.status == "ready" for e in sources)
            checks["context_payload_valid"] = (kernel.session.blobs.get(next(e for e in sources if e.source_id == "normal-memory").result)
                .decode().startswith("- type:") if memory_spec else kernel.mcp is None)
            await command(app, pilot, "/status")
            visible = " ".join(screen(app).split())
            checks["completion_visible"] = ("Handoff completed" in visible and "Context project-record: ready" in visible
                and (not memory_spec or "Context normal-memory: ready" in visible))
            checks["intents_settled"] = settled(kernel)
            checks["admission_settled"] = kernel.resources.scheduler.activity("local") == (None, 0)
            report["session_id"], report["task_id"], report["event_count"] = str(session_id), task_id, len(events)
        checks["owned_processes_stopped"] = await finish_app(app) and not kernel.resources._owned and all(p.returncode is not None for p in killed)
        replay = project_tasks(read_session(root / "sessions", session_id))
        checks["replay_preserves_evidence"] = replay.items[task_id].evidence == checked.evidence
        report["passed"] = set(checks) == expected_checks(mode) and all(checks.values())
        report["stage"] = "complete"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        try:
            report["failure_state"] = {"execution": getattr(kernel.tasks.selected(), "execution", None),
                "destination_selected": kernel.loop.model == "local-small", "settled": settled(kernel),
                "command_still_in_composer": app.query_one("#prompt", Input).value.startswith("/"),
                "prompt_focused": app.query_one("#prompt", Input).has_focus,
                "stage_files_present": [letter for letter in letters if (project / f"{letter}.txt").exists()]}
        except Exception as state_error:
            report["failure_state_error_type"] = type(state_error).__name__
    finally:
        if owner is not None and not owner.done():
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
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
    report = {"suite": "handoff-destination-recovery-v3", "observed_at": datetime.now(timezone.utc).isoformat(),
        "versions": {name: version(name) for name in ("litellm", "openai", "textual", "mcp")},
        "image": IMAGE, "weights": {k: v for k, v in weights.items() if k != "runtime_args"},
        "catalog": models.entries, "journeys": [], "passed": False,
        "context_policies": {"project": profile(None).model_dump(mode="json"), "memory": profile(True).model_dump(mode="json")},
        "source": "controlled Codex-compatible process; real MCP; no subscription credentials",
        "destination": "real local Qwen3-8B", "memory": "installed normal memory plugin; readonly vault; no writes",
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            *sorted((ROOT / "src/harness").glob("*.py")), Path(__file__), ROOT / "scripts/qualify_handoff_memory.py",
            ROOT / "scripts/qualify_handoff.py", ROOT / "scripts/qualify_local.py", ROOT / "scripts/qualify_m3.py",
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
        save()

        async def run():
            for spec in (None, memory):
                for mode in MODES:
                    with tempfile.TemporaryDirectory(prefix="harness-handoff-destination-") as directory:
                        report["journeys"].append(await journey(Path(directory), CatalogProvider(models), mode, spec))
                    save()

        asyncio.run(run())
        report["passed"] = len(report["journeys"]) == 6 and all(r["passed"] for r in report["journeys"])
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        save()
    print(json.dumps({"passed": report["passed"], "journeys": [r["passed"] for r in report["journeys"]]}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
