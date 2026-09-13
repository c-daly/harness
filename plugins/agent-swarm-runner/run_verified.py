"""Continue one plugin-selected task against explicit, independent host checks.

The installed plugin owns the queue. Harness executes and supervises one task;
passing these checks leaves release review and queue completion to the operator.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
from pathlib import Path
import signal

from harness.completion import CompletionCheck, CompletionPlan, CompletionService
from harness.completion_checks import CommandChecks, CommandVerifier
from harness.hooks import Allow, Block, ProposedModelCall
from harness.log import read_session
from harness.tasks import TaskRequirement
from harness.types import ModelId
from harness.usage_budget import project_usage

from run_one import build_native_kernel, command, plugin_command, save, worker_prompt


class PinnedRoute:
    def __init__(self, model):
        self.model = model

    async def __call__(self, action):
        if isinstance(action, ProposedModelCall) and str(action.model) != self.model:
            return Block("the workflow's model route is pinned")
        return Allow()


async def execute(args, request, *, resume=None):
    workspace = Path(request["worktree_dir"]).resolve(strict=True)
    branch = command(["git", "branch", "--show-current"], cwd=workspace).strip()
    if branch != request["branch_name"]:
        raise ValueError("selected worktree branch differs from plugin request")
    if resume is None and command(["git", "status", "--porcelain"], cwd=workspace):
        raise ValueError("initial worktree is dirty; reconcile preserved work before starting")
    specification = CommandChecks.model_validate_json(args.checks.read_text())
    kernel, resolved = build_native_kernel(
        args, workspace, max_children=args.max_attempts, resume=resume
    )
    kernel.hooks.register_dispatch("verified-worker-model", PinnedRoute(args.model))
    service = CompletionService(kernel.session)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    try:
        verifier = CommandVerifier(specification, workspace=workspace, blobs=kernel.session.blobs)
        configuration = {
            "request": request,
            "checks": specification.model_dump(mode="json"),
            "model": args.model,
            "route": str(resolved.route),
            "max_model_calls": args.max_model_calls,
            "max_attempts": args.max_attempts,
            "timeout": args.timeout,
            "worker_prompt": worker_prompt(request),
            "agent_definition": kernel.runner.agents["swarm-implementer"].model_dump(mode="json"),
            "context_policy": kernel.context_policy.model_dump(mode="json"),
        }
        plan = CompletionPlan(
            task=request["task_name"],
            objective=request["task_name"],
            checks=tuple(
                CompletionCheck(id=c.id, description=c.description) for c in specification.checks
            ),
            configuration=kernel.session.blobs.put(
                json.dumps(configuration, sort_keys=True).encode()
            ),
            max_attempts=args.max_attempts,
            timeout_seconds=args.timeout,
        )
        if resume is None:
            await kernel.loop.start()
            launch = {
                **configuration,
                "root_session_id": str(kernel.session.id),
                "workspace": str(workspace),
                "head_before": command(["git", "rev-parse", "HEAD"], cwd=workspace).strip(),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "queue_completion_recorded": False,
            }
            save(args.output / "launch.json", launch)
            plugin_command(args, "spawned", request["task_name"], f"harness:{kernel.session.id}")
        else:
            # Check host controls before making any new attempt. Dirty work from
            # the previous attempt is preserved and checked by the core service.
            if service.state().plan != plan:
                raise ValueError("recorded completion configuration differs from this invocation")
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)

        async def attempt(prompt):
            # A deterministic host uses the same native child runtime as
            # dispatch_agent, without spending a coordinator inference call.
            return await kernel.runner.run_result(
                prompt=prompt,
                model=ModelId(args.model),
                parent=kernel.session,
                agent="swarm-implementer",
                requirement_title=plan.objective,
                requirements=tuple(
                    TaskRequirement(id=c.id, description=c.description) for c in plan.checks
                ),
            )

        print(
            json.dumps(
                {
                    "task": plan.task,
                    "root_session_id": str(kernel.session.id),
                    "resume": bool(resume),
                }
            ),
            flush=True,
        )
        state = await service.run(
            plan,
            prompt=configuration["worker_prompt"],
            execute=attempt,
            verify=verifier,
            identify=verifier.identify,
            pause_after=args.pause_after,
        )
        return state
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        try:
            state = service.state()
            events = read_session(kernel.session.base, kernel.session.id, repair=False)
            usage = project_usage(events)
            report = {
                "root_session_id": str(kernel.session.id),
                "task": request["task_name"],
                "status": state.status,
                "reason": state.reason,
                "attempts": state.attempts,
                "pending_attempt": state.pending,
                "workspace_sha256": state.workspace_sha256,
                "observation": state.observation.model_dump(mode="json")
                if state.observation
                else None,
                "history": [e.model_dump(mode="json") for e in state.history],
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_usd": str(usage.cost_usd),
                    "unknown_input": usage.unknown_input,
                    "unknown_output": usage.unknown_output,
                    "unknown_cost": usage.unknown_cost,
                },
                "queue_completion_recorded": False,
                "review_state": "requires_scope_and_release_review",
                "git_status": command(["git", "status", "--porcelain"], cwd=workspace),
            }
            save(args.output / "result.json", report)
            print(
                json.dumps({k: report[k] for k in ("status", "reason", "attempts", "usage")}),
                flush=True,
            )
            await kernel.loop.end()
        finally:
            try:
                await kernel.resources.close(emit=kernel.session.append)
            finally:
                kernel.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plugin-root", "manifest", "state-dir", "output", "catalog", "checks"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-model-calls", type=int, default=160)
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--pause-after", type=int)
    parser.add_argument(
        "--resume", action="store_true", help="resume a recorded checkpoint in --output"
    )
    args = parser.parse_args()
    with (args.state_dir / ".harness-runner.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.resume:
            launch = json.loads((args.output / "launch.json").read_text())
            request, resume = launch["request"], launch["root_session_id"]
            status = json.loads(plugin_command(args, "status"))
            queued = status["tasks"][request["task_name"]]
            if queued["status"] != "spawned" or queued["worker_id"] != f"harness:{resume}":
                raise ValueError("plugin task ownership changed; inspect the queue before resuming")
        else:
            args.output.mkdir(parents=True, exist_ok=False)
            requests = json.loads(plugin_command(args, "pending", "--json"))
            save(args.output / "pending.json", requests)
            (args.output / "queue-before.json").write_text(plugin_command(args, "status"))
            if not requests:
                print("No eligible plugin tasks.")
                return
            request, resume = requests[0], None
        state = asyncio.run(execute(args, request, resume=resume))
        if state.status not in {"checks_passed", "paused"}:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
