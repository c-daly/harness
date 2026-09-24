"""A small real-model continuity trial, executed through Harness.

Creates only a new isolated output directory. Uses the selected configured
inference endpoint; it never starts a model runtime. All gates are fixed here,
before execution. This is a finite behavior check, not a long-term evaluation.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from harness.agent import AgentTask, TaskLimits
from harness.capture import capture_state
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.context import ContextPolicy
from harness.log import read_session
from harness.mcp_config import McpServerSpec
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId


PREFIX = "mcp__resident-memory__"
CORRECTION = ("Correction for this project: choose staging for the upcoming deployment, even though "
              "deployment.txt says production. Carry this decision into the next session. "
              "Acknowledge only, and do not modify files.")
TASK = ("Read deployment.txt, then write choice.txt containing just the deployment environment "
        "we should use. Use our current project decisions if they differ from the file's default.")


async def run(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "deployment.txt").write_text("Default deployment environment: production\n")
    vault = root / "vault"
    (vault / "10-projects/outing").mkdir(parents=True)
    os.environ["HARNESS_OUTING_VAULT"] = str(vault)
    spec = McpServerSpec(name="resident-memory", transport="stdio", command=sys.executable,
        args=(str(Path(__file__).with_name("server.py")), "--memory-plugin", str(args.memory_plugin),
              "--project", "outing"), env={"MEMORY_VAULT_DIR": "HARNESS_OUTING_VAULT"}, restart="never")
    catalog = Catalog.load(args.catalog)
    selected = dict(catalog.entries[args.model])
    selected.pop("local", None)  # Existing endpoint only; never start/stop a user's runtime.
    provider = CatalogProvider(Catalog(entries={args.model: selected}))
    if provider.execution_kind(ModelId(args.model)) != "inference":
        raise ValueError("outing requires an inference model")
    report = {"model": args.model, "criteria": {
        "seed_saved": "capture is saved with verified receipt",
        "seed_no_mutation": "correction session creates no choice.txt",
        "control": "fresh session without memory writes production",
        "continuity": "fresh session with normal project-memory source writes staging",
        "independent_sessions": "all three session IDs differ",
    }, "sessions": []}

    async def session(label, prompt, *, capture=False, memory=False):
        tools = ["read_file", "write_file"]
        if capture:
            tools += [PREFIX + "capture_prepare", PREFIX + "capture_write"]
        if memory:
            tools += [PREFIX + "memory_search", PREFIX + "memory_read"]
        policy = ContextPolicy.model_validate({"history_turns": 4, "max_input_bytes": 32768,
            "tools": tools, "parallel_tool_calls": False, "tool_recovery_attempts": 1,
            "response": {"max_output_tokens": 1024, "temperature": 0},
            "capture": dict(project="outing", workspace=str(workspace), model=args.model,
                prepare_tool=PREFIX + "capture_prepare", write_tool=PREFIX + "capture_write",
                max_output_tokens=512, timeout_seconds=60) if capture else None,
            "sources": [dict(id="project-memory", tool=PREFIX + "memory_search",
                args=dict(subject="outing", type="project", max_bytes=4096, limit=6), max_bytes=4096)] if memory else []})
        permissions = PermissionEngine([RuleSet(default="deny", rules=[
            *(PermissionRule("allow", tool) for tool in tools), PermissionRule("allow", f"model:{args.model}")])])
        kernel = build_kernel(base_dir=root / "state", provider=provider, model=ModelId(args.model),
            workspace_root=workspace, native_tools=True, context_policy=policy,
            mcp=[spec], permissions=permissions)
        started = time.monotonic()
        entry = dict(label=label, session_id=str(kernel.session.id))
        report["sessions"].append(entry)
        try:
            warnings = await kernel.mcp.start()
            if warnings:
                raise RuntimeError(str(warnings))
            await kernel.loop.start()
            kernel.mcp.flush_events()
            result = await kernel.loop.run_task(AgentTask(prompt=prompt,
                limits=TaskLimits(timeout_seconds=120, max_iterations=12)))
            entry.update(status=result.status, response=result.read_text(kernel.session.blobs))
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        except asyncio.CancelledError:
            entry["error"] = "trial interrupted by operator"
            report.update(interrupted=True, passed=False)
            raise
        finally:
            events = read_session(kernel.session.base, kernel.session.id, repair=False)
            requests, _, captures = capture_state(events)
            entry["captures"] = [captures[key].model_dump(mode="json") if key in captures else
                dict(capture_id=key, status="pending", reason="intent recorded; no capture attempted")
                for key in requests]
            entry["tools"] = [str(e.event.tool) for e in events if e.event.type == "tool_call_proposed"]
            entry["seconds"] = round(time.monotonic() - started, 3)
            choice = workspace / "choice.txt"
            entry["choice"] = choice.read_text().strip() if choice.exists() else None
            await kernel.mcp.stop()
            kernel.session.close()
            (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(entry), flush=True)
        return entry

    seed = await session("correction", CORRECTION, capture=True)
    control = await session("fresh-without-memory", TASK)
    (workspace / "choice.txt").unlink(missing_ok=True)
    continuity = await session("fresh-with-memory", TASK, memory=True)
    report["gates"] = dict(seed_saved=bool(seed["captures"]) and all(
        c["status"] == "saved" for c in seed["captures"]), seed_no_mutation=seed["choice"] is None,
        control=control["choice"] == "production" and control.get("status") == "completed",
        continuity=continuity["choice"] == "staging" and continuity.get("status") == "completed",
        independent_sessions=len({e["session_id"] for e in report["sessions"]}) == 3)
    report["passed"] = all(report["gates"].values())
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "gates": report["gates"]}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-plugin", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
