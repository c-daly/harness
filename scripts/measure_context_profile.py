"""Measure one installed context-selection profile on frozen public cases.

Run only inside the bounded offline container. This is model/profile development
evidence, not a prompt-adoption path or fresh confirmation. Never overwrite a run.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.assessment_evaluation import AssessmentGrader
from harness.cli import build_kernel
from harness.fold import fold
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider_litellm import CatalogProvider
from harness.semantic_assessment import CONTEXT_PROMPT, function_version
from harness.semantics import ASSESSMENT_LIMITS, AssessmentObservation
from scripts.qualify_assessment_evaluation import public_experiments
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, close, isolation, sha256

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {"baseline-8b": ("qwen3-8b", "28"),
            "candidate-4b": ("qwen3-4b-instruct", "99")}
MAX_CASE_MS = 2000
MAX_LATENCY_RATIO = 1.2


def profile_catalog(profile, model_file):
    name, layers = PROFILES[profile]
    models = catalog(model_file, name)
    entry = models.entries["local-small"]
    entry["max_input_tokens"] = 4096
    entry["local"]["startup_seconds"] = 120
    command = entry["local"]["command"]
    # The Instruct profile has no sampler defaults; hold these equal to the 8B.
    if not MODEL_PROFILES[name]["runtime_args"]:
        command.extend(MODEL_PROFILES["qwen3-8b"]["runtime_args"])
    for flag, value in (("--n-gpu-layers", layers), ("--ctx-size", "4096"),
                         ("--presence-penalty", "0")):
        command[command.index(flag) + 1] = value
    return models


def compare(baseline, candidate):
    """Regrade a complete public pair; an average gain cannot conceal a regression."""
    suite = public_experiments()[0].suite
    expected_ids = [case.id for case in suite.cases]
    outputs = []
    for report, profile in ((baseline, "baseline-8b"), (candidate, "candidate-4b")):
        if (report.get("profile") != profile or report.get("suite") != suite.model_dump(mode="json")
                or report.get("prompt") != CONTEXT_PROMPT.model_dump(mode="json")
                or not report.get("mechanics_passed") or not report.get("weights_verified") or "error_type" in report
                or [row["id"] for row in report["cases"]] != expected_ids):
            raise ValueError("comparison requires complete, matching public profile reports")
        command = report["catalog"]["local-small"]["local"]["command"]
        model_file = Path(command[command.index("--model") + 1])
        weights = MODEL_PROFILES[PROFILES[profile][0]]
        if (report["catalog"] != profile_catalog(profile, model_file).entries
                or report["weights"] != {k: v for k, v in weights.items() if k != "runtime_args"}
                or report["image"] != IMAGE or report["source_sha256"] != baseline["source_sha256"]):
            raise ValueError("comparison requires the frozen profiles and identical executed sources")
        grader = AssessmentGrader(report["suite"])
        observations = [AssessmentObservation.model_validate(row["observation"]) for row in report["cases"]]
        if any(o.function_version != function_version("context_selection") or o.limits != ASSESSMENT_LIMITS
               for o in observations):
            raise ValueError("comparison requires the unchanged function and inference limits")
        for case, observed in zip(suite.cases, observations, strict=True):
            if (observed.input is None or observed.input.sha256 != hashlib.sha256(case.input.model_dump_json().encode()).hexdigest()
                    or observed.prompt.sha256 != hashlib.sha256(CONTEXT_PROMPT.model_dump_json().encode()).hexdigest()):
                raise ValueError("observation does not reference the frozen input and builtin prompt")
        outputs.append([(grader.matches(case, grader.output(o)), o.duration_ms)
                        for case, o in zip(suite.cases, observations, strict=True)])
    incumbent, proposed = outputs
    improved = [case.id for case, b, c in zip(suite.cases, incumbent, proposed, strict=True) if c[0] and not b[0]]
    regressions = [case.id for case, b, c in zip(suite.cases, incumbent, proposed, strict=True) if b[0] and not c[0]]
    total_baseline = sum(ms for _, ms in incumbent)
    ratio = sum(ms for _, ms in proposed) / total_baseline if total_baseline > 0 else None
    critical_failed = [case.id for case, (ok, _) in zip(suite.cases, proposed, strict=True) if case.critical and not ok]
    gates = {"all_cases_correct": all(ok for ok, _ in proposed), "critical_passed": not critical_failed,
             "no_regressions": not regressions, "improved": bool(improved),
             "case_latency": max(ms for _, ms in proposed) <= MAX_CASE_MS,
             "latency_ratio": ratio is not None and ratio <= MAX_LATENCY_RATIO}
    return {"baseline_correct": sum(ok for ok, _ in incumbent),
            "candidate_correct": sum(ok for ok, _ in proposed), "cases": len(suite.cases),
            "rules_correct": sum(grader.matches(case, grader.rules(case)) for case in suite.cases),
            "improved_ids": improved, "regressed_ids": regressions, "critical_failed_ids": critical_failed,
            "candidate_max_latency_ms": max(ms for _, ms in proposed), "latency_ratio": ratio,
            "gates": gates, "development_passed": all(gates.values()), "quality_qualification": False}


def save(output, report):
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


async def measure(models, output, report):
    suite = public_experiments()[0].suite
    grader = AssessmentGrader(report["suite"])
    kernel = build_kernel(base_dir=output / "state", model="local-small", provider=CatalogProvider(models),
        permissions=PermissionEngine([RuleSet(rules=[PermissionRule("allow", "model:local-small")],
                                             default="deny")]))
    try:
        await kernel.loop.start()
        report["session_id"] = str(kernel.session.id)
        before = kernel.tasks.state()
        started = time.monotonic()
        async with kernel.resources.use(models.resolve("local-small"), emit=kernel.session.append):
            pass
        report["startup_seconds"] = time.monotonic() - started
        report["warmup"] = (await kernel.semantics.select_context(
            suite.cases[0].input, model="local-small")).model_dump(mode="json")
        for case in suite.cases:
            observed = await kernel.semantics.select_context(case.input, model="local-small")
            report["cases"].append({"id": case.id, "correct": grader.matches(case, grader.output(observed)),
                "rules_correct": grader.matches(case, grader.rules(case)),
                "observation": observed.model_dump(mode="json")})
            save(output, report)
            print(case.id, report["cases"][-1]["correct"], observed.reason, round(observed.duration_ms, 1), flush=True)
        state = fold(read_session(kernel.session.base, kernel.session.id))
        report["checks"] = {"task_preserved": kernel.tasks.state() == before,
            "no_selection": not read_improvements(kernel.session.base, kernel.session.id).prompt_changes,
            "records_settled": not (state.open_model_intents or state.open_evaluations),
            "no_actions": not state.messages}
    finally:
        await close(kernel)
        report["owned_runtime_stopped"] = not kernel.resources._owned
        events = read_session(kernel.session.base, kernel.session.id)
        # Retain original envelopes except repetitive resource polling; the full log stays in state/.
        (output / "inference-events.jsonl").write_text("".join(
            e.model_dump_json() + "\n" for e in events if e.event.type != "resource_observed"))
        blobs = {}
        for item in [report.get("warmup"), *(row["observation"] for row in report["cases"])]:
            if item is None:
                continue
            observed = AssessmentObservation.model_validate(item)
            for ref in (observed.prompt, observed.input, observed.inference_input):
                if ref is not None:
                    blobs[ref.sha256] = kernel.session.blobs.get(ref).decode()
        (output / "referenced-inputs.json").write_text(json.dumps(blobs, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--model-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir()
    models = profile_catalog(args.profile, args.model_file)
    weights = MODEL_PROFILES[PROFILES[args.profile][0]]
    report = {"stage": "public_context_profile_development", "observed_at": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile, "quality_qualification": False, "held_out": False, "plugins": "absent",
        "image": IMAGE, "weights": {k: v for k, v in weights.items() if k != "runtime_args"},
        "catalog": models.entries, "prompt": CONTEXT_PROMPT.model_dump(mode="json"),
        "suite": public_experiments()[0].suite.model_dump(mode="json"),
        "limits": ASSESSMENT_LIMITS.model_dump(mode="json"), "cases": [], "checks": {},
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            *sorted((ROOT / "src/harness").glob("*.py")), Path(__file__),
            ROOT / "scripts/qualify_local.py", ROOT / "scripts/qualify_assessment_evaluation.py"]}}
    save(args.output, report)
    try:
        report["isolation"] = isolation()
        if args.model_file.stat().st_size != weights["bytes"] or sha256(args.model_file) != weights["sha256"]:
            raise ValueError("requires the pinned preinstalled weights")
        report["weights_verified"] = True
        report["runtime_version"] = subprocess.run(["/app/llama-server", "--version"], cwd="/app",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10, check=True).stdout.strip()
        report["hardware"] = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        save(args.output, report)
        asyncio.run(measure(models, args.output, report))
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        report["mechanics_passed"] = (bool(report["checks"]) and all(report["checks"].values())
            and report.get("owned_runtime_stopped", False) and "error_type" not in report)
        save(args.output, report)
    print(json.dumps({k: report[k] for k in ("profile", "mechanics_passed", "error_type") if k in report}), flush=True)
    raise SystemExit(0 if report["mechanics_passed"] else 1)


if __name__ == "__main__":
    main()
