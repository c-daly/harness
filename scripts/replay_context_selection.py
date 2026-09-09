"""Replay a public context-selection development candidate in an isolated runtime.

This does not create fresh confirmation evidence. Outputs are exclusive: an
existing attempt directory is never overwritten, including after setup failure.
"""

import argparse
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from harness.cli import build_kernel
from harness.fold import fold
from harness.improvement import verdict
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.prompt_improvement import default_prompt, prompt_selection
from harness.provider_litellm import CatalogProvider
from harness.semantic_assessment import AssessmentPrompt
from scripts.qualify_assessment_evaluation import public_experiments
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, close, isolation, sha256

ROOT = Path(__file__).resolve().parents[1]


async def replay(models, spec, output, report):
    model = spec.configuration.model
    kernel = build_kernel(base_dir=output / "state", model=model, provider=CatalogProvider(models),
        permissions=PermissionEngine([RuleSet(rules=[PermissionRule("allow", f"model:{model}")],
                                             default="deny")]))
    try:
        await kernel.loop.start()
        baseline = default_prompt(kernel.session, function="context_selection")
        before = kernel.tasks.state()
        async with kernel.resources.use(models.resolve(str(model)), emit=kernel.session.append):
            pass  # Cold loading is separate from the unchanged semantic deadline.
        report["seeds"] = [(await kernel.semantics.select_context(case.input, model=model)).model_dump(mode="json")
                           for case in spec.suite.cases[1:4]]
        result = await kernel.improvement_service.compare_assessment(spec)
        state = read_improvements(kernel.session.base, kernel.session.id)
        plan = state.plans[result.plan_id]
        report.update(verdict=verdict(plan, result), plan=plan.model_dump(mode="json"),
            result=result.model_dump(mode="json"), session_id=str(kernel.session.id),
            report=json.loads(kernel.session.blobs.get(result.artifact)))
        checks = report["checks"]
        checks["paired_run_completed"] = result.completion == "completed"
        checks["every_arm_measured"] = all(
            r.incumbent_passed is not None and r.candidate_passed is not None for r in result.observations)
        if report["verdict"] != "passed":
            try:
                kernel.improvement_service.adopt(result.id, model=model, function="context_selection")
            except ValueError:
                checks["failed_candidate_refused"] = True
            else:
                checks["failed_candidate_refused"] = False
        # Even a passing public replay does not authorize adopting the prompt.
        checks["builtin_retained"] = prompt_selection(kernel.session, kernel.provider, model,
            function="context_selection")[0] == baseline and not state.prompt_changes
        checks["task_preserved"] = kernel.tasks.state() == before
        folded = fold(read_session(kernel.session.base, kernel.session.id))
        checks["settled"] = not (folded.open_evaluations or folded.open_model_intents)
        checks["no_transcript_actions"] = not folded.messages
    finally:
        await close(kernel)
        report["owned_runtime_stopped"] = not kernel.resources._owned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New attempt directory")
    parser.add_argument("--model-file", type=Path, default=Path("/models/8b.gguf"))
    args = parser.parse_args()
    bounds = isolation()
    weights = MODEL_PROFILES["qwen3-8b"]
    if args.model_file.stat().st_size != weights["bytes"] or sha256(args.model_file) != weights["sha256"]:
        parser.error("requires the pinned preinstalled Qwen3-8B weights")
    prompt = AssessmentPrompt.model_validate_json(args.candidate.read_bytes())
    models = catalog(args.model_file, "qwen3-8b")
    entry = models.entries["local-small"]
    entry["max_input_tokens"] = 4096
    entry["local"]["startup_seconds"] = 120
    command = entry["local"]["command"]
    for flag, value in (("--n-gpu-layers", "28"), ("--ctx-size", "4096"), ("--presence-penalty", "0")):
        command[command.index(flag) + 1] = value
    spec = public_experiments()[0]
    spec = spec.model_copy(update={"candidate": prompt,
        "configuration": spec.configuration.model_copy(update={
            "runtime_version": "llama.cpp-b9603-qwen3-8b-gpu28-c4096-presence0"}),
        "hypothesis": "An ordered eligibility and ambiguity procedure corrects prior public selection failures.",
        "expected_benefit": "Correct unavailable-context and ambiguous-reference responses without losing semantic relevance."})
    # Revalidate model_copy and reject a prompt for another function before any work.
    spec = type(spec).model_validate(spec.model_dump())
    args.output.mkdir()
    report = {"stage": "public_development_replay", "held_out": False, "quality_qualification": False,
        "observed_at": datetime.now(timezone.utc).isoformat(), "image": IMAGE, "weights": weights,
        "isolation": bounds, "plugins": "absent", "checks": {},
        "provenance": "Operator-authored candidate, public cases including the held_out partition; no fresh confirmation.",
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            *sorted((ROOT / "src/harness").glob("*.py")), Path(__file__),
            ROOT / "scripts/qualify_local.py", ROOT / "scripts/qualify_assessment_evaluation.py"]}}

    def save():
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    (args.output / "frozen-experiment.json").write_text(spec.model_dump_json(indent=2) + "\n")
    (args.output / "catalog.json").write_text(json.dumps(models.entries, indent=2) + "\n")
    save()  # Preserve the exact prompt, cases, gates and runtime before starting it.
    try:
        report["runtime_version"] = subprocess.run(["/app/llama-server", "--version"], cwd="/app",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10, check=True).stdout.strip()
        report["hardware"] = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        asyncio.run(replay(models, spec, args.output, report))
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        report["cgroup_peak_bytes"] = int(Path("/sys/fs/cgroup/memory.peak").read_text())
        report["mechanics_passed"] = (bool(report["checks"]) and all(report["checks"].values())
            and report.get("owned_runtime_stopped", False) and "error_type" not in report)
        save()
    print(json.dumps({k: report[k] for k in ("stage", "verdict", "mechanics_passed", "error_type") if k in report}))
    raise SystemExit(0 if report["mechanics_passed"] else 1)


if __name__ == "__main__":
    main()
