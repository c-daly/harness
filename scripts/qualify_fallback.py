"""Fixed offline fallback journeys: real local inference, injected transport faults.

Requires the pinned M3 GPU container, weights and normal memory mount. The
primary is a loopback HTTP fault endpoint, not a real cloud outage. Four TUI
journeys exercise connection refusal and HTTP 401 with and without plugins.
No private memory or generated prose is exported; temporary sessions are erased.
"""

import argparse
import asyncio
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.cli import build_kernel
from harness.fallback import FallbackPolicy
from harness.fold import fold
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider_litellm import CatalogProvider
from harness.routing import RoutingRuleSet
from harness.tui import HarnessApp
from harness.types import ModelId
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, events, isolation, sha256, start_latency
from scripts.qualify_m3 import ROOT, WRITE, attempt, mounted, profile, screen, submit, write_record

SUITE = "m4-local-fallback-v1"
THRESHOLDS = {"project_seconds": 45, "cold_start_seconds": 30, "ui_mount_seconds": 3}
FACTS = {"project": "harbor", "retry_limit": 3}


async def unauthorized(reader, writer):
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        body = b'{"error":{"message":"injected expired credentials","type":"authentication_error"}}'
        writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nConnection: close\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def make_kernel(root, models, memory_root):
    specs = () if memory_root is None else (McpServerSpec(
        name="memory", transport="stdio", command=str(memory_root / ".venv/bin/python"),
        args=("-B", str(memory_root / "lib/server.py")), env={"MEMORY_VAULT_DIR": "MEMORY_VAULT_DIR"},
        tools_allow=("memory_list",), restart="never", tool_timeout_s=10),)
    permissions = PermissionEngine([RuleSet(rules=[
        PermissionRule("allow", "model:primary"), PermissionRule("allow", "model:local-small"),
        PermissionRule("allow", "read_file"), PermissionRule("allow", "write_file"),
        PermissionRule("allow", "mcp__memory__memory_list", {"subject": "harness"}),
    ], default="deny")])
    return build_kernel(base_dir=root / "sessions", provider=CatalogProvider(models), model=ModelId("primary"),
        native_tools=True, workspace_root=root / "project", permissions=permissions, mcp=specs,
        context_policy=profile(memory_root), routing_rules=RoutingRuleSet(
            default="primary", fallback=FallbackPolicy(models=("local-small",))))


async def journey(root, model_file, memory_root, fault):
    write_record(root, FACTS)
    models = catalog(model_file, "qwen3-8b")
    models.entries["local-small"]["tags"] = ["tools"]  # M3 qualified bounded fixture profile.
    models.entries["primary"] = {"route": "openai/fault", "api_base": "http://127.0.0.1:18081/v1"}
    server = await asyncio.start_server(unauthorized, "127.0.0.1", 18081) if fault == "authentication" else None
    kernel = make_kernel(root, models, memory_root)
    app = HarnessApp(kernel, native_tools=True, workspace_root=root / "project")
    report = {"fault": fault, "plugins": "normal-memory" if memory_root else "absent",
              "checks": {}, "passed": False}
    checks = report["checks"]
    started = time.monotonic()
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            composer = await mounted(app, pilot, memory_root)
            report["mount_seconds"] = round(time.monotonic() - started, 3)
            checks["mount_deadline"] = report["mount_seconds"] <= THRESHOLDS["ui_mount_seconds"]
            checks["input_before_runtime_start"] = not composer.disabled and not kernel.resources._owned
            await submit(composer, pilot, "/task new Extract the current project settings")
            await submit(composer, pilot, "/task require Review RESULT.json")
            task_id = kernel.tasks.selected().definition.id
            report["attempt"] = await attempt(app, pilot, composer, root, WRITE, FACTS, write=True)
            checks["write_journey"] = report["attempt"]["passed"]
            log = events(kernel, root / "sessions")
            state = fold(read_session(root / "sessions", kernel.session.id))
            decisions = state.fallback_decisions
            report["decisions"] = [d.model_dump(mode="json") for d in decisions]
            starts = [e for e in log if e.type == "agent_run_started"]
            checks["one_assignment"] = len(starts) == 1 and starts[0].task_id == task_id
            checks["criteria_preserved"] = len(kernel.tasks.selected().unresolved) == 1
            checks["recorded_switch"] = (len(decisions) == 1 and decisions[0].status == "selected"
                and decisions[0].reason == fault and decisions[0].task_id == task_id
                and decisions[0].run_id == starts[0].run_id and decisions[0].to_model == "local-small")
            checks["switch_visible_in_status"] = "Fallback selected: primary -> local-small" in screen(app)
            checks["only_local_completed"] = all(e.model == "local-small" for e in log if e.type == "model_call_completed")
            writes = [e for e in log if e.type == "dispatch_resolved" and e.tool == "write_file"]
            checks["one_write"] = len(writes) == 1
            report["model_reservations"] = kernel.loop.dispatcher.scope.budget.model_calls
            report["retry_events"] = sum(e.type == "retry_attempted" for e in log)
            checks["retries_accounted"] = report["retry_events"] == (3 if fault == "network" else 0)
            report["cold_start_seconds"] = start_latency(kernel, root / "sessions")
            checks["cold_start_deadline"] = (report["cold_start_seconds"] is not None
                and report["cold_start_seconds"] <= THRESHOLDS["cold_start_seconds"])
            checks["no_automatic_acceptance"] = not kernel.tasks.selected().accepted
        checks["owned_runtime_stopped"] = not kernel.resources._owned
        checks["settled_replay"] = not (state.open_intents or state.open_model_intents or state.open_agent_runs)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        if server:
            server.close()
            await server.wait_closed()
        kernel.session.close()
    report["passed"] = bool(checks) and all(checks.values()) and "error_type" not in report
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
        "faults_injected": True, "inference": "real local Qwen3-8B", "memory_writes": 0,
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            *sorted((ROOT / "src/harness").glob("*.py")), Path(__file__).resolve(),
            ROOT / "scripts/qualify_m3.py", ROOT / "scripts/qualify_local.py"]}}

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    async def run():
        for memory in (None, args.memory_root):
            for fault in ("network", "authentication"):
                with tempfile.TemporaryDirectory(prefix="harness-fallback-") as directory:
                    report["journeys"].append(await journey(Path(directory), args.model_file, memory, fault))
                save()

    save()
    asyncio.run(run())
    report["passed"] = len(report["journeys"]) == 4 and all(r["passed"] for r in report["journeys"])
    report["cgroup_peak_bytes"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    save()
    print(json.dumps({"passed": report["passed"], "journeys": [r["passed"] for r in report["journeys"]]}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
