"""Opt-in live local/native-remote/Codex workflow, with a controlled tool wait.

Only the three participants use models. The operator driver and artifact oracle
are deterministic. Sessions and partial work stay in the exclusive output tree.
See docs/mixed-runtime-qualification.md for scope, invocation and limitations.
"""

import argparse
import asyncio
import hashlib
import json
import platform
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from harness.activity import waiting
from harness.agent import AgentOutput, TaskLimits, current_agent_run, execute_task
from harness.catalog import Catalog
from harness.cli import build_kernel
from harness.context import ContextPolicy, ResponsePolicy
from harness.coordination import load_report
from harness.events import AgentRunFinished, AgentRunStarted, DispatchResolved, ToolCallCompleted, UserMessage
from harness.execution import current_scope
from harness.frontmatter import AgentDef
from harness.log import read_session
from harness.mixture import Expert, run_strategy_result
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.portable import export_task, task_package
from harness.provider_codex import CodexProvider
from harness.provider_litellm import CatalogProvider
from harness.run_controls import cancel_run


ROOT = Path(__file__).resolve().parent.parent
ROLES = ("local", "remote", "external")
CASE = {"jobs": [
    {"id": "harbor", "status": "failed", "attempts": 1, "max_attempts": 4},
    {"id": "mesa", "status": "failed", "attempts": 2, "max_attempts": 3},
    {"id": "orchard", "status": "succeeded", "attempts": 1, "max_attempts": 5},
    {"id": "ridge", "status": "failed", "attempts": 3, "max_attempts": 3},
]}
EXPECTED = {"retryable": ["harbor", "mesa"], "remaining_attempts": 4}
PROMPT = (
    "Read CASE.json using read_file. A job is retryable only when it failed and "
    "attempts is less than max_attempts. Compute the alphabetically sorted IDs "
    "of retryable jobs and the sum of their remaining attempts. Write ONLY the "
    "JSON object with keys retryable (array of strings) and remaining_attempts "
    "(integer) to your assigned output file using write_file. Then return that "
    "same JSON object. Use only the provided file tools."
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def events(kernel, session_id=None):
    return read_session(kernel.session.base, session_id or kernel.session.id, repair=False)


def budget(kernel):
    return kernel.loop.dispatcher.scope.budget


def accounting(kernel):
    value = budget(kernel)
    usage = value.usage.state
    return {"model_calls": value.model_calls, "tool_calls": value.tool_calls, "children": value.children,
        "input_tokens_observed": usage.input_tokens, "output_tokens_observed": usage.output_tokens,
        "cost_usd_observed": str(usage.cost_usd), "unknown_input_attempts": usage.unknown_input,
        "unknown_output_attempts": usage.unknown_output, "unknown_cost_attempts": usage.unknown_cost,
        "pending_usage_attempts": len(usage.pending)}


def settled(kernel):
    value = budget(kernel)
    return not (value.busy or value.controls.snapshot() or value.activity.snapshot()
                or value.runs.snapshot(kernel.session.id) or value.coordinations.snapshot()
                or value.usage.state.pending)


class PausedRead:
    """Pause the first actual external file-tool call, after core permission checks."""

    def __init__(self, wrapped):
        self.wrapped, self.spec = wrapped, wrapped.spec
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.run_id = None

    async def __call__(self, args):
        active = current_agent_run.get()
        if active is not None and active.runtime == "codex" and self.run_id is None:
            self.run_id = active.run_id
            self.entered.set()
            with waiting("qualification fixture read release"):
                await self.release.wait()
        return await self.wrapped(args)


def make_kernel(root, provider, aliases, *, resume=None, seconds=180):
    project = root / "project"
    # Absolute and relative paths still pass through the core WorkspaceGuard.
    engine = PermissionEngine([RuleSet(rules=[
        *(PermissionRule("allow", f"model:{alias}") for alias in aliases.values()),
        PermissionRule("allow", "read_file"),
        PermissionRule("allow", "write_file", {"file_path": "*.json"}),
    ], default="deny")])
    kernel = build_kernel(base_dir=root / "sessions", model=aliases["local"], provider=provider,
        native_tools=True, workspace_root=project, permissions=engine, resume_session_id=resume,
        context_policy=None if resume else ContextPolicy(
            tools=("read_file", "write_file"), response=ResponsePolicy(max_output_tokens=4096)),
        execution_overrides={} if resume else {"max_model_calls": 24, "max_tool_calls": 32,
            "max_children": 8, "max_depth": 3, "max_active_children": 3, "max_active_coordinators": 1,
            "coordination_timeout_seconds": seconds, "task_timeout_seconds": seconds,
            "inference_timeout_seconds": min(seconds, 120)},
        pricing_for=lambda alias: provider.catalog.resolve(str(alias)).pricing_dict())
    kernel.runner.agents.update({role: AgentDef(name=role, description="Public workflow participant",
        tools=("read_file", "write_file"), body=(
            f"Your assigned output file is {role}.json. Read it before overwriting if it exists. "
            "Use only read_file and write_file through Harness, including when they have an MCP prefix. "
            "Do not use built-in shell, search, network or other tools. Do not change CASE.json or other outputs."
        )) for role in ROLES})
    return kernel


async def run_team(kernel, aliases, roles, seconds):
    """Fixed operator orchestration through core task/coordination contracts."""
    scope = kernel.loop.dispatcher.scope
    task = kernel.tasks.prepare(PROMPT).model_copy(update={"limits": TaskLimits(timeout_seconds=seconds)})

    async def execute():
        kernel.session.append(UserMessage(text=PROMPT))
        result = await run_strategy_result("ensemble", kernel.runner, kernel.session, PROMPT,
            [Expert(model=aliases[role], agent=role) for role in roles])
        return AgentOutput(result.text, "completed" if result.status == "completed" else "incomplete",
                           result.reason)

    token = current_scope.set(scope)
    try:
        return await execute_task(kernel.session, task, runtime="harness", model=None,
            execute=execute, activity=scope.budget.activity, run_budgets=scope.budget.runs,
            run_controls=scope.budget.controls,
            capabilities={"qualification_driver": "fixed operator orchestration; no planning model"})
    finally:
        current_scope.reset(token)


def artifact(root, role):
    path = root / "project" / f"{role}.json"
    if not path.is_file():
        return {"exists": False, "exact": False}
    data = path.read_bytes()
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        value = None
    # bool is not an integer answer, even though Python considers True == 1.
    exact = (isinstance(value, dict) and value == EXPECTED
             and type(value.get("remaining_attempts")) is int)
    return {"exists": True, "exact": exact, "bytes": len(data), "sha256": digest(data)}


def member_evidence(kernel, root, member):
    child = member.result.child_session_id
    log = events(kernel, child) if child else []
    resolved = {e.event.call_id: e.event for e in log if isinstance(e.event, DispatchResolved)}
    successful = [resolved[e.event.call_id] for e in log if isinstance(e.event, ToolCallCompleted)
                  and not e.event.is_error and e.event.call_id in resolved]
    starts = {e.event.run_id: e.event for e in log if isinstance(e.event, AgentRunStarted)}
    finishes = {e.event.result.run_id: e.event.result for e in log if isinstance(e.event, AgentRunFinished)}
    role = member.agent
    observed_artifact = artifact(root, role)

    def file_call(call, tool, filename):
        if call.tool != tool or not isinstance((call.args or {}).get("file_path"), str):
            return False
        path = Path(call.args["file_path"])
        return (root / "project" / path).resolve() == (root / "project" / filename).resolve()

    return {"role": role, "model": member.model, "session_id": child,
        "status": member.result.status, "reason": member.result.reason,
        "checks": [e.model_dump(mode="json") for e in member.evidence] if member.evidence is not None else None,
        "output_retained": member.result.output is not None,
        "fixture_read": any(file_call(c, "read_file", "CASE.json") for c in successful),
        "artifact_written": any(file_call(c, "write_file", f"{role}.json")
            and isinstance(c.args.get("content"), str)
            and digest(c.args["content"].encode()) == observed_artifact.get("sha256") for c in successful),
        "artifact": observed_artifact,
        "runs": [{"id": identity, "runtime": start.runtime, "parent_run_id": start.parent_run_id,
            "terminal": finishes[identity].status if identity in finishes else None}
            for identity, start in starts.items()]}


def observe_phase(kernel, root, result):
    terminal = next(e.event for e in reversed(events(kernel)) if e.event.type == "coordination_finished")
    report = load_report(kernel.session.blobs, terminal, kernel.session.id)
    return {"root_run_id": result.run_id, "root_status": result.status,
        "coordination_id": report.id, "status": report.result.status, "gate": report.gate,
        "check_requirements": [r.model_dump(mode="json") for r in report.requirements or ()],
        "acceptance": report.acceptance, "settled": settled(kernel), "accounting": accounting(kernel),
        "members": [member_evidence(kernel, root, member) for member in report.members]}


def completed_member(member, root_run):
    runs = member["runs"]
    parents = {run["id"]: run["parent_run_id"] for run in runs}

    def reaches_root(identity):
        visited = set()
        while identity in parents and identity not in visited:
            visited.add(identity)
            identity = parents[identity]
        return identity == root_run

    return (member["status"] == "completed" and member["fixture_read"] and member["artifact_written"]
        and member["artifact"]["exact"] and member["output_retained"] and bool(runs)
        and all(run["terminal"] == "completed" for run in runs)
        and len(parents) == len(runs) and all(reaches_root(run["id"]) for run in runs))


def assess(report):
    """Require both complete phases and every named obligation; empty reports fail."""
    phases = report.get("phases", [])
    checks = dict(report.get("checks", {}))
    checks["journey_finished"] = report.get("stage") == "finished" and "error_type" not in report
    checks["two_phases"] = len(phases) == 2
    if len(phases) == 2:
        first, second = phases
        members = {member["role"]: member for member in first["members"]}
        checks["all_three_participants"] = len(first["members"]) == 3 and set(members) == set(ROLES)
        checks["native_partial_work"] = all(role in members and completed_member(members[role], first["root_run_id"])
                                           for role in ("local", "remote"))
        external = members.get("external", {})
        checks["targeted_external_stop"] = (external.get("status") == "cancelled"
            and external.get("reason") == "operator_cancelled"
            and any(run["runtime"] == "codex" and run["terminal"] == "cancelled"
                    for run in external.get("runs", [])))
        checks["external_file_absent_at_stop"] = not external.get("artifact", {}).get("exists", True)
        checks["partial_coordination"] = first["status"] == first["root_status"] == "incomplete"
        checks["only_unfinished_member_retried"] = (len(second["members"]) == 1
            and second["members"][0]["role"] == "external")
        checks["external_completed"] = (checks["only_unfinished_member_retried"]
            and completed_member(second["members"][0], second["root_run_id"])
            and any(run["runtime"] == "codex" for run in second["members"][0]["runs"]))
        checks["resumed_review_hold"] = (second["status"] == second["root_status"] == "incomplete"
            and second["gate"] == "recorded_checks"
            and len(second["check_requirements"]) == 1
            and second["check_requirements"][0]["check"]["kind"] == "review"
            and len(second["members"]) == 1
            and len(second["members"][0].get("checks") or []) == 1
            and second["members"][0]["checks"][0]["requirement_id"] == second["check_requirements"][0]["id"]
            and second["members"][0]["checks"][0]["status"] == "unverified")
        checks["settled_and_unaccepted"] = all(p["settled"] and p["acceptance"] == "unverified" for p in phases)
    required = {"controlled_wait_observed", "cancel_intent_recorded", "restart_counts_exact",
        "restart_is_idle", "restart_task_exact", "restart_limits_exact", "restart_context_exact",
        "native_files_preserved", "fixture_unchanged", "export_retains_both_coordinations",
        "export_retains_stop", "task_not_accepted"}
    checks["all_obligations_observed"] = required <= checks.keys()
    report["checks"] = checks
    report["passed"] = all(checks.values())
    return report


async def journey(root, provider, aliases, *, seconds=180, report=None, save=lambda: None):
    report = report if report is not None else {}
    report.update(phases=[], checks={}, passed=False)
    project = root / "project"
    project.mkdir()
    case_bytes = (json.dumps(CASE, indent=2) + "\n").encode()
    (project / "CASE.json").write_bytes(case_bytes)
    kernel = None
    work = None
    gate = None
    try:
        kernel = make_kernel(root, provider, aliases, seconds=seconds)
        await kernel.loop.start()
        task_id = kernel.tasks.create("Assess retryable jobs with three different runtimes").id
        kernel.tasks.add_requirement({"id": "review", "description": "Operator reviews all three artifacts"})
        session_id = kernel.session.id
        report["session_id"] = session_id
        gate = PausedRead(kernel.registry.get("read_file"))
        kernel.registry.register(gate)
        report["stage"] = "mixed-attempt"
        save()
        work = asyncio.create_task(run_team(kernel, aliases, ROLES, seconds))
        entered = asyncio.create_task(gate.entered.wait())
        try:
            done, _ = await asyncio.wait((work, entered), return_when=asyncio.FIRST_COMPLETED)
            report["checks"]["controlled_wait_observed"] = entered in done and gate.entered.is_set()
            if gate.entered.is_set() and not work.done():
                report["activity_at_stop"] = [asdict(row) for row in budget(kernel).activity.snapshot()]
                started = time.monotonic()
                cancel_run(kernel, gate.run_id)
                report["cancelled_run_id"] = gate.run_id
                report["checks"]["cancel_intent_recorded"] = any(
                    e.event.type == "agent_run_cancel_requested" and e.event.run_id == gate.run_id
                    for e in events(kernel))
                # Measure the targeted runtime settling, separately from sibling completion.
                while any(row.run_id == gate.run_id for row in budget(kernel).controls.snapshot()):
                    await asyncio.sleep(.01)
                report["cancel_seconds"] = round(time.monotonic() - started, 3)
            result = await work
        finally:
            entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)
        report["phases"].append(observe_phase(kernel, root, result))
        before = accounting(kernel)
        limits, context = budget(kernel).limits, kernel.context_policy
        files = {role: artifact(root, role) for role in ("local", "remote")}
        # Inspect the fixture directory before starting a fresh external attempt.
        report["external_file_before_retry"] = artifact(root, "external")
        await kernel.resources.close(emit=kernel.session.append)
        await kernel.loop.end()
        kernel.session.close()
        kernel = None
        report["stage"] = "restart"
        save()
        kernel = make_kernel(root, provider, aliases, resume=session_id, seconds=seconds)
        checks = report["checks"]
        checks["restart_counts_exact"] = accounting(kernel) == before
        checks["restart_is_idle"] = settled(kernel)
        checks["restart_task_exact"] = kernel.tasks.selected().definition.id == task_id
        checks["restart_limits_exact"] = budget(kernel).limits == limits
        checks["restart_context_exact"] = kernel.context_policy == context
        report["stage"] = "external-retry"
        save()
        work = asyncio.create_task(run_team(kernel, aliases, ("external",), seconds))
        result = await work
        report["phases"].append(observe_phase(kernel, root, result))
        checks["native_files_preserved"] = all(artifact(root, role) == value for role, value in files.items())
        checks["fixture_unchanged"] = (project / "CASE.json").read_bytes() == case_bytes
        package, _ = task_package(root / "sessions", session_id)
        checks["export_retains_both_coordinations"] = (
            {c["id"] for c in package["coordination"]} == {p["coordination_id"] for p in report["phases"]})
        checks["export_retains_stop"] = any(
            c["run_id"] == report.get("cancelled_run_id") for c in package["cancellation_requests"])
        checks["task_not_accepted"] = not package["task"]["accepted"] and package["task"]["unresolved"] == ["review"]
        export_task(root / "sessions", session_id, root / "continuation.zip")
        report["stage"] = "finished"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        if work is not None and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        if gate is not None:
            gate.release.set()
        try:
            if kernel is not None:
                await kernel.resources.close(emit=kernel.session.append)
        except Exception as exc:
            report["error_type"] = type(exc).__name__
        finally:
            if kernel is not None:
                kernel.session.close()
            assess(report)
            save()
    return report


def validate_aliases(catalog, aliases):
    if len(set(aliases.values())) != 3:
        raise ValueError("select three distinct catalog aliases")
    local, remote, external = (catalog.resolve(aliases[role]) for role in ROLES)
    if local.local is None or local.execution_kind != "inference":
        raise ValueError("local alias requires a configured local inference profile")
    if remote.local is not None or remote.execution_kind != "inference":
        raise ValueError("remote alias requires a remote inference route")
    if external.backend != "codex":
        raise ValueError("external alias requires the typed Codex adapter")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    for role in ROLES:
        parser.add_argument(f"--{role}", required=True)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--seconds", type=int, choices=range(30, 601), default=180, metavar="30..600")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalog = Catalog.load(args.catalog)
    aliases = {role: getattr(args, role) for role in ROLES}
    validate_aliases(catalog, aliases)
    args.output.mkdir(parents=True, exist_ok=False)
    # Reports omit catalog startup commands, credentials, endpoint URLs and raw errors.
    report = {"observed_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
        "driver": "deterministic operator; live participants; controlled external read wait",
        "plugins": [], "seconds_per_phase": args.seconds, "routes": {role: {
            "alias": alias, "route": str(catalog.resolve(alias).route),
            "execution_kind": catalog.resolve(alias).execution_kind} for role, alias in aliases.items()},
        "source_sha256": {str(path.relative_to(ROOT)): digest(path.read_bytes())
            for path in [*sorted((ROOT / "src/harness").glob("*.py")), Path(__file__).resolve()]}}

    def save():
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: report[k] for k in ("stage", "passed", "error_type") if k in report}), flush=True)

    provider = CatalogProvider(catalog, codex=CodexProvider(binary=args.codex_binary))
    asyncio.run(journey(args.output.resolve(), provider, aliases, seconds=args.seconds, report=report, save=save))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
