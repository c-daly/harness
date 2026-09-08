"""Offline public smoke of supervised assessment improvement, with genuine local responses.

Public inputs deliberately exercise known difficult cases; they are not naturally
collected failures or held-out quality evidence. No model outputs are injected.
"""

import argparse
import asyncio
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harness.assessment_evaluation import AssessmentPromptExperiment, CONTEXT_REQUIRED_CASES
from harness.cli import build_kernel
from harness.fold import fold
from harness.improvement import verdict
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.prompt_improvement import default_prompt, prompt_selection, repeated_failures
from harness.provider_litellm import CatalogProvider
from harness.types import ModelId
from scripts.qualify_assessment_evaluation import public_experiments
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, close, isolation, sha256

ROOT = Path(__file__).resolve().parents[1]
MODEL = ModelId("local-small")


async def assess(kernel, function):
    if function == "context_selection":
        return await kernel.semantics.select_context(CONTEXT_REQUIRED_CASES[1].input, model=MODEL)
    return await kernel.semantics.assess_progress(model=MODEL)


async def journey(kernel, spec):
    function = spec.suite.function
    row = {"function": function, "seeds": [], "checks": {}, "quality_qualification": False}
    checks = row["checks"]
    kernel.tasks.create("Public fixture: create an artifact, then ask the operator to inspect it")
    kernel.tasks.add_requirement({"id": "output", "description": "Produce the expected output",
        "check": {"kind": "output", "sha256": "a" * 64}})
    kernel.tasks.add_requirement({"id": "review", "description": "Operator reviews the result"})
    task_before = kernel.tasks.state()
    baseline = default_prompt(kernel.session, function=function)
    for _ in range(2):
        row["seeds"].append((await assess(kernel, function)).model_dump(mode="json"))
    count = len(repeated_failures(kernel.session, MODEL, baseline, function))
    row["invalid_failure_count"] = count
    calls_before = sum(e.event.type == "model_call_proposed" for e in read_session(kernel.session.base, kernel.session.id))
    if count < 2:
        try:
            await kernel.improvement_service.propose(model=MODEL, function=function)
        except ValueError:
            row["proposal"] = "held: insufficient live invalid-output evidence"
        calls_after = sum(e.event.type == "model_call_proposed" for e in read_session(kernel.session.base, kernel.session.id))
        checks["evidence_gate"] = row.get("proposal") is not None and calls_after == calls_before
    else:
        prior_candidates = set(read_improvements(kernel.session.base, kernel.session.id).candidates)
        proposal_after_seq = read_session(kernel.session.base, kernel.session.id)[-1].seq
        try:
            candidate = await kernel.improvement_service.propose(model=MODEL, function=function)
        except Exception as exc:
            from harness.errors import ProviderError
            if not isinstance(exc, (ProviderError, ValueError)):
                raise
            row["proposal"] = {"held": type(exc).__name__}
            if type(exc) is ValueError:
                row["proposal"]["reason"] = str(exc)[:256]  # Core control message, not provider response text.
                from harness.messages import Message
                completions = [e.event for e in read_session(kernel.session.base, kernel.session.id)
                    if e.seq > proposal_after_seq and e.event.type == "model_call_completed"
                    and e.event.purpose == "improvement:propose"]
                if completions:
                    proposed = json.loads(Message.model_validate(completions[-1].message).text()).get("prompt", {})
                    previous = json.loads(kernel.session.blobs.get(baseline))
                    row["proposal"]["shape"] = {
                        "function_matches": proposed.get("function") == function,
                        "instructions_nonblank": bool(proposed.get("instructions", "").strip()),
                        "instructions_changed": proposed.get("instructions", "").strip() != previous["instructions"].strip()}
            checks["rejected_proposal_inactive"] = prompt_selection(kernel.session, kernel.provider, MODEL,
                function=function)[0] == baseline
            checks["rejected_proposal_not_recorded"] = set(read_improvements(
                kernel.session.base, kernel.session.id).candidates) == prior_candidates
        else:
            row["proposal"] = {"candidate": candidate.model_dump(mode="json"),
                "prompt": json.loads(kernel.session.blobs.get(candidate.artifact))}
            result = await kernel.improvement_service.evaluate(candidate.id, experiment=spec)
            state = read_improvements(kernel.session.base, kernel.session.id)
            decision = verdict(state.plans[result.plan_id], result)
            row["evaluation"] = {"verdict": decision, "result": result.model_dump(mode="json"),
                "report": json.loads(kernel.session.blobs.get(result.artifact))}
            checks["evaluation_completed"] = result.completion == "completed"
            checks["proposal_inactive_until_operator_action"] = prompt_selection(
                kernel.session, kernel.provider, MODEL, function=function)[0] == baseline
            if decision == "passed":
                # Explicit driver action stands in for the operator, under the existing
                # shadow-only policy; it never grants automatic task-control authority.
                change = kernel.improvement_service.adopt(result.id, model=MODEL, function=function)
                live = await assess(kernel, function)
                checks["selected_prompt_used"] = live.prompt == candidate.artifact and live.selection_id == change.id
                row["selected_live_observation"] = live.model_dump(mode="json")
                kernel.improvement_service.rollback(model=MODEL, function=function)
                checks["exact_rollback"] = prompt_selection(kernel.session, kernel.provider, MODEL,
                    function=function)[0] == baseline
            else:
                try:
                    kernel.improvement_service.adopt(result.id, model=MODEL, function=function)
                except ValueError:
                    checks["failed_candidate_held"] = True
                else:
                    checks["failed_candidate_held"] = False
    checks["task_preserved"] = kernel.tasks.state() == task_before
    checks["builtin_restored_or_unchanged"] = prompt_selection(kernel.session, kernel.provider, MODEL,
        function=function)[0] == baseline
    state = fold(read_session(kernel.session.base, kernel.session.id))
    checks["settled"] = not (state.open_evaluations or state.open_model_intents or state.open_agent_runs)
    checks["no_transcript_actions"] = not state.messages
    row["mechanics_passed"] = all(checks.values())
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-file", type=Path, default=Path("/models/8b.gguf"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bounds = isolation()
    weights = MODEL_PROFILES["qwen3-8b"]
    if args.model_file.stat().st_size != weights["bytes"] or sha256(args.model_file) != weights["sha256"]:
        parser.error("requires the pinned provisioned Qwen3-8B weights")
    models = catalog(args.model_file, "qwen3-8b")
    specs = [AssessmentPromptExperiment.model_validate(e.model_dump(
        exclude={"candidate", "hypothesis", "expected_benefit"})) for e in public_experiments()]
    report = {"suite": "public-assessment-improvement-v1", "timestamp": datetime.now(timezone.utc).isoformat(),
        "quality_qualification": False, "held_out": False, "plugins": "absent", "image": IMAGE,
        "weights": weights, "isolation": bounds, "experiments": [e.model_dump(mode="json") for e in specs],
        "seed_provenance": "Real local responses to deliberately difficult public inputs; no injected outputs. "
                           "Repeated cases are not naturally collected failures or independent quality samples.",
        "journeys": [], "mechanics_passed": False, "catalog": models.entries,
        "runtime_version": subprocess.run(["/app/llama-server", "--version"], cwd="/app", stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=10).stdout.strip(),
        "hardware": subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout.strip(),
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [*sorted((ROOT / "src/harness").glob("*.py")),
            Path(__file__), ROOT / "scripts/qualify_local.py", ROOT / "scripts/qualify_assessment_evaluation.py"]}}

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    save()  # Freeze the operator's suite and gates before any proposal or assessment inference.

    async def run():
        with tempfile.TemporaryDirectory(prefix="harness-assessment-improvement-") as directory:
            kernel = build_kernel(base_dir=Path(directory), model=MODEL, provider=CatalogProvider(models),
                permissions=PermissionEngine([RuleSet(rules=[PermissionRule("allow", "model:local-small")], default="deny")]))
            try:
                await kernel.loop.start()
                async with kernel.resources.use(models.resolve(str(MODEL)), emit=kernel.session.append):
                    pass  # Separate cold loading from the fixed semantic-call deadlines.
                for spec in specs:
                    report["journeys"].append(await journey(kernel, spec))
                    save()
            finally:
                await close(kernel)
            report["owned_runtime_stopped"] = not kernel.resources._owned
    try:
        asyncio.run(run())
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    report["mechanics_passed"] = (len(report["journeys"]) == len(specs)
        and all(row["mechanics_passed"] for row in report["journeys"]) and "error_type" not in report
        and report.get("owned_runtime_stopped", False))
    report["cgroup_peak_bytes"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    save()
    print(json.dumps({"mechanics_passed": report["mechanics_passed"], "functions": [
        {"function": r["function"], "failures": r["invalid_failure_count"], "proposal": r["proposal"],
         "evaluation": r.get("evaluation", {}).get("verdict")} for r in report["journeys"]]}))
    raise SystemExit(0 if report["mechanics_passed"] else 1)


if __name__ == "__main__":
    main()
