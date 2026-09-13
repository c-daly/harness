"""Supervised integration driver, not the complete agent-swarm router port.

The installed plugin determines eligibility and builds the task prompt. Harness
owns the coordinator, native child, tools, permissions, budget, and journals.
One clean worktree per invocation; no automatic completion, retries, or merges.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from harness.catalog import Catalog
from harness.cli import build_kernel, run_once
from harness.context import ContextPolicy, ResponsePolicy
from harness.events import AgentRunFinished, SubagentFinished, SubagentSpawned
from harness.execution import ExecutionLimits, current_scope
from harness.hooks import Allow, Block, ProposedModelCall, Rewrite
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.plugins import load_plugins
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId
from harness.usage_budget import UsageLimits, project_usage


def command(argv, *, cwd=None, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def plugin_command(args, *parts):
    env = dict(os.environ, ORCHESTRATION_STATE_DIR=str(args.state_dir))
    return command(
        [
            sys.executable,
            str(args.plugin_root / "lib/orchestrator.py"),
            parts[0],
            str(args.manifest),
            *parts[1:],
        ],
        env=env,
    )


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class SingleWorker:
    """Pin the queued request and route; the coordinator cannot substitute work."""

    def __init__(self, prompt, model):
        self.prompt, self.model = prompt, model

    async def __call__(self, action):
        if isinstance(action, ProposedModelCall):
            return Allow() if str(action.model) == self.model else Block("route is pinned")
        scope = current_scope.get()
        if scope is not None and scope.depth > 0:
            return Allow()
        if str(action.tool) != "dispatch_agent":
            return Block("the coordinator may only dispatch the selected queue request")
        return Rewrite(
            type(action)(
                call_id=action.call_id,
                tool=action.tool,
                args={"agent": "swarm-implementer", "model": self.model, "prompt": self.prompt},
            )
        )


def build_native_kernel(args, workspace, *, max_children=None, resume=None):
    catalog = Catalog.load(args.catalog)
    resolved = catalog.resolve(args.model)
    if resolved.execution_kind != "inference":
        raise ValueError("this measurement requires native inference, not an external agent CLI")
    rules = RuleSet(
        rules=[
            PermissionRule("allow", tool=name)
            for name in (
                f"model:{args.model}",
                "dispatch_agent",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "bash",
            )
        ],
        default="deny",
    )
    context = ContextPolicy(
        history_turns=8,
        max_input_bytes=262144,
        tools=("dispatch_agent", "read_file", "write_file", "edit_file", "glob", "grep", "bash"),
        parallel_tool_calls=False,
        tool_recovery_attempts=1,
        response=ResponsePolicy(
            max_output_tokens=16384,
            instructions="Make one tool call per response. Continue through implementation and checks.",
        ),
    )
    # Verified runs have separate supervisor and worker controls. Legacy run_one
    # callers still use timeout for the worker. Neither omission means infinity.
    if hasattr(args, "worker_timeout"):
        task_timeout = args.worker_timeout
    else:
        task_timeout = getattr(args, "timeout", None)
    if task_timeout is None:
        task_timeout = ExecutionLimits().task_timeout_seconds
    kernel = build_kernel(
        provider=CatalogProvider(catalog),
        base_dir=args.output / "journal",
        model=ModelId(args.model),
        resume_session_id=resume,
        model_pinned=True,
        native_tools=True,
        workspace_root=workspace,
        permissions=PermissionEngine([rules]),
        plugins=load_plugins([Path(__file__).parent / "bundle"]),
        context_policy=context,
        pricing=resolved.pricing_dict() or None,
        pricing_for=lambda model: catalog.resolve(str(model)).pricing_dict(),
        execution_limits=ExecutionLimits(
            max_model_calls=args.max_model_calls,
            max_tool_calls=200,
            max_children=(
                max_children if max_children is not None else ExecutionLimits().max_children
            ),
            max_active_children=1,
            max_depth=1,
            task_timeout_seconds=task_timeout,
            inference_timeout_seconds=180,
        ),
        usage_limits=(
            UsageLimits(max_input_tokens=1000000, max_output_tokens=80000)
            if resolved.usage_accounting == "reported"
            else UsageLimits()
        ),
    )
    return kernel, resolved


def worker_prompt(request):
    return (
        request["prompt"]
        + "\n\nNative Harness supervisor instructions (take precedence):\n"
        + (
            "This is one bounded implementation attempt, not release authorization. "
            "Use Harness native tools in the supplied worktree. Do not run another agent CLI. "
            "Do not commit, push, merge, remove worktrees, install packages, or access the network. "
            "Preserve all partial work if a limit is reached; report what remains. "
            "Implement the task and run focused tests plus Ruff; full suite and PR publication "
            "will be verified separately. Use the already installed Python interpreter "
            f"{sys.executable}. For tests use env PYTHONPATH=src:. {sys.executable} -m pytest. "
            "No arbitrary minimum test count. Use simple bash commands without chaining or redirects."
        )
    )


async def execute(args, request):
    workspace = Path(request["worktree_dir"]).resolve(strict=True)
    branch = command(["git", "branch", "--show-current"], cwd=workspace).strip()
    if branch != request["branch_name"]:
        raise ValueError("selected worktree branch differs from plugin request")
    if command(["git", "status", "--porcelain"], cwd=workspace):
        raise ValueError("selected worktree is dirty; reconcile preserved work before retrying")
    kernel, resolved = build_native_kernel(args, workspace, max_children=1)
    prompt = worker_prompt(request)
    kernel.hooks.register_dispatch("agent-swarm-single-worker", SingleWorker(prompt, args.model))
    launch = {
        "task": request["task_name"],
        "workspace": str(workspace),
        "branch": branch,
        "head_before": command(["git", "rev-parse", "HEAD"], cwd=workspace).strip(),
        "root_session_id": str(kernel.session.id),
        "pid": os.getpid(),
        "model_alias": args.model,
        "model_route": str(resolved.route),
        "declared_usage_accounting": resolved.usage_accounting,
        "max_model_calls": args.max_model_calls,
        "context_output_token_cap": 16384,
        "child_task_limits": kernel.runner.agents["swarm-implementer"].limits.model_dump(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "subagent_runtime_sha256": hashlib.sha256(
            (Path(__file__).parents[2] / "src/harness/subagent.py").read_bytes()
        ).hexdigest(),
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "queue_policy": "installed agent-swarm ParallelOrchestrator.get_pending_tasks",
        "binding": "native manifest runner; Claude router hooks and memory not loaded",
    }
    save(args.output / "launch.json", launch)
    (args.output / "worker-prompt.txt").write_text(prompt)
    try:
        plugin_command(args, "spawned", request["task_name"], f"harness:{kernel.session.id}")
    except BaseException:
        kernel.session.close()
        raise
    print(json.dumps(launch), flush=True)
    error = None
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGINT, task.cancel)
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        result = await run_once(
            kernel,
            "Dispatch exactly one swarm-implementer child on the configured model now. "
            "The plugin supplies its complete queued request at dispatch. Wait for it and "
            "report its actual result. Do not perform the implementation yourself.",
        )
        (args.output / "coordinator-result.txt").write_text(result)
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        events = list(read_session(args.output / "journal", kernel.session.id, repair=False))
        children = [
            e.event.child_session_id for e in events if isinstance(e.event, SubagentSpawned)
        ]
        usage = project_usage(events)
        report = {
            **launch,
            "error": error,
            "queue_completion_recorded": False,
            "review_state": "requires_independent_verification",
            "children": children,
            "parent_terminals": [
                e.event.model_dump(mode="json")
                for e in events
                if isinstance(e.event, (AgentRunFinished, SubagentFinished))
            ],
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": str(usage.cost_usd),
                "unknown_input": usage.unknown_input,
                "unknown_output": usage.unknown_output,
                "unknown_cost": usage.unknown_cost,
            },
            "git_status": command(["git", "status", "--porcelain"], cwd=workspace),
        }
        save(args.output / "result.json", report)
        print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plugin-root", "manifest", "state-dir", "output", "catalog"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-model-calls", type=int, default=48)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument(
        "--inspect", action="store_true", help="save eligible requests without execution"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    # This lock coordinates this driver only. The previous host must be stopped
    # before handoff; the external plugin runner does not honor this lock.
    with (args.state_dir / ".harness-runner.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        requests = json.loads(plugin_command(args, "pending", "--json"))
        save(args.output / "pending.json", requests)
        (args.output / "queue-before.json").write_text(plugin_command(args, "status"))
        if args.inspect or not requests:
            print(json.dumps({"eligible_tasks": [r["task_name"] for r in requests]}))
            return
        asyncio.run(execute(args, requests[0]))


if __name__ == "__main__":
    main()
