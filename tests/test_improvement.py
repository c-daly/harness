"""Improvement gates use persisted evidence and paired measurements, not self-scores."""

import pytest

from harness.improvement import (
    AdoptionPolicy, Candidate, EvaluationCase, EvaluationPlan, Evidence, ExperimentResult,
    ImprovementState, Measurement, adoption_decision, verdict,
)
from harness.improvement_journal import ImprovementJournal
from harness.log import read_session
from harness.resume import resume_session
from harness.session import Session
from harness.types import SessionId


def records(session):
    candidate_artifact = session.blobs.put(b"candidate prompt")
    suite = session.blobs.put(b"fixed cases and expected outcomes")
    evidence = Evidence(id="e1", source_session=session.id, source_seq=1, category="failure",
                        observation="A prior outcome needs a correction")
    candidate = Candidate(id="c1", target="prompt", incumbent_version="a" * 64,
                          artifact=candidate_artifact, evidence_ids=(evidence.id,),
                          hypothesis="Handle the missing case", expected_benefit="One fewer failure")
    plan = EvaluationPlan(id="p1", candidate_id=candidate.id, incumbent_version="a" * 64,
                          candidate_version=candidate_artifact.sha256, suite=suite,
                          evaluator_version="b" * 64, max_case_latency_ms=100,
                          cases=(EvaluationCase(id="regression", partition="regression", critical=True),
                                 EvaluationCase(id="new", partition="held_out")))
    result = ExperimentResult(id="r1", plan_id=plan.id, incumbent_version="a" * 64,
                              candidate_version=candidate_artifact.sha256, evaluator_version="b" * 64,
                              artifact=session.blobs.put(b"trusted evaluator output"), observations=(
                                  Measurement(case_id="regression", incumbent_passed=True, candidate_passed=True,
                                              incumbent_latency_ms=10, candidate_latency_ms=10),
                                  Measurement(case_id="new", incumbent_passed=False, candidate_passed=True,
                                              incumbent_latency_ms=10, candidate_latency_ms=10)))
    return evidence, candidate, plan, result


def test_round_trip_without_plugins_and_replay_performs_no_experiments(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        evidence, candidate, plan, result = records(session)
        journal = ImprovementJournal(session)
        for record in (evidence, candidate, plan, result):
            journal.record(record)
        assert verdict(plan, result) == "passed"
        policy = AdoptionPolicy(version="c" * 64)
        assert adoption_decision(candidate, plan, result, policy) == "review_required"
        assert adoption_decision(candidate, plan, result, policy.model_copy(
            update={"automatic_targets": frozenset({"prompt"})})) == "eligible"
    resumed, _state = resume_session(tmp_path, SessionId("s"))
    try:
        replayed = ImprovementJournal(resumed)
        assert replayed.state.results == journal.state.results
        assert not _state
    finally:
        resumed.close()
    assert sum(e.event.type == "improvement_recorded" for e in read_session(tmp_path, SessionId("s"))) == 4


@pytest.mark.parametrize("failure", ["critical", "missing", "slow", "no_benefit"])
def test_failed_or_inconclusive_candidate_cannot_qualify_even_under_automatic_policy(tmp_path, failure):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        _, candidate, plan, result = records(session)
        first, second = result.observations
        if failure == "critical":
            first = first.model_copy(update={"candidate_passed": False})
        elif failure == "missing":
            second = second.model_copy(update={"candidate_latency_ms": None})
        elif failure == "slow":
            second = second.model_copy(update={"candidate_latency_ms": 101})
        elif failure == "no_benefit":
            second = second.model_copy(update={"candidate_passed": False})
        result = result.model_copy(update={"observations": (first, second)})
        assert adoption_decision(candidate, plan, result, AdoptionPolicy(
            version="c" * 64, automatic_targets={"prompt"})) == "refused"


def test_candidate_cannot_replace_plan_versions_or_omit_a_case(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        evidence, candidate, plan, result = records(session)
        state = ImprovementState()
        for record in (evidence, candidate, plan):
            state.apply(record)
        with pytest.raises(ValueError, match="immutable"):
            state.apply(plan.model_copy(update={"min_improved_cases": 0}))
        with pytest.raises(ValueError, match="fixed evaluation"):
            state.apply(result.model_copy(update={"evaluator_version": "d" * 64}))
        with pytest.raises(ValueError, match="each planned case"):
            state.apply(result.model_copy(update={"observations": result.observations[:1]}))
        assert not state.results


def test_missing_evidence_or_artifacts_never_persist(tmp_path):
    from harness.blobs import BlobRef, MissingBlobError
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        evidence, candidate, _, _ = records(session)
        journal = ImprovementJournal(session)
        with pytest.raises(ValueError, match="missing evidence"):
            journal.record(candidate)
        with pytest.raises(ValueError, match="source event"):
            journal.record(evidence.model_copy(update={"source_seq": 999}))
        journal.record(evidence)
        with pytest.raises(MissingBlobError):
            journal.record(candidate.model_copy(update={"artifact": BlobRef(sha256="f"*64, size=10)}))
        assert not journal.state.candidates
    assert sum(e.event.type == "improvement_recorded" for e in read_session(tmp_path, SessionId("s"))) == 1


def test_second_journal_cannot_overwrite_first_producers_plan(tmp_path):
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        first, second = ImprovementJournal(session), ImprovementJournal(session)
        evidence, candidate, plan, _ = records(session)
        for record in (evidence, candidate, plan):
            first.record(record)
        with pytest.raises(ValueError, match="immutable"):
            second.record(plan.model_copy(update={"min_improved_cases": 0}))


def test_headless_inspection_does_not_mutate_session(tmp_path, monkeypatch, capsys):
    from harness.cli import main
    with Session(tmp_path, SessionId("s")) as session:
        session.start()
        journal = ImprovementJournal(session)
        for record in records(session):
            journal.record(record)
    before = read_session(tmp_path, SessionId("s"))
    monkeypatch.setattr("sys.argv", ["harness", "improvements", "s", "--base-dir", str(tmp_path)])
    main()
    text = capsys.readouterr().out
    assert "1 candidates, 1 experiments" in text and "evaluation=passed" in text
    assert "do not activate" in text
    assert read_session(tmp_path, SessionId("s")) == before


async def test_improvement_status_visible_in_final_terminal_without_plugins(tmp_path):
    from textual.widgets import Input
    from tests.test_tui import make_app
    from tests.test_tui_queue import screen_text
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        for record in records(app.kernel.session):
            app.kernel.improvements.record(record)
        app.query_one("#prompt", Input).value = "/improvements"
        await pilot.press("enter")
        await pilot.pause(0.1)
        visible = screen_text(app)
        assert "1 candidates, 1 experiments" in visible
        assert "evaluation=passed" in visible and "do not activate" in visible
