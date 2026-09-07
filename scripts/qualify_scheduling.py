"""Offline local scheduling gate with real inference and the final TUI compositor.

Two aliases load the same pinned 8B weights on different ports, in one declared
device group. A real work stream holds admission while the user queues, edits
and cancels. A second submission takes priority over queued work, replaces the
idle owned runtime and writes the project artifact exactly once. Temporary
sessions include normal memory but reports contain only public fixture metadata.
"""

import argparse
import asyncio
import copy
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.cli import build_kernel
from harness.fold import fold
from harness.inference import InferenceRequest
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.messages import Message
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider import TextDelta
from harness.provider_litellm import CatalogProvider
from harness.tui import HarnessApp
from harness.types import ModelId
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, events, isolation, sha256, until
from scripts.qualify_m3 import ROOT, WRITE, attempt, mounted, profile, screen, submit, write_record

SUITE = "m4-local-scheduling-v1"
THRESHOLDS = {"project_seconds": 45, "cold_stream_seconds": 35, "cancel_seconds": 2,
              "ui_mount_seconds": 3, "background_ms": 250}
FACTS = {"project": "harbor", "retry_limit": 3}


def make_kernel(root, model_file, memory_root):
    models = catalog(model_file, "qwen3-8b")
    models.entries["local-small"]["tags"] = ["tools"]
    worker = copy.deepcopy(models.entries["local-small"])
    worker["api_base"] = "http://127.0.0.1:18082/v1"
    command = list(worker["local"]["command"])
    command[command.index("--port") + 1] = "18082"
    worker["local"]["command"] = command
    models.entries["worker"] = worker
    specs = () if memory_root is None else (McpServerSpec(
        name="memory", transport="stdio", command=str(memory_root / ".venv/bin/python"),
        args=("-B", str(memory_root / "lib/server.py")), env={"MEMORY_VAULT_DIR": "MEMORY_VAULT_DIR"},
        tools_allow=("memory_list",), restart="never", tool_timeout_s=10),)
    permissions = PermissionEngine([RuleSet(rules=[
        PermissionRule("allow", "model:worker"), PermissionRule("allow", "model:local-small"),
        PermissionRule("allow", "read_file"), PermissionRule("allow", "write_file"),
        PermissionRule("allow", "mcp__memory__memory_list", {"subject": "harness"}),
    ], default="deny")])
    return build_kernel(base_dir=root / "sessions", provider=CatalogProvider(models), model=ModelId("local-small"),
        native_tools=True, workspace_root=root / "project", permissions=permissions, mcp=specs,
        context_policy=profile(memory_root))


async def exercise(app, pilot, composer, root, report):
    kernel, checks = app.kernel, report["checks"]
    dispatcher = kernel.loop.dispatcher
    streaming = asyncio.Event()
    pending = []

    async def inference(alias, prompt, *, tokens=16, observe=None):
        return await dispatcher.dispatch_inference(provider=kernel.loop.provider, request=InferenceRequest(
            model=ModelId(alias), messages=(Message.user_text(prompt),), purpose="work",
            timeout_seconds=45, max_output_tokens=tokens), on_chunk=observe)

    def observe(chunk):
        if isinstance(chunk, TextDelta) and chunk.text:
            streaming.set()

    try:
        start = time.monotonic()
        owner = asyncio.create_task(inference("worker",
            "List the integers from 1 to 10000, one per line. Do not summarize or skip any.",
            tokens=4096, observe=observe))
        pending.append(owner)
        await until(streaming.is_set, seconds=THRESHOLDS["cold_stream_seconds"])
        report["cold_stream_seconds"] = round(time.monotonic() - start, 3)
        checks["real_stream_held_admission"] = not owner.done()
        work = asyncio.create_task(inference("local-small", "Reply with OK."))
        pending.append(work)
        await until(lambda: kernel.resources.scheduler.activity("local")[1] == 1)
        await submit(composer, pilot, "/task new Extract the current project settings")
        await submit(composer, pilot, "/task require Review RESULT.json")
        task_id = kernel.tasks.selected().definition.id
        await submit(composer, pilot, WRITE)
        await until(lambda: kernel.resources.scheduler.activity("local")[1] == 2)
        composer.value = "unsent local scheduling draft"
        await pilot.pause(0.1)
        checks["queue_visible"] = "waiting for local group local" in screen(app)
        start = time.monotonic()
        observation = await kernel.semantics.interpret("Is it ready?", model=ModelId("local-small"))
        report["background_ms"] = round((time.monotonic() - start) * 1000, 3)
        checks["background_abstained"] = (observation.reason == "busy"
            and report["background_ms"] <= THRESHOLDS["background_ms"])
        start = time.monotonic()
        app.action_interrupt()
        await until(lambda: app.controller.active is None and not app._interrupting,
                    seconds=THRESHOLDS["cancel_seconds"])
        report["queue_cancel_seconds"] = round(time.monotonic() - start, 3)
        checks["draft_preserved"] = composer.value == "unsent local scheduling draft" and not composer.disabled
        checks["cancel_without_execution"] = (kernel.resources.scheduler.activity("local") == ("worker", 1)
            and not (root / "project/RESULT.json").exists())
        await submit(composer, pilot, "/queue resume")
        foreground = asyncio.create_task(attempt(app, pilot, composer, root, WRITE, FACTS, write=True))
        pending.append(foreground)
        await until(lambda: kernel.resources.scheduler.activity("local")[1] == 2)
        checks["no_preemption"] = not owner.done() and not work.done()
        start = time.monotonic()
        owner.cancel()
        try:
            await owner
        except asyncio.CancelledError:
            pass
        report["stream_cancel_seconds"] = round(time.monotonic() - start, 3)
        checks["stream_cancel_deadline"] = report["stream_cancel_seconds"] <= THRESHOLDS["cancel_seconds"]
        report["attempt"] = await foreground
        await work
        checks["write_journey"] = report["attempt"]["passed"]
        log = events(kernel, root / "sessions")
        admissions = [e.observation for e in log if e.type == "local_request_observed"]
        acquired = [o for o in admissions if o.status == "acquired"]
        checks["interactive_before_queued_work"] = [o.priority for o in acquired[:3]] == [
            "work", "interactive", "work"]
        active = set()
        checks["no_overlap"] = True
        for observation in admissions:
            if observation.status == "acquired":
                checks["no_overlap"] &= not active
                active.add(observation.request_id)
            elif observation.status in ("released", "cancelled"):
                active.discard(observation.request_id)
        checks["queue_settled"] = not active and kernel.resources.scheduler.activity("local") == (None, 0)
        actions = [(e.alias, e.action) for e in log if e.type == "local_runtime_requested"]
        checks["idle_owned_replacement"] = actions == [
            ("worker", "start"), ("worker", "stop"), ("local-small", "start")]
        checks["one_write"] = sum(e.type == "dispatch_resolved" and e.tool == "write_file" for e in log) == 1
        checks["task_and_criteria_preserved"] = (kernel.tasks.selected().definition.id == task_id
            and len(kernel.tasks.selected().unresolved) == 1 and not kernel.tasks.selected().accepted)
        state = fold(read_session(root / "sessions", kernel.session.id))
        checks["settled_replay"] = not (state.open_intents or state.open_model_intents or state.open_agent_runs)
        report["admissions"] = [{"alias": o.alias, "priority": o.priority, "status": o.status,
                                 "reason": o.reason, "wait_ms": round(o.wait_ms, 3)} for o in admissions]
        report["runtime_actions"] = actions
        report["model_reservations"] = dispatcher.scope.budget.model_calls
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def journey(root, model_file, memory_root):
    write_record(root, FACTS)
    kernel = make_kernel(root, model_file, memory_root)
    app = HarnessApp(kernel, native_tools=True, workspace_root=root / "project")
    report = {"plugins": "normal-memory" if memory_root else "absent", "checks": {}, "passed": False}
    started = time.monotonic()
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            composer = await mounted(app, pilot, memory_root)
            report["mount_seconds"] = round(time.monotonic() - started, 3)
            report["checks"]["mount_deadline"] = report["mount_seconds"] <= THRESHOLDS["ui_mount_seconds"]
            await exercise(app, pilot, composer, root, report)
        report["checks"]["owned_runtime_stopped"] = not kernel.resources._owned
        report["passed"] = all(report["checks"].values())
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        kernel.session.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-root", type=Path, required=True)
    parser.add_argument("--model-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bounds = isolation()
    weights = MODEL_PROFILES["qwen3-8b"]
    if args.model_file.stat().st_size != weights["bytes"] or sha256(args.model_file) != weights["sha256"]:
        parser.error("requires the pinned Qwen3-8B weights")
    report = {"suite": SUITE, "observed_at": datetime.now(timezone.utc).isoformat(), "thresholds": THRESHOLDS,
        "isolation": bounds, "image": IMAGE, "weights": weights, "journeys": [], "passed": False,
        "inference": "real local Qwen3-8B, two aliases of the same weights", "memory_writes": 0,
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            *sorted((ROOT / "src/harness").glob("*.py")), Path(__file__).resolve(),
            ROOT / "scripts/qualify_m3.py", ROOT / "scripts/qualify_local.py"]}}

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    async def run():
        for memory in (None, args.memory_root):
            with tempfile.TemporaryDirectory(prefix="harness-scheduling-") as directory:
                report["journeys"].append(await journey(Path(directory), args.model_file, memory))
            save()

    save()
    asyncio.run(run())
    report["passed"] = len(report["journeys"]) == 2 and all(r["passed"] for r in report["journeys"])
    report["cgroup_peak_bytes"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    save()
    print(json.dumps({"passed": report["passed"], "journeys": [r["passed"] for r in report["journeys"]]}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
