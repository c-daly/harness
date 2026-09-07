"""Public regression gates cannot hide a critical error behind average accuracy."""

from scripts.qualify_semantics import GATES, summarize


def row(**updates):
    return {"function": "progress_assessment", "critical": True, "correct": True,
            "baseline_correct": True, "duration_ms": 100, **updates}


def test_critical_failure_blocks_otherwise_high_accuracy():
    report = summarize([row() for _ in range(20)] + [row(correct=False)])
    progress = report["progress_assessment"]
    assert progress["accuracy"] > GATES["min_accuracy"]
    assert not progress["critical_passed"] and not progress["regression_passed"]
    assert progress["baseline_accuracy"] == 1


def test_empty_or_slow_results_cannot_pass():
    assert not any(r["regression_passed"] for r in summarize([]).values())
    report = summarize([row(duration_ms=GATES["max_warm_latency_ms"] + 1)])
    assert not report["progress_assessment"]["regression_passed"]
