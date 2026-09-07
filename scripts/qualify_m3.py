"""Opt-in, offline M3 gate using provisioned weights and the normal memory plugin.

Run inside the resource-bounded container documented in docs/local-assistant.md.
Every case is critical. Reports contain only metadata and public-fixture checks;
temporary sessions and retrieved private memory are removed after each journey.
"""

import argparse
import asyncio
import copy
import json
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from textual.widgets import Input

from harness.cli import build_kernel
from harness.context import ContextPolicy
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import TextDelta
from harness.provider_litellm import CatalogProvider
from harness.tui import HarnessApp
from harness.types import ModelId
from scripts.qualify_local import (
    IMAGE, MODEL_PROFILES, THRESHOLDS, artifact_metadata, catalog,
    events, isolation, settled, sha256, tool_outcomes, until,
)

ROOT = Path(__file__).resolve().parent.parent
SUITE = "m3-offline-project-v2"
# Frozen before qualification: two earlier regressions and a new project record.
CASES = (("harbor", 3), ("maple-ν", 23), ("cobalt", 7))
WRITE = ("Using the supplied project-record context, write RESULT.json containing only "
         "its project and retry_limit fields. Then briefly confirm the values.")
ANSWER = "Tell me the project name and retry limit from the supplied context. Do not use tools."


def profile(memory):
    return ContextPolicy.load(ROOT / "docs/examples/local-resident" /
                              ("memory.toml" if memory else "project.toml"))


def make_kernel(root, models, memory_root, resume=None):
    specs = () if memory_root is None else (McpServerSpec(
        name="memory", transport="stdio", command=str(memory_root / ".venv/bin/python"),
        args=("-B", str(memory_root / "lib/server.py")), env={"MEMORY_VAULT_DIR": "MEMORY_VAULT_DIR"},
        tools_allow=("memory_list",), restart="never", tool_timeout_s=10),)
    permissions = PermissionEngine([RuleSet(rules=[
        PermissionRule("allow", "model:local-small"), PermissionRule("allow", "read_file"),
        PermissionRule("allow", "write_file"),
        PermissionRule("allow", "mcp__memory__memory_list", {"subject": "harness"}),
    ], default="deny")])
    # Use the shipped profile and the CLI's default system prompt.
    return build_kernel(base_dir=root / "sessions", provider=CatalogProvider(models), model=ModelId("local-small"),
        native_tools=True, workspace_root=root / "project", permissions=permissions,
        resume_session_id=resume, mcp=specs, context_policy=None if resume else profile(memory_root))


def write_record(root, facts):
    (root / "project").mkdir(exist_ok=True)
    (root / "project/PROJECT.md").write_text(
        f"# Project record\n\nproject: {facts['project']}\nretry_limit: {facts['retry_limit']}\n",
        encoding="utf-8")


def screen(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


def contains_facts(text, facts):
    return bool(re.search(r"(?<![\w-])" + re.escape(facts["project"]) + r"(?![\w-])", text) and
                re.search(r"(?<![\w.+-])" + str(facts["retry_limit"]) + r"(?!\w|\.\d)", text))


def exact_artifact(value, facts):
    return isinstance(value, dict) and type(value.get("retry_limit")) is int and value == facts


async def mounted(app, pilot, memory_root):
    if memory_root:
        await until(lambda: type(app.screen).__name__ == "ServerChecklistScreen")
        await pilot.press("enter")
    await until(lambda: app._bus_pump_worker is not None)
    return app.query_one("#prompt", Input)


async def submit(composer, pilot, text):
    composer.value = text
    composer.post_message(Input.Submitted(composer, text))
    await pilot.pause(0.05)


async def attempt(app, pilot, composer, root, prompt, facts, *, write=False):
    kernel, base = app.kernel, root / "sessions"
    prior = sum(e.type == "agent_run_finished" for e in events(kernel, base))
    started = time.monotonic()
    await submit(composer, pilot, prompt)
    await until(lambda: sum(e.type == "agent_run_finished" for e in events(kernel, base)) > prior,
                seconds=THRESHOLDS["project_seconds"])
    await until(lambda: app.controller.active is None)
    await pilot.pause(0.1)
    elapsed = time.monotonic() - started
    log = events(kernel, base)
    result = [e.result for e in log if e.type == "agent_run_finished"][-1]
    sources = [e for e in log if e.type == "context_source_observed" and e.run_id == result.run_id]
    ready = [e for e in sources if e.status == "ready"]
    expected_sources = len(kernel.context_policy.sources)
    checks = {
        "completed": result.status == "completed",
        "deadline": elapsed <= THRESHOLDS["project_seconds"],
        "sources_ready_once": len(ready) == expected_sources and len(sources) == 2 * expected_sources,
        "answer_contains_facts": contains_facts(result.read_text(kernel.session.blobs), facts),
        "answer_visible": contains_facts(screen(app), facts),
        "settled": settled(kernel, base),
        "unaccepted": not kernel.tasks.selected().accepted,
    }
    memory = next((e for e in ready if e.source_id == "normal-memory"), None)
    if expected_sources == 2:
        checks["normal_memory_nonempty"] = bool(memory and memory.result and
            kernel.session.blobs.get(memory.result).decode().startswith("- type:"))
    else:
        checks["no_plugins"] = kernel.mcp is None and not any(
            e.type == "dispatch_resolved" and e.tool and str(e.tool).startswith("mcp__") for e in log)
    row = {"checks": checks, "seconds": round(elapsed, 3), "status": result.status,
           "sources": [{"id": e.source_id, "status": e.status, "bytes": e.byte_count,
                        "sha256": e.result.sha256 if e.result else None} for e in sources if e.status != "fetching"]}
    if write:
        try:
            artifact = json.loads((root / "project/RESULT.json").read_text())
        except (OSError, ValueError):
            artifact = None
        checks["artifact_exact"] = exact_artifact(artifact, facts)
        row["artifact"] = artifact_metadata(artifact, expected=facts)
        # Filter to this attempt, including real context calls but never private payloads.
        calls = {e.call_id for e in log if e.type == "tool_call_proposed" and e.agent_run_id == result.run_id}
        checks["native_write_succeeded"] = any(e.type == "tool_call_completed" and e.call_id in calls
            and not e.is_error and any(r.type == "dispatch_resolved" and r.call_id == e.call_id
                                     and r.tool == "write_file" for r in log) for e in log)
        row["tools"] = [{"tool": e["tool"], "is_error": e["is_error"]} for e in tool_outcomes(kernel, base)]
    await submit(composer, pilot, "/status")
    await pilot.pause(0.1)
    checks["status_visible"] = ("Context project-record: ready" in screen(app) and
        "Local runtimes:" in screen(app) and "local-small:" in screen(app) and "1/1 unresolved" in screen(app))
    if memory:
        checks["memory_status_visible"] = "Context normal-memory: ready" in screen(app)
    row["passed"] = all(checks.values())
    return row


async def runtime_metadata():
    # Server-reported configuration, separate from the tested tool capability.
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        response = await client.get("http://127.0.0.1:8080/props")
        response.raise_for_status()
        data = response.json()
    return {"context_window": data.get("default_generation_settings", {}).get("n_ctx"),
            "slots": data.get("total_slots"), "build_info": data.get("build_info")}


async def journey(root, models, memory_root, facts):
    write_record(root, facts)
    report = {"mode": "normal-memory" if memory_root else "no-plugins", "fixture": facts,
              "checks": {}, "attempts": [], "stage": "mount"}
    checks = report["checks"]
    session_id, task_id = None, None
    for resumed in (False, True):
        kernel = make_kernel(root, models, memory_root, session_id)
        kernel.loop.max_iterations = 6
        app = HarnessApp(kernel, native_tools=True, workspace_root=root / "project")
        label = "resumed" if resumed else "first"
        started = time.monotonic()
        previous_events = len(events(kernel, root / "sessions"))
        owned_processes = []
        try:
            async with app.run_test(size=(140, 45)) as pilot:
                composer = await mounted(app, pilot, memory_root)
                report[label + "_mount_seconds"] = round(time.monotonic() - started, 3)
                checks[label + "_mount_deadline"] = report[label + "_mount_seconds"] <= THRESHOLDS["ui_mount_seconds"]
                checks[label + "_composer_before_start"] = not composer.disabled and not kernel.resources._owned
                if not resumed:
                    await submit(composer, pilot, "/task new Extract the current project settings")
                    await submit(composer, pilot, "/task require Review RESULT.json")
                    task_id = kernel.tasks.selected().definition.id
                else:
                    checks["task_and_profile_survive_restart"] = (
                        kernel.tasks.selected().definition.id == task_id and kernel.context_policy == profile(memory_root))
                    checks["cancel_visible_after_restart"] = "execution: cancelled" in screen(app)
                    # Change the record while stopped. A correct resumed answer needs fresh retrieval.
                    facts = {"project": facts["project"] + "-next", "retry_limit": facts["retry_limit"] + 1}
                    write_record(root, facts)
                    report["resumed_fixture"] = facts
                    report["stage"] = "resumed-answer"
                    report["attempts"].append(await attempt(app, pilot, composer, root, ANSWER, facts))
                report["stage"] = label + "-write"
                report["attempts"].append(await attempt(app, pilot, composer, root, WRITE, facts, write=True))
                report[label + "_runtime"] = await runtime_metadata()
                checks[label + "_runtime_context"] = report[label + "_runtime"]["context_window"] == 8192
                from harness.log import read_session
                current_log = read_session(root / "sessions", kernel.session.id)[previous_events:]
                starts = [e.ts for e in current_log if e.event.type == "local_runtime_requested" and e.event.action == "start"]
                ready = [e.ts for e in current_log if e.event.type == "resource_observed" and
                         e.event.observation.status == "ready" and e.event.observation.ownership == "harness"]
                latency = ready[0] - starts[0] if starts and ready else None
                report[label + "_start_seconds"] = latency
                checks[label + "_owned_runtime"] = bool(kernel.resources._owned)
                owned_processes = [entry[0] for entry in kernel.resources._owned.values()]
                checks[label + "_cold_start_deadline"] = latency is not None and latency <= THRESHOLDS["cold_start_seconds"]
                if not resumed:
                    report["stage"] = "cancel-stream"
                    streamed = asyncio.Event()
                    previous = kernel.loop.on_chunk
                    def chunk(value):
                        previous(value)
                        if isinstance(value, TextDelta) and value.text:
                            streamed.set()
                    kernel.loop.on_chunk = chunk
                    await submit(composer, pilot, "Write 500 numbered sentences about a harbor. Do not use tools.")
                    await asyncio.wait_for(streamed.wait(), 30)
                    await submit(composer, pilot, "Pending follow-up; do not start after interruption.")
                    composer.value = "unsent draft"
                    started = time.monotonic()
                    app.action_interrupt()
                    await until(lambda: app.controller.active is None and not app._interrupting, seconds=2)
                    await pilot.pause(0.1)
                    report["cancel_seconds"] = round(time.monotonic() - started, 3)
                    checks["cancel_deadline"] = report["cancel_seconds"] <= THRESHOLDS["cancel_seconds"]
                    checks["real_stream_cancelled"] = kernel.tasks.selected().execution == "cancelled"
                    checks["draft_preserved"] = composer.value == "unsent draft" and "unsent draft" in screen(app)
                    checks["queue_paused"] = app.controller.paused and len(app.controller.pending) == 1 and "Queue paused" in screen(app)
                    checks["cancel_settled"] = settled(kernel, root / "sessions")
                checks[label + "_unaccepted"] = not kernel.tasks.selected().accepted
                session_id = kernel.session.id
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            checks["journey_completed"] = False
            break
        finally:
            kernel.session.close()
            checks[label + "_owned_process_stopped"] = (not kernel.resources._owned and
                bool(owned_processes) and all(p.returncode is not None for p in owned_processes))
    else:
        report["stage"] = "complete"
        checks["journey_completed"] = True
    report["passed"] = all(checks.values()) and all(a["passed"] for a in report["attempts"])
    return report


async def unavailable(root, models, memory_root, failure):
    write_record(root, {"project": "unavailable", "retry_limit": 1})
    models = copy.deepcopy(models)
    local = models.entries["local-small"]["local"]
    if failure == "missing-assets":
        local["required_files"] += ["/models/intentionally-missing.gguf"]
        expected = "missing_configuration"
    else:
        local["command"][local["command"].index("--model") + 1] = "/models/intentionally-missing.gguf"
        expected = "local runtime exited during startup"
    report = {"mode": "normal-memory" if memory_root else "no-plugins", "failure": failure,
              "checks": {}, "stage": "mount"}
    checks = report["checks"]
    kernel = make_kernel(root, models, memory_root)
    session_id = kernel.session.id
    app = HarnessApp(kernel, native_tools=True, workspace_root=root / "project")
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            composer = await mounted(app, pilot, memory_root)
            await submit(composer, pilot, "/task new Work while runtime unavailable")
            await submit(composer, pilot, "/queue pause")
            await submit(composer, pilot, "Tell me the project settings.")
            await submit(composer, pilot, "Queued follow-up")
            await submit(composer, pilot, "/queue resume")
            await until(lambda: app.controller.last_failed is not None and app.controller.active is None)
            composer.value = "draft while unavailable"
            await pilot.pause(0.1)
            checks["failure_and_queue_visible"] = expected in screen(app) and "Queue paused" in screen(app)
            checks["draft_editable"] = not composer.disabled and "draft while unavailable" in screen(app)
            checks["queue_retained"] = len(app.controller.pending) == 1
            checks["no_model_answer"] = not any(e.type == "model_call_completed" for e in events(kernel, root / "sessions"))
            checks["failed_task_recorded"] = kernel.tasks.selected().execution == "failed"
            checks["settled"] = settled(kernel, root / "sessions")
            await submit(composer, pilot, "/status")
            checks["records_accessible"] = "Work while runtime unavailable" in screen(app)
            queued = app.controller.pending[0].id
            await submit(composer, pilot, f"/queue edit {queued} Edited follow-up")
            checks["queue_edit_works"] = app.controller.pending[0].text == "Edited follow-up"
            await submit(composer, pilot, "/queue clear")
            checks["queue_clear_works"] = not app.controller.pending
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        checks["unavailable_journey_completed"] = False
    finally:
        kernel.session.close()
        checks["failed_process_stopped"] = not kernel.resources._owned
    kernel = make_kernel(root, models, memory_root, session_id)
    app = HarnessApp(kernel, native_tools=True, workspace_root=root / "project")
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            composer = await mounted(app, pilot, memory_root)
            checks["resume_without_runtime"] = not composer.disabled and kernel.tasks.selected().execution == "failed"
            await submit(composer, pilot, "/status")
            checks["saved_failure_visible"] = "execution: failed" in screen(app)
            checks["no_start_on_resume"] = not kernel.resources._owned
    except Exception as exc:
        report["resume_error_type"] = type(exc).__name__
        checks["resume_completed"] = False
    finally:
        kernel.session.close()
    report["passed"] = all(checks.values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, required=True)
    parser.add_argument("--model-profile", choices=MODEL_PROFILES, default="qwen3-4b-instruct")
    parser.add_argument("--model-file", type=Path, default=Path("/models/local.gguf"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bounds = isolation()
    weights = MODEL_PROFILES[args.model_profile]
    if args.model_file.stat().st_size != weights["bytes"] or sha256(args.model_file) != weights["sha256"]:
        parser.error("requires the pinned preinstalled weights for the selected profile")
    models = catalog(args.model_file, args.model_profile)
    report = {"suite": SUITE, "observed_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": THRESHOLDS, "isolation": bounds, "image": IMAGE, "weights": weights,
        "model_profile": args.model_profile,
        "model_catalog": models.entries,
        "profile_digests": {mode: profile(mode == "normal-memory").digest for mode in ("no-plugins", "normal-memory")},
        "memory_writes": 0, "automatic_fallback_qualified": False, "cpu_only_qualified": False,
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path)
            for path in [*sorted((ROOT / "src/harness").glob("*.py")), Path(__file__).resolve()]},
        "runtime_version": subprocess.run(["/app/llama-server", "--version"], cwd="/app", stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, timeout=10).stdout.strip(),
        "hardware": subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                     "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                    timeout=10).stdout.strip(),
        "peak_device_memory_mib": 0, "journeys": [], "unavailable": [], "passed": False}

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    async def monitor():
        while True:
            process = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), 5)
                if process.returncode == 0:
                    values = [int(value) for value in stdout.decode().split() if value.isdecimal()]
                    report["peak_device_memory_mib"] = max([report["peak_device_memory_mib"], *values])
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            await asyncio.sleep(1)

    async def cases():
        for memory_root in (None, args.memory_root):
            for name, limit in CASES:
                with tempfile.TemporaryDirectory(prefix="harness-m3-") as directory:
                    report["journeys"].append(await journey(Path(directory), models, memory_root,
                                                           {"project": name, "retry_limit": limit}))
                save()
            for failure in ("missing-assets", "startup-exit"):
                with tempfile.TemporaryDirectory(prefix="harness-m3-unavailable-") as directory:
                    report["unavailable"].append(await unavailable(Path(directory), models, memory_root, failure))
                save()

    async def run():
        monitoring = asyncio.create_task(monitor())
        try:
            async with asyncio.timeout(900):
                await cases()
        except Exception as exc:
            report["suite_error_type"] = type(exc).__name__
        finally:
            monitoring.cancel()
            try:
                await monitoring
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    report["cgroup_peak_bytes"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    report["passed"] = all(row["passed"] for row in report["journeys"] + report["unavailable"])
    report["passed"] &= (len(report["journeys"]) == 2 * len(CASES) and len(report["unavailable"]) == 4
                         and "suite_error_type" not in report)
    report["capabilities"] = {"tool_workflow": "passed" if report["passed"] else "unqualified",
                              "structured_output": "unknown", "context_tokens": 8192}
    save()
    print(json.dumps({"suite": SUITE, "passed": report["passed"],
                      "journeys": [r["passed"] for r in report["journeys"]],
                      "unavailable": [r["passed"] for r in report["unavailable"]]}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
