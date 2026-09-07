"""A failed or interrupted real-model experiment cannot qualify context activation."""

import asyncio
import json
from itertools import count
from types import SimpleNamespace

import pytest

from harness.improvement_journal import read_improvements
from scripts import evaluate_local_planning as experiment


@pytest.mark.parametrize("outcome", ["failed", "passed", "cancelled"])
@pytest.mark.parametrize("recovery", [False, True])
async def test_experiment_uses_core_gate_and_never_activates(tmp_path, monkeypatch, outcome, recovery):
    evidence = tmp_path / "prior.json"
    evidence.write_text('{"passed":false}')
    args = SimpleNamespace(audit_dir=tmp_path / "audit", evidence=evidence,
        output=tmp_path / "report.json", model_file=tmp_path / "model", memory_root=tmp_path / "memory",
        recovery=recovery)
    calls = []
    clock = count()
    monkeypatch.setattr(experiment, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    async def trial(root, models, memory_root, *, facts, parallel_tool_calls, tool_recovery_attempts=0):
        calls.append((facts, parallel_tool_calls, tool_recovery_attempts))
        if outcome == "cancelled":
            raise asyncio.CancelledError
        candidate = tool_recovery_attempts == 2 if recovery else parallel_tool_calls is False
        return {"passed": outcome == "passed" and candidate}

    monkeypatch.setattr(experiment, "project_case", trial)
    report = await experiment.evaluate(args, {"provider": "scripted-test"})
    assert report["automatic_activation"] is False
    assert report["verdict"] == {"failed": "failed", "passed": "passed", "cancelled": "inconclusive"}[outcome]
    assert report["adoption"] == ("review_required" if outcome == "passed" else "refused")
    assert json.loads(args.output.read_text())["result"]["observations"] == report["result"]["observations"]
    state = read_improvements(args.audit_dir, report["session_id"])
    assert len(state.evidence) == len(state.candidates) == len(state.plans) == len(state.results) == 1
    assert state.runs[report["run_id"]] == ("cancelled" if outcome == "cancelled" else "completed")
    if outcome == "cancelled":
        assert all(m["candidate_passed"] is None for m in report["result"]["observations"])
    else:
        assert len(calls) == 2 * len(experiment.CASES)
        if recovery:
            assert all(parallel is False for _, parallel, _ in calls)
            assert {attempts for _, _, attempts in calls} == {0, 2}
