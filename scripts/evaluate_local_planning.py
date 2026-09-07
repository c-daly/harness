"""Fixed, opt-in context-policy experiment using core improvement records.

Run inside the offline container from docs/local-model-qualification.md. Requires
the normal memory plugin. Audit logs contain metadata only; model sessions are
temporary. A successful evaluation remains review-required and activates nothing.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from harness.events import ErrorRaised, EvaluationRunFinished, EvaluationRunStarted
from harness.improvement import (AdoptionPolicy, Candidate, EvaluationCase, EvaluationPlan,
                                 Evidence, ExperimentResult, Measurement, adoption_decision, verdict)
from harness.improvement_journal import ImprovementJournal
from harness.log import read_session
from harness.session import Session
from harness.types import SessionId
from scripts.qualify_local import (FACTS, IMAGE, WEIGHTS_BYTES, WEIGHTS_SHA256, catalog, isolation,
                                   project_case, sha256)


# Original regression plus reserved fixtures; fixed before any candidate runs.
# Expected facts appear in the source file, never as answer hints in the prompt.
CASES = tuple({"id": f"{name}-{memory}-{repeat}", "memory": memory, "facts": facts,
               "partition": "regression" if name == "harbor" else "held_out", "critical": True}
              for name, facts in (("harbor", FACTS), ("orchard", {"project": "orchard-λ", "retry_limit": 17}))
              for memory in (False, True) for repeat in range(2))

# Fresh held-out source facts for the separately selected recovery experiment.
RECOVERY_CASES = tuple({"id": f"{name}-{memory}-{repeat}", "memory": memory, "facts": facts,
                        "partition": "regression" if name == "harbor" else "held_out", "critical": True}
                       for name, facts in (("harbor", FACTS), ("cobalt", {"project": "cobalt-μ", "retry_limit": 29}))
                       for memory in (False, True) for repeat in range(2))


def payload(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


async def evaluate(args, configuration):
    recovery = getattr(args, "recovery", False)
    cases = RECOVERY_CASES if recovery else CASES
    incumbent_policy = {"parallel_tool_calls": False, "tool_recovery_attempts": 0} if recovery else {
        "parallel_tool_calls": None}
    candidate_policy = {"parallel_tool_calls": False, "tool_recovery_attempts": 2} if recovery else {
        "parallel_tool_calls": False}
    session = Session(args.audit_dir, SessionId(uuid4().hex))
    session.start()
    journal = ImprovementJournal(session)
    imported = session.blobs.put(args.evidence.read_bytes())
    source = session.append(ErrorRaised(where="evaluation:local-artifact",
        message=f"Prior offline artifact check failed; evidence report SHA-256 {imported.sha256}"))
    journal.record(Evidence(id="local-artifact-failure", source_session=session.id,
        source_seq=source.seq, category="failure",
        observation=f"Imported failed local tool report {imported.sha256}."))
    incumbent = session.blobs.put(payload(incumbent_policy))
    candidate = Candidate(id="bounded-tool-recovery" if recovery else "single-tool-response", target="context",
        incumbent_version=incumbent.sha256,
        artifact=session.blobs.put(payload(candidate_policy)),
        evidence_ids=("local-artifact-failure",),
        hypothesis=("Bounded feedback after a fully rejected response allows grounded sequential tools."
                    if recovery else "One tool proposal per response prevents a write from sharing a source-read response."),
        expected_benefit="Correct file artifacts with normal memory while retaining no-plugin behavior.")
    journal.record(candidate)
    evaluator_version = hashlib.sha256(payload(configuration)).hexdigest()
    plan = EvaluationPlan(id="local-recovery-v1" if recovery else "local-planning-v1", candidate_id=candidate.id,
        incumbent_version=incumbent.sha256, candidate_version=candidate.artifact.sha256,
        suite=session.blobs.put(payload(cases)), evaluator_version=evaluator_version,
        cases=tuple(EvaluationCase(**{k: c[k] for k in ("id", "partition", "critical")}) for c in cases),
        min_improved_cases=2, max_latency_ratio=2, max_case_latency_ms=60000)
    journal.record(plan)
    run_id = uuid4().hex
    session.append(EvaluationRunStarted(run_id=run_id, plan_id=plan.id, incumbent=incumbent,
                                        configuration=session.blobs.put(payload(configuration))))
    rows = {case["id"]: {} for case in cases}
    status = "completed"
    try:
        # A whole-run deadline includes MCP setup, readiness, all grading and cleanup.
        async with asyncio.timeout(300):
            for index, case in enumerate(cases):
                # Alternate order to avoid giving one side every first/cold SDK call.
                sides = ("incumbent", "candidate") if index % 2 == 0 else ("candidate", "incumbent")
                for side in sides:
                    with tempfile.TemporaryDirectory(prefix="harness-planning-trial-") as temp:
                        started = time.monotonic()
                        row = await project_case(Path(temp), catalog(args.model_file),
                            args.memory_root if case["memory"] else None, facts=case["facts"],
                            **(candidate_policy if side == "candidate" else incumbent_policy))
                        row["elapsed_ms_including_checks"] = (time.monotonic() - started) * 1000
                        rows[case["id"]][side] = row
    except TimeoutError:
        status = "timed_out"
    except asyncio.CancelledError:
        status = "cancelled"
    except Exception:
        status = "failed"
    finally:
        measurements = []
        for case in cases:
            pair = rows[case["id"]]
            fields = {"case_id": case["id"]}
            for side in ("incumbent", "candidate"):
                row = pair.get(side)
                fields[f"{side}_passed"] = row["passed"] if row else None
                fields[f"{side}_latency_ms"] = row["elapsed_ms_including_checks"] if row else None
            measurements.append(Measurement(**fields))
        result = ExperimentResult(id=f"result-{run_id}", run_id=run_id, plan_id=plan.id,
            incumbent_version=incumbent.sha256, candidate_version=candidate.artifact.sha256,
            evaluator_version=evaluator_version, observations=tuple(measurements), completion=status,
            artifact=session.blobs.put(payload({"configuration": configuration, "rows": rows})))
        journal.record(result)
        session.append(EvaluationRunFinished(run_id=run_id, status=status, result_id=result.id))
        policy = AdoptionPolicy(version=hashlib.sha256(b"review-required-v1").hexdigest())
        report = {"schema_version": 1, "configuration": configuration, "cases": cases, "rows": rows,
            "session_id": str(session.id), "run_id": run_id, "completion": status,
            "plan": plan.model_dump(mode="json"), "result": result.model_dump(mode="json"),
            "verdict": verdict(plan, result), "adoption": adoption_decision(candidate, plan, result, policy),
            "automatic_activation": False, "full_m3_qualified": False,
            "journal_records": [e.event.record.model_dump(mode="json")
                for e in read_session(session.base, session.id) if e.event.type == "improvement_recorded"]}
        session.close()
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-file", type=Path, default=Path("/models/local.gguf"))
    parser.add_argument("--memory-root", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recovery", action="store_true", help="compare bounded correction with rejection alone")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.evidence is None:
        args.evidence = Path("docs/handoffs/2026-09-06-core-agency") / (
            "local-planning-evaluation.json" if args.recovery else "local-qualification.json")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    caps = isolation()
    if args.model_file.stat().st_size != WEIGHTS_BYTES or sha256(args.model_file) != WEIGHTS_SHA256:
        parser.error("model differs from the pinned profile")
    if not os.environ.get("MEMORY_VAULT_DIR"):
        parser.error("MEMORY_VAULT_DIR must select the read-only mounted normal vault")
    evidence = json.loads(args.evidence.read_text())
    if args.recovery:
        valid_evidence = (evidence.get("verdict") == "failed" and
            evidence.get("configuration", {}).get("weights_sha256") == WEIGHTS_SHA256)
    else:
        valid_evidence = evidence.get("passed") is False and evidence.get("provider") == "real-local-model"
    if not valid_evidence:
        parser.error("requires the prior failed real-model qualification report")
    repo = Path(__file__).resolve().parents[1]
    configuration = {"isolation": caps, "image": IMAGE, "weights_sha256": WEIGHTS_SHA256,
        "experiment": "bounded-tool-recovery" if args.recovery else "single-tool-response",
        "catalog": catalog(args.model_file).entries, "deadline_seconds": 300,
        "scripts": {p.name: sha256(p) for p in (Path(__file__), repo / "scripts/qualify_local.py")},
        "core_source": {str(p.relative_to(repo)): sha256(p) for p in sorted((repo / "src/harness").glob("*.py"))},
        "dependencies": {name: version(name) for name in ("litellm", "mcp", "httpx", "pydantic")},
        "runtime_version": subprocess.check_output(["/app/llama-server", "--version"], cwd="/app",
            stderr=subprocess.STDOUT, text=True, timeout=10).strip(),
        "memory_server_sha256": sha256(args.memory_root / "lib/server.py")}
    report = asyncio.run(evaluate(args, configuration))
    print(json.dumps({k: report[k] for k in ("session_id", "completion", "verdict", "adoption")}, indent=2))
    raise SystemExit(0 if report["verdict"] == "passed" else 1)


if __name__ == "__main__":
    main()
