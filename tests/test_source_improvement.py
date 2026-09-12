"""Real source checks: fixed evidence, isolated edits, and honest interruption."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness.events import EvaluationRunFinished, EvaluationRunStarted
from harness.fold import fold
from harness.improvement import Evidence, verdict
from harness.improvement_journal import ImprovementJournal, inspect_improvement
from harness.log import read_session
from harness.resume import resume_session
from harness.session import Session
from harness.types import SessionId


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


@pytest.fixture
def revisions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "calc.py").write_text("def double(x):\n    return x + 1\n")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-qm", "incumbent")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "calc.py").write_text("def double(x):\n    return x + x\n")
    git(repo, "commit", "-qam", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    # Dirty, private and untracked work is neither included nor modified.
    (repo / "calc.py").write_text("private uncommitted work\n")
    (repo / "private.txt").write_text("keep me\n")
    return repo, base, candidate


def specification(revisions, **overrides):
    repo, base, candidate = revisions
    probe = "import sys\nsys.path.insert(0, '.')\nfrom calc import double\nassert double({x}) == {expected}\n"
    data = dict(repository=str(repo), incumbent_revision=base, candidate_revision=candidate,
                evidence_ids=["e"], hypothesis="Use both operands", expected_benefit="Correct doubling",
                cases=[dict(id="regression", partition="regression", critical=True,
                            script=probe.format(x=1, expected=2)),
                       dict(id="new", partition="held_out", critical=True,
                            script=probe.format(x=7, expected=14))],
                max_latency_ratio=20, max_case_latency_ms=10000)
    return data | overrides


def journal_at(tmp_path):
    session = Session(tmp_path / "sessions", SessionId("source"))
    session.start()
    journal = ImprovementJournal(session)
    journal.record(Evidence(id="e", source_session=session.id, source_seq=1,
                            category="failure", observation="Double returned the wrong answer"))
    return session, journal


async def prepare(journal, revisions, **overrides):
    from harness.source_improvement import SourceProposal, prepare_source
    return await prepare_source(journal, SourceProposal.model_validate(specification(revisions, **overrides)))


async def test_useful_patch_is_measured_without_touching_source_or_activation(tmp_path, revisions):
    from harness.source_improvement import run_source_evaluation
    repo, _, _ = revisions
    dirty = git(repo, "status", "--porcelain")
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, revisions)
        text = inspect_improvement(journal.state, session.blobs, plan.candidate_id)
        assert "calc.py" in text and "+    return x + x" in text
        result = await run_source_evaluation(journal, plan.id)
        assert verdict(plan, result) == "passed"
        assert result.observations[1].incumbent_passed is False
        assert result.observations[1].candidate_passed is True
        assert not journal.state.prompt_changes
        report = json.loads(session.blobs.get(result.artifact))
        assert report["activation_qualified"] is False
        assert report["isolation"] == "fresh_directory_not_security_sandbox"
        assert report["cases"][0]["incumbent"]["returncode"] == 0
        assert git(repo, "status", "--porcelain") == dirty
        assert (repo / "calc.py").read_text() == "private uncommitted work\n"
    reopened, _ = resume_session(session.base, session.id)
    with reopened:
        replayed = ImprovementJournal(reopened)
        assert replayed.state.results[result.id] == result
        assert not fold(read_session(session.base, session.id)).open_evaluations


async def test_regression_is_not_hidden_by_an_improvement(tmp_path, revisions):
    from harness.source_improvement import run_source_evaluation
    session, journal = journal_at(tmp_path)
    cases = specification(revisions)["cases"]
    cases[0]["script"] = "from pathlib import Path\nassert 'x + 1' in Path('calc.py').read_text()\n"
    with session:
        plan = await prepare(journal, revisions, cases=cases)
        result = await run_source_evaluation(journal, plan.id)
        assert verdict(plan, result) == "failed"
        assert result.observations[1].candidate_passed is True


@pytest.mark.parametrize("problem", ["timeout", "output"])
async def test_bounded_failure_cannot_pass(tmp_path, revisions, problem):
    from harness.source_improvement import run_source_evaluation
    session, journal = journal_at(tmp_path)
    cases = specification(revisions)["cases"]
    cases[0]["script"] = ("import time\ntime.sleep(10)" if problem == "timeout"
                          else "print('x' * 100000)")
    with session:
        plan = await prepare(journal, revisions, cases=cases, case_timeout_seconds=0.1,
                             max_output_bytes=1024)
        result = await run_source_evaluation(journal, plan.id)
        report = json.loads(session.blobs.get(result.artifact))
        assert report["cases"][0]["incumbent"]["status"] == (
            "timed_out" if problem == "timeout" else "output_limit")
        assert verdict(plan, result) != "passed"
        assert len(report["cases"][0]["incumbent"]["stdout"].encode()) <= 1024


async def test_check_cancellation_settles_before_terminal_and_retains_unknowns(tmp_path, revisions, monkeypatch):
    import harness.source_improvement as source
    session, journal = journal_at(tmp_path)
    entered = asyncio.Event()
    original = source._run_check

    async def observed(*args, **kwargs):
        entered.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(source, "_run_check", observed)
    cases = specification(revisions)["cases"]
    cases[0]["script"] = "import time\ntime.sleep(60)"
    with session:
        plan = await prepare(journal, revisions, cases=cases)
        task = asyncio.create_task(source.run_source_evaluation(journal, plan.id))
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = list(journal.state.results.values())[-1]
        assert result.completion == "cancelled"
        assert all(row.candidate_passed is None for row in result.observations)
        events = read_session(session.base, session.id)
        assert isinstance(events[-1].event, EvaluationRunFinished)
        assert events[-1].event.status == "cancelled"
        assert sum(isinstance(e.event, EvaluationRunStarted) for e in events) == 1
        assert not fold(events).open_evaluations


async def test_candidate_check_edits_do_not_replace_frozen_operator_checks(tmp_path, revisions):
    from harness.source_improvement import run_source_evaluation
    repo, base, _ = revisions
    (repo / "calc.py").write_text("def double(x):\n    return 999\n")
    (repo / "check.py").write_text("# everything passes!\n")
    git(repo, "add", "calc.py", "check.py")
    git(repo, "commit", "-qm", "weaken checks")
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, (repo, base, git(repo, "rev-parse", "HEAD")))
        result = await run_source_evaluation(journal, plan.id)
        assert verdict(plan, result) == "failed"


@pytest.mark.parametrize("kind", ["symlink", "submodule", "empty"])
async def test_nonportable_or_empty_candidates_refused_before_records(tmp_path, revisions, kind):
    repo, base, candidate = revisions
    if kind == "symlink":
        (repo / "link").symlink_to(tmp_path / "outside")
        git(repo, "add", "link")
    elif kind == "submodule":
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{candidate},module")
    if kind != "empty":
        git(repo, "commit", "-qm", kind)
        candidate = git(repo, "rev-parse", "HEAD")
    else:
        candidate = base
    session, journal = journal_at(tmp_path)
    with session, pytest.raises(ValueError):
        await prepare(journal, (repo, base, candidate))
    assert not journal.state.candidates and not journal.state.plans


async def test_prepared_source_is_portable_and_repeated_preparation_is_deterministic(tmp_path, revisions):
    from harness.source_improvement import run_source_evaluation
    session, journal = journal_at(tmp_path)
    with session:
        first = await prepare(journal, revisions)
        second = await prepare(journal, revisions)
        assert first.incumbent_version == second.incumbent_version
        assert first.candidate_version == second.candidate_version
        assert first.suite == second.suite
        assert "assert double(7) == 14" in inspect_improvement(journal.state, session.blobs, first.id)
        revisions[0].rename(tmp_path / "moved-repo")
        result = await run_source_evaluation(journal, first.id)
        assert verdict(first, result) == "passed"


async def test_checks_start_fresh_and_do_not_inherit_parent_environment(tmp_path, revisions, monkeypatch):
    from harness.source_improvement import run_source_evaluation
    monkeypatch.setenv("SOURCE_TEST_CREDENTIAL", "not-for-child")
    script = ("import os\nfrom pathlib import Path\n"
              "assert 'SOURCE_TEST_CREDENTIAL' not in os.environ\n"
              "assert not Path('marker').exists()\nPath('marker').touch()\n")
    cases = specification(revisions)["cases"]
    for case in cases:
        case["script"] = script + case["script"]
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, revisions, cases=cases)
        result = await run_source_evaluation(journal, plan.id)
        assert verdict(plan, result) == "passed"


@pytest.mark.parametrize("path", ["../outside", "/outside", "a/../../outside", ".git/config",
                                 "a/.GIT/config", "a\\outside", "a//b", "a/./b", "a\nb"])
def test_every_materialized_source_path_must_be_safe(path):
    from harness.source_improvement import SourceSnapshot
    with pytest.raises(ValueError, match="source paths"):
        SourceSnapshot(revision="a" * 40, files={path: {"content": ""}})


@pytest.mark.parametrize("mutation", ["path", "digest", "duplicate", "overlap", "empty"])
async def test_corrupt_patch_refused_before_execution(tmp_path, revisions, mutation):
    from harness.source_improvement import load_patch
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, revisions)
        patch = json.loads(session.blobs.get(journal.state.candidates[plan.candidate_id].artifact))
        if mutation == "path":
            patch["edits"][0]["path"] = "../escape"
        elif mutation == "digest":
            patch["edits"][0]["before"] = "f" * 64
        elif mutation == "duplicate":
            patch["edits"] *= 2
        elif mutation == "overlap":
            patch["edits"].append(dict(path="calc.py/child", before=None, after={"content": ""}))
        else:
            patch["edits"].append(dict(path="missing", before=None, after=None))
        with pytest.raises(ValueError):
            load_patch(session.blobs, session.blobs.put(json.dumps(patch).encode()))


@pytest.mark.parametrize("mutation", ["suite", "artifact", "version"])
async def test_corruption_or_evaluator_drift_never_starts_a_check(tmp_path, revisions, monkeypatch, mutation):
    import harness.source_improvement as source
    from harness.blobs import BlobIntegrityError
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, revisions)
        if mutation == "version":
            monkeypatch.setattr(source, "evaluator_version", lambda suite: "f" * 64)
        else:
            ref = plan.suite if mutation == "suite" else journal.state.candidates[plan.candidate_id].artifact
            (session.blobs._root / ref.sha256).write_bytes(b"corrupted")
        with pytest.raises((ValueError, BlobIntegrityError)):
            await source.run_source_evaluation(journal, plan.id)
        assert not any(isinstance(e.event, EvaluationRunStarted) for e in read_session(session.base, session.id))


async def test_abandoned_evaluation_is_aborted_on_replay_without_relaunch(tmp_path, revisions):
    from harness.source_improvement import load_patch
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, revisions)
        patch, _, _ = load_patch(session.blobs, journal.state.candidates[plan.candidate_id].artifact)
        session.append(EvaluationRunStarted(run_id="interrupted", plan_id=plan.id,
                                           incumbent=patch.incumbent, configuration=plan.suite))
    resumed, _ = resume_session(session.base, session.id)
    with resumed:
        state = ImprovementJournal(resumed).state
        assert state.runs["interrupted"] == "aborted"
        assert not state.results
        assert plan.id in state.plans


async def test_overall_timeout_preserves_completed_measurements(tmp_path, revisions, monkeypatch):
    import harness.source_improvement as source
    session, journal = journal_at(tmp_path)
    original = source._run_check
    real_timeout = asyncio.timeout
    deadlines = []
    calls = 0

    def capture_timeout(delay):
        deadline = real_timeout(delay)
        deadlines.append(deadline)
        return deadline

    async def expire(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Expire the registered outer timeout after one real check finishes.
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            await asyncio.sleep(0)
        return await original(*args)

    monkeypatch.setattr(source, "_run_check", expire)
    with session:
        plan = await prepare(journal, revisions)
        monkeypatch.setattr(asyncio, "timeout", capture_timeout)
        result = await source.run_source_evaluation(journal, plan.id)
        assert deadlines[0].expired()
        assert result.completion == "timed_out"
        assert result.observations[0].incumbent_passed is True
        assert result.observations[0].candidate_passed is None
        assert verdict(plan, result) == "inconclusive"


def _alive(pid):
    try:
        # A killed grandchild may remain a zombie until its system parent reaps it.
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("mode", ["success", "timeout", "cancel"])
async def test_process_group_is_settled_before_result(mode, tmp_path):
    from harness.source_improvement import _process
    pidfile = tmp_path / "child.pid"
    script = ("import subprocess,sys,time\nfrom pathlib import Path\n"
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
              " stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
              f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
              + ("time.sleep(60)\n" if mode != "success" else ""))
    task = asyncio.create_task(_process([sys.executable, "-c", script], cwd=tmp_path,
                                       env={"PATH": os.defpath}, timeout=5, limit=4096))
    async with asyncio.timeout(5):
        while not pidfile.exists():
            await asyncio.sleep(0.01)
    child = int(pidfile.read_text())
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["status"] == ("timed_out" if mode == "timeout" else "completed")
    async with asyncio.timeout(1):
        while _alive(child):
            await asyncio.sleep(0.01)


async def test_headless_prepare_evaluate_and_inspection_need_no_provider(tmp_path, revisions, monkeypatch, capsys):
    from harness.cli import main
    session, _ = journal_at(tmp_path)
    session.close()
    spec = tmp_path / "source.json"
    spec.write_text(json.dumps(specification(revisions)))
    common = ["harness", "improve-source", "--base-dir", str(session.base), str(session.id)]
    monkeypatch.setattr(sys, "argv", [*common, "prepare", str(spec)])
    await asyncio.to_thread(main)
    assert "No code is activated" in capsys.readouterr().out
    from harness.improvement_journal import read_improvements
    plan = next(iter(read_improvements(session.base, session.id).plans.values()))
    monkeypatch.setattr(sys, "argv", [*common, "evaluate", plan.id])
    await asyncio.to_thread(main)
    assert "passed (completed)" in capsys.readouterr().out
    result = next(iter(read_improvements(session.base, session.id).results.values()))
    monkeypatch.setattr(sys, "argv", ["harness", "improvements", "--base-dir", str(session.base),
                                     str(session.id), "--show", result.id])
    await asyncio.to_thread(main)
    output = capsys.readouterr().out
    assert "Recorded source check outcomes" in output and "returncode" in output


async def test_source_controls_work_in_the_rendered_terminal(tmp_path, revisions):
    from tests.test_tui import make_app
    from tests.test_tui_improvements import submit
    from tests.test_tui_queue import screen_text
    app = make_app(tmp_path / "tui")
    spec = tmp_path / "source.json"
    spec.write_text(json.dumps(specification(revisions)))
    async with app.run_test(size=(160, 55)) as pilot:
        await pilot.pause(0.1)
        journal = app.kernel.improvements
        journal.record(Evidence(id="e", source_session=journal.session.id, source_seq=1,
                                category="failure", observation="Double returned the wrong answer"))
        await submit(app, pilot, f"/improvements source-prepare {spec}")
        plan = next(iter(app.kernel.improvements.state.plans.values()))
        assert "Source candidate" in screen_text(app)
        await submit(app, pilot, f"/improvements show {plan.candidate_id}")
        assert "return x + x" in screen_text(app)
        await submit(app, pilot, f"/improvements source-evaluate {plan.id}")
        assert "passed (completed)" in screen_text(app)
        assert "do not qualify code activation" in screen_text(app)
        assert not fold(read_session(journal.session.base, journal.session.id)).messages


async def test_large_output_is_bounded_without_blocking_reaping(tmp_path):
    from harness.source_improvement import _process
    async with asyncio.timeout(5):
        result = await _process([sys.executable, "-c", "import os; os.write(1, b'x' * 10000000)"],
                                cwd=tmp_path, env={}, timeout=2, limit=256)
    assert result["status"] == "output_limit"
    assert len(result["stdout"]) + len(result["stderr"]) == 256


async def test_cancellation_during_launch_still_owns_and_reaps_process(tmp_path, monkeypatch):
    from harness.source_improvement import _process
    loop = asyncio.get_running_loop()
    original = loop.subprocess_exec
    ready = asyncio.Event()
    release = asyncio.Event()
    owned = []

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        owned.append(result[0].get_pid())
        ready.set()
        await release.wait()
        return result

    monkeypatch.setattr(loop, "subprocess_exec", delayed)
    task = asyncio.create_task(_process([sys.executable, "-c", "import time;time.sleep(60)"],
                                        cwd=tmp_path, env={}, timeout=5, limit=1024))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # repeated cancellation cannot release the pending launch
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owned and not _alive(owned[0])


async def test_launch_error_is_recorded_as_failed_with_unknown_measurements(tmp_path, revisions, monkeypatch):
    import harness.source_improvement as source
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, revisions)

        async def unavailable(*args, **kwargs):
            raise FileNotFoundError("interpreter unavailable")

        monkeypatch.setattr(asyncio.get_running_loop(), "subprocess_exec", unavailable)
        with pytest.raises(FileNotFoundError):
            await source.run_source_evaluation(journal, plan.id)
        result = next(iter(journal.state.results.values()))
        assert result.completion == "failed"
        assert verdict(plan, result) == "inconclusive"
        assert not fold(read_session(session.base, session.id)).open_evaluations


async def test_patch_preserves_additions_deletions_binary_and_executable_modes(tmp_path, revisions):
    from harness.source_improvement import load_patch
    repo, base, _ = revisions
    git(repo, "rm", "-f", "calc.py")
    (repo / "run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "image.dat").write_bytes(b"\0\xff\x01")
    git(repo, "add", "run.sh", "image.dat")
    git(repo, "commit", "-qm", "new files")
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, (repo, base, git(repo, "rev-parse", "HEAD")))
        _, incumbent, candidate = load_patch(session.blobs, journal.state.candidates[plan.candidate_id].artifact)
        assert "calc.py" in incumbent.files and "calc.py" not in candidate.files
        assert candidate.files["run.sh"].executable
        assert candidate.files["image.dat"].bytes() == b"\0\xff\x01"


async def test_legacy_code_records_remain_inspectable(tmp_path):
    from tests.test_improvement import records
    session, journal = journal_at(tmp_path)
    with session:
        evidence, candidate, plan, result = records(session)
        candidate = candidate.model_copy(update={"target": "code"})
        for record in (evidence, candidate, plan, result):
            journal.record(record)
        for record in (candidate, plan, result):
            assert record.id in inspect_improvement(journal.state, session.blobs, record.id)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize"])
async def test_proposal_file_refusals_do_not_run_or_record(tmp_path, kind):
    from harness.source_improvement_cli import perform
    path = tmp_path / "proposal"
    if kind == "symlink":
        path.symlink_to(tmp_path / "elsewhere")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b" " * (1024 * 1024 + 1))
    session, journal = journal_at(tmp_path)
    with session, pytest.raises(ValueError):
        await perform(journal, ["prepare", str(path)])
    assert not journal.state.candidates


async def test_cli_live_session_refusal_teaches_the_available_control(tmp_path):
    from harness.source_improvement_cli import main
    session, _ = journal_at(tmp_path)
    with session, pytest.raises(SystemExit, match="use its /improvements controls"):
        await asyncio.to_thread(main, ["--base-dir", str(session.base), str(session.id), "evaluate", "p"])


async def test_source_checks_cannot_accidentally_discover_parent_git_repository(tmp_path, revisions, monkeypatch):
    import harness.source_improvement as source
    repo, _, _ = revisions
    # Force the actual fresh source copies underneath an existing Git repository.
    monkeypatch.setattr(source.tempfile, "tempdir", str(repo))
    cases = specification(revisions)["cases"]
    for case in cases:
        case["script"] = ("import subprocess\n"
            "result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True)\n"
            "assert result.returncode != 0, result.stdout\n" + case["script"])
    session, journal = journal_at(tmp_path)
    with session:
        plan = await prepare(journal, revisions, cases=cases)
        result = await source.run_source_evaluation(journal, plan.id)
        assert verdict(plan, result) == "passed"
