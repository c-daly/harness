"""Offline supervised-improvement smoke with real local proposal/evaluation.

Two explicitly injected malformed responses seed the detector. All subsequent
inference uses pinned real weights. Public fixtures qualify loop mechanics,
not generalization or prompt quality. Gates are fixed before any local run.
Private context stays in temporary sessions; the report contains metadata only.
"""

import argparse
import asyncio
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.fold import fold
from harness.improvement import verdict
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.prompt_improvement import PromptExperiment, prompt_selection
from harness.provider import StreamStop, TextDelta, text_turn
from harness.semantic_evaluation import EvaluatorConfig, MessageEvaluationCase, MessageEvaluationSuite, REQUIRED_CASES
from harness.semantics import SemanticLimits
from harness.tui import HarnessApp
from harness.types import ModelId
from scripts.qualify_local import IMAGE, MODEL_PROFILES, catalog, isolation, sha256
from scripts.qualify_m3 import ROOT, WRITE, attempt, make_kernel, mounted, submit, write_record

SUITE = "m4-supervised-improvement-v1"
THRESHOLDS = {"proposal_seconds": 60, "project_seconds": 45}
FACTS = {"project": "harbor", "retry_limit": 3}
MODEL = ModelId("local-small")


def experiment():
    return PromptExperiment(configuration=EvaluatorConfig(model=MODEL, runtime_version="llama.cpp-b9603-qwen3-8b",
        limits=SemanticLimits(timeout_seconds=5)), suite=MessageEvaluationSuite(cases=(*REQUIRED_CASES,
            MessageEvaluationCase(id="public-question", partition="held_out", text="Which branch contains the fix?",
                                  expected="question"),
            MessageEvaluationCase(id="public-pause", partition="held_out", text="Pause here and wait for my reply.",
                                  expected="pause"))),
        min_improved_cases=1, max_latency_ratio=1.2, max_case_latency_ms=2000)


async def journey(root, model_file, memory_root):
    write_record(root, FACTS)
    models = catalog(model_file, "qwen3-8b")
    models.entries["local-small"]["tags"] = ["tools"]
    kernel = make_kernel(root, models, memory_root)
    app = HarnessApp(kernel, native_tools=True, workspace_root=root / "project")
    report = {"plugins": "normal-memory" if memory_root else "absent", "checks": {}, "passed": False}
    checks = report["checks"]
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            composer = await mounted(app, pilot, memory_root)
            await submit(composer, pilot, "/task new Extract the current project settings")
            await submit(composer, pilot, "/task require Review RESULT.json")
            report["project"] = await attempt(app, pilot, composer, root, WRITE, FACTS, write=True)
            checks["project_workflow"] = report["project"]["passed"]
            if not checks["project_workflow"]:
                return report
            incumbent = prompt_selection(kernel.session, kernel.provider, MODEL)[0]
            original = kernel.provider.infer

            async def malformed(request):
                for chunk in text_turn("injected malformed classifier output"):
                    yield chunk

            # Injection stays under real dispatch/accounting and is removed
            # before proposal generation or either arm of the paired evaluator.
            kernel.provider.infer = malformed
            try:
                observations = [await kernel.semantics.interpret(text, model=MODEL)
                                for text in ("injected format fault one", "injected format fault two")]
            finally:
                kernel.provider.infer = original
            checks["injected_failures_recorded"] = all(o.reason == "invalid_output" for o in observations)
            proposal_text, stops = [], []

            async def observed(request):
                source = original(request)
                try:
                    async for chunk in source:
                        if isinstance(chunk, TextDelta):
                            proposal_text.append(chunk.text)
                        elif isinstance(chunk, StreamStop):
                            stops.append(chunk.stop_reason)
                        yield chunk
                finally:
                    await source.aclose()

            started = time.monotonic()
            kernel.provider.infer = observed
            try:
                candidate = await kernel.improvement_service.propose(model=MODEL)
            finally:
                kernel.provider.infer = original
                text = "".join(proposal_text)
                report["proposal_response"] = {"characters": len(text), "stops": stops}
                try:
                    parsed = json.loads(text)
                    report["proposal_response"]["object"] = isinstance(parsed, dict)
                    if isinstance(parsed, dict):
                        report["proposal_response"]["keys"] = sorted(str(k)[:64] for k in parsed)[:16]
                        from harness.prompt_improvement import PromptProposal
                        from pydantic import ValidationError
                        try:
                            PromptProposal.model_validate(parsed)
                        except ValidationError as exc:
                            report["proposal_response"]["validation"] = [e["type"] for e in exc.errors()]
                except ValueError:
                    report["proposal_response"]["json"] = False
            report["proposal_seconds"] = round(time.monotonic() - started, 3)
            report["candidate_version"] = candidate.artifact.sha256
            checks["bounded_real_proposal"] = report["proposal_seconds"] <= THRESHOLDS["proposal_seconds"]
            checks["evidence_linked"] = len(candidate.evidence_ids) == 2
            result = await kernel.improvement_service.evaluate(candidate.id, experiment=experiment())
            decision = verdict(kernel.improvements.state.plans[result.plan_id], result)
            report["evaluation"] = {"verdict": decision, "completion": result.completion,
                "observations": [m.model_dump(mode="json") for m in result.observations],
                "evaluator_version": result.evaluator_version}
            checks["paired_run_completed"] = result.completion == "completed"
            checks["no_implicit_adoption"] = not read_improvements(root / "sessions", kernel.session.id).prompt_changes
            if decision == "passed":
                # Explicit test operator action, confined to this disposable
                # session. A public smoke pass is not a deployment recommendation.
                kernel.improvement_service.adopt(result.id, model=MODEL)
                checks["explicit_selection"] = prompt_selection(kernel.session, kernel.provider, MODEL)[0] == candidate.artifact
                kernel.improvement_service.rollback(model=MODEL)
                checks["exact_rollback"] = prompt_selection(kernel.session, kernel.provider, MODEL)[0] == incumbent
            else:
                try:
                    kernel.improvement_service.adopt(result.id, model=MODEL)
                except ValueError:
                    checks["held_candidate_refused"] = True
                else:
                    checks["held_candidate_refused"] = False
            checks["task_unaccepted"] = not kernel.tasks.selected().accepted
            state = fold(read_session(root / "sessions", kernel.session.id))
            checks["settled_records"] = not (state.open_intents or state.open_model_intents
                                             or state.open_agent_runs or state.open_evaluations)
        checks["owned_runtime_stopped"] = not kernel.resources._owned
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
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
        "experiment": experiment().model_dump(mode="json"), "isolation": bounds, "image": IMAGE, "weights": weights,
        "faults_injected": "two malformed classification responses per journey; not observed model defects",
        "inference": "real local Qwen3-8B proposal and paired evaluation", "quality_qualification": False,
        "suite_provenance": "public fixtures withheld from proposal input; not genuine held-out qualification",
        "memory_writes": 0, "journeys": [], "passed": False,
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in [
            *sorted((ROOT / "src/harness").glob("*.py")), Path(__file__).resolve(),
            ROOT / "scripts/qualify_m3.py", ROOT / "scripts/qualify_local.py"]}}

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    async def run():
        for memory in (None, args.memory_root):
            with tempfile.TemporaryDirectory(prefix="harness-improvement-") as directory:
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
