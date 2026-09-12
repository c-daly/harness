"""Explicit source selection, rollback and real next-process activation."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness.improvement_journal import ImprovementJournal, read_improvements, render_improvements
from harness.resume import resume_session
from harness.source_improvement import run_source_evaluation
from tests.test_source_improvement import git, journal_at, prepare, revisions as source_revisions

revisions = source_revisions


async def ready(tmp_path, revisions):
    repo, base, candidate = revisions
    # Both committed versions expose the same operator-selected entrypoint.
    entry = ('import os, sys\nfrom calc import double\n'
             'def main():\n    print(double(int(sys.argv[1])))\n'
             '    print(os.getcwd())\n')
    git(repo, 'checkout', '-f', base)
    (repo / 'entry.py').write_text(entry)
    git(repo, 'add', 'entry.py')
    git(repo, 'commit', '-qm', 'baseline entrypoint')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'calc.py').write_text('def double(x):\n    return x + x\n')
    git(repo, 'commit', '-qam', 'fix')
    candidate = git(repo, 'rev-parse', 'HEAD')
    (repo / 'calc.py').write_text('private dirty work\n')
    session, journal = journal_at(tmp_path)
    plan = await prepare(journal, (repo, base, candidate))
    result = await run_source_evaluation(journal, plan.id)
    return session, journal, result


def launch(session, slot='trial', *args):
    from harness.source_promotion import launch_source
    return launch_source(session.base, session.id, slot, list(args), execute=False)


def invoke(command, cwd):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=10, check=True)


async def test_adopt_launch_and_rollback_restore_behavior_after_restart(tmp_path, revisions):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source, rollback_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        change = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        command, tree = launch(session, 'trial', '7')
        assert invoke(command, tmp_path).stdout.splitlines() == ['14', str(tmp_path)]
        assert 'Selected source trial:' in render_improvements(journal.state)
        assert 'private dirty work' in (revisions[0] / 'calc.py').read_text()
    reopened, _ = resume_session(session.base, session.id)
    with reopened:
        journal = ImprovementJournal(reopened)
        assert journal.state.source_changes[change.id] == change
        undo = rollback_source(journal, 'trial')
        assert undo.source == change.previous
        restored, restored_tree = launch(reopened, 'trial', '7')
        assert invoke(restored, tmp_path).stdout.splitlines()[0] == '8'
        assert invoke(command, tmp_path).stdout.splitlines()[0] == '14'
        assert restored_tree != tree
        assert read_improvements(session.base, session.id).active_sources['trial'] == undo.id


@pytest.mark.parametrize('problem', ['failed', 'incomplete', 'unfinished', 'stale', 'wrong_entry', 'corrupt'])
async def test_ineligible_adoption_never_changes_selection(tmp_path, revisions, monkeypatch, problem):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        if problem in {'failed', 'incomplete'}:
            from harness.events import EvaluationRunStarted, EvaluationRunFinished
            from harness.improvement import ExperimentResult
            plan = journal.state.plans[result.plan_id]
            candidate = journal.state.candidates[plan.candidate_id]
            from harness.source_improvement import load_patch
            patch, _, _ = load_patch(session.blobs, candidate.artifact)
            session.append(EvaluationRunStarted(run_id='later', plan_id=plan.id,
                                               incumbent=patch.incumbent, configuration=plan.suite))
            observations = list(result.observations)
            observations[0] = observations[0].model_copy(update={'candidate_passed': False if problem == 'failed' else None})
            later = ExperimentResult(**(result.model_dump() | {'id': 'later', 'run_id': 'later',
                                                              'observations': observations}))
            journal.record(later)
            session.append(EvaluationRunFinished(run_id='later', status='completed', result_id=later.id))
        elif problem == 'unfinished':
            # Preserve a recorded result but lose its terminal line, as after a crash.
            log = session.base / 'sessions' / f'{session.id}.jsonl'
            lines = log.read_bytes().splitlines(keepends=True)
            log.write_bytes(b''.join(lines[:-1]))
        elif problem == 'stale':
            monkeypatch.setattr('harness.source_promotion.evaluator_version', lambda suite: '0' * 64)
        elif problem == 'corrupt':
            (session.blobs._root / result.artifact.sha256).write_text('corrupt')
        entry = SourceEntrypoint(module='absent' if problem == 'wrong_entry' else 'entry', function='main')
        from harness.blobs import BlobIntegrityError
        with pytest.raises((ValueError, BlobIntegrityError)):
            adopt_source(journal, result.id, 'trial', entry)
        assert not read_improvements(session.base, session.id).source_changes


async def test_launch_copies_are_private_and_refuse_corrupt_snapshot(tmp_path, revisions):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source
    from harness.blobs import BlobIntegrityError
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        change = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        _, first = launch(session, 'trial', '7')
        (first / 'calc.py').write_text('def double(x): return -1\n')
        command, second = launch(session, 'trial', '7')
        assert first != second and invoke(command, tmp_path).stdout.splitlines()[0] == '14'
        (session.blobs._root / change.source.sha256).write_text('corrupt')
        with pytest.raises(BlobIntegrityError):
            launch(session)


async def test_model_free_cli_adoption_and_actual_exec(tmp_path, revisions):
    session, journal, result = await ready(tmp_path, revisions)
    session.close()
    # cli.py is invoked through its installed console entry point in production.
    base = [sys.executable, '-c', 'from harness.cli import main; main()']
    env = os.environ | {'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    args = ['--base-dir', str(session.base), str(session.id)]
    adopted = subprocess.run(base + ['improve-source', *args, 'adopt', result.id, 'trial', 'entry:main', '.'],
                             env=env, capture_output=True, text=True, timeout=15)
    assert adopted.returncode == 0, adopted.stderr
    assert 'next process' in adopted.stdout
    child = subprocess.run(base + ['run-source', *args, 'trial', '--', '7'], cwd=tmp_path,
                           env=env, capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert child.stdout.splitlines() == ['14', str(tmp_path)]
    assert 'Source launch' in child.stderr
    rolled = subprocess.run(base + ['improve-source', *args, 'rollback', 'trial'],
                            env=env, capture_output=True, text=True, timeout=15)
    assert rolled.returncode == 0, rolled.stderr
    assert json.loads(read_improvements(session.base, session.id).source_changes[
        read_improvements(session.base, session.id).active_sources['trial']].model_dump_json())['action'] == 'rollback'


async def test_selection_append_is_only_activation_authority(tmp_path, revisions, monkeypatch):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source, rollback_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        original = session.append
        def fail(event):
            raise OSError('journal unavailable')
        monkeypatch.setattr(session, 'append', fail)
        with pytest.raises(OSError, match='journal unavailable'):
            adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        assert not read_improvements(session.base, session.id).active_sources
        assert not (session.base / 'source-launches').exists()
        monkeypatch.setattr(session, 'append', original)
        adopted = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        # An older result cannot overwrite a newer incumbent, even when it passed.
        with pytest.raises(ValueError, match='current selected version'):
            adopt_source(journal, result.id, 'trial', adopted.entrypoint)
        monkeypatch.setattr(session, 'append', fail)
        with pytest.raises(OSError, match='journal unavailable'):
            rollback_source(journal, 'trial')
        assert read_improvements(session.base, session.id).active_sources['trial'] == adopted.id


@pytest.mark.parametrize('failure', ['write', 'exec', 'symlink'])
async def test_launch_failure_preserves_selection_and_unrelated_files(tmp_path, revisions, monkeypatch, failure):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source, launch_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        change = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        unrelated = tmp_path / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'keep').write_text('private')
        if failure == 'symlink':
            (session.base / 'source-launches').symlink_to(unrelated, target_is_directory=True)
        elif failure == 'exec':
            def fail(*args):
                raise OSError('exec failed')
            monkeypatch.setattr(os, 'execv', fail)
        else:
            write = Path.write_bytes
            def fail(path, data):
                if path.name == 'calc.py':
                    raise OSError('disk full')
                return write(path, data)
            monkeypatch.setattr(Path, 'write_bytes', fail)
        with pytest.raises((OSError, ValueError)):
            launch_source(session.base, session.id, 'trial', ['7'])
        assert read_improvements(session.base, session.id).active_sources['trial'] == change.id
        assert (unrelated / 'keep').read_text() == 'private'
        if failure != 'symlink':
            assert not list((session.base / 'source-launches').iterdir())


async def test_forged_selection_and_rollback_are_rejected_by_journal(tmp_path, revisions):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source, rollback_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        change = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        undo = rollback_source(journal, 'trial')
        for update in ({'source': change.previous}, {'previous_id': change.id},
                       {'previous_entrypoint': change.entrypoint.model_copy(update={'function': 'different'})},
                       {'evaluator_version': '0' * 64}):
            forged = change.model_copy(update={'id': 'forged', 'previous_id': undo.id} | update)
            with pytest.raises(ValueError):
                journal.record(forged)
        forged = undo.model_copy(update={'id': 'forged', 'previous_id': undo.id,
                                         'previous': undo.source, 'source': undo.source})
        with pytest.raises(ValueError, match='exact preceding source'):
            journal.record(forged)
        assert read_improvements(session.base, session.id).active_sources['trial'] == undo.id


async def test_preceding_entrypoint_is_restored_after_second_adoption(tmp_path, revisions):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source, rollback_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        first = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        repo = revisions[0]
        base = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'checkout', '--', 'calc.py')
        with (repo / 'calc.py').open('a') as output:
            output.write('def triple(x):\n    return x * 3\n')
        with (repo / 'entry.py').open('a') as output:
            output.write('def triple_main():\n    from calc import triple\n    print(triple(int(sys.argv[1])))\n')
        git(repo, 'commit', '-qam', 'add triple')
        cases = [dict(id='double', partition='regression', critical=True,
                      script="import sys; sys.path.insert(0, '.'); from calc import double; assert double(7) == 14"),
                 dict(id='triple', partition='held_out', critical=True,
                      script="import sys; sys.path.insert(0, '.'); from calc import triple; assert triple(7) == 21")]
        plan = await prepare(journal, (repo, base, git(repo, 'rev-parse', 'HEAD')), cases=cases)
        result = await run_source_evaluation(journal, plan.id)
        second = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='triple_main'))
        assert second.previous == first.source
        command, _ = launch(session, 'trial', '7')
        assert invoke(command, tmp_path).stdout.strip() == '21'
        restored = rollback_source(journal, 'trial')
        assert restored.source == first.source and restored.entrypoint == first.entrypoint
        command, _ = launch(session, 'trial', '7')
        assert invoke(command, tmp_path).stdout.splitlines()[0] == '14'


@pytest.mark.parametrize('root', ['../src', '/src', 'src//nested', '.git', 'src/./nested'])
def test_entrypoint_import_roots_refuse_traversal(root):
    from harness.improvement import SourceEntrypoint
    with pytest.raises(ValueError):
        SourceEntrypoint(module='entry', function='main', import_root=root)


def test_regular_source_package_required_before_any_launch():
    from harness.improvement import SourceEntrypoint
    from harness.source_improvement import SourceFile, SourceSnapshot
    from harness.source_promotion import _entry_file
    empty = SourceFile(content='')
    entry = SourceEntrypoint(module='harness.cli', function='main', import_root='src')
    snapshot = SourceSnapshot(revision='1' * 40, files={'src/harness/cli.py': empty})
    with pytest.raises(ValueError, match='__init__.py'):
        _entry_file(snapshot, entry)
    snapshot = SourceSnapshot(revision='1' * 40, files={**snapshot.files, 'src/harness/__init__.py': empty})
    assert _entry_file(snapshot, entry) == 'src/harness/cli.py'


def test_evaluator_normalizes_interpreter_spelling_without_erasing_virtualenv(monkeypatch):
    from harness.source_improvement import evaluator_version, SourceSuite
    from tests.test_source_improvement import specification
    fields = specification((Path('.'), 'main', 'candidate'))
    suite = SourceSuite.model_validate({k: v for k, v in fields.items() if k in SourceSuite.model_fields})
    monkeypatch.setattr(sys, 'executable', '/tmp/venv/bin/python')
    expected = evaluator_version(suite)
    monkeypatch.setattr(sys, 'executable', '/tmp/venv/../venv/bin/python')
    assert evaluator_version(suite) == expected
    monkeypatch.setattr(sys, 'executable', '/tmp/other/bin/python')
    assert evaluator_version(suite) != expected


async def test_new_selection_records_round_trip_and_do_not_execute_on_fold(tmp_path, revisions, monkeypatch):
    from harness.events import Envelope, ImprovementRecorded, UnknownEvent, parse_envelope_line
    from harness.fold import fold
    from harness.improvement import SourceEntrypoint
    from harness.log import read_session
    from harness.source_promotion import adopt_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        change = adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
        line = Envelope(session_id=session.id, seq=100, ts=1, event=ImprovementRecorded(record=change)).model_dump_json()
        assert parse_envelope_line(line).event.record == change
        assert isinstance(parse_envelope_line(line.replace('source_change', 'future_change')).event, UnknownEvent)
        def forbidden(*args, **kwargs):
            raise AssertionError('replay attempted filesystem activation')
        monkeypatch.setattr('harness.source_promotion.launch_source', forbidden)
        folded = fold(read_session(session.base, session.id))
        assert not folded.messages
        assert read_improvements(session.base, session.id).source_changes[change.id] == change


async def test_source_selection_controls_are_visible_in_terminal(tmp_path, revisions):
    from harness.improvement import Evidence
    from tests.test_tui import make_app
    from tests.test_tui_improvements import submit
    from tests.test_tui_queue import screen_text
    session, _, _ = await ready(tmp_path, revisions)
    session.close()
    repo = revisions[0]
    candidate, base = git(repo, 'rev-parse', 'HEAD'), git(repo, 'rev-parse', 'HEAD^')
    app = make_app(tmp_path / 'tui')
    async with app.run_test(size=(160, 55)) as pilot:
        await pilot.pause(0.1)
        journal = app.kernel.improvements
        journal.record(Evidence(id='e', source_session=journal.session.id, source_seq=1,
                                category='failure', observation='Wrong doubling'))
        plan = await prepare(journal, (repo, base, candidate))
        result = await run_source_evaluation(journal, plan.id)
        await submit(app, pilot, f'/improvements source-adopt {result.id} trial entry:main .')
        assert 'Applies to the next process' in screen_text(app)
        selected = journal.state.active_sources['trial']
        await submit(app, pilot, f'/improvements show {selected}')
        assert 'supervised-source-v1' in screen_text(app)
        await submit(app, pilot, '/improvements source-rollback trial')
        assert 'Restored source trial' in screen_text(app)
        await submit(app, pilot, '/improvements')
        assert 'Selected source trial:' in screen_text(app)
        assert journal.state.active_sources['trial'] != selected


async def test_torn_journal_launch_teaches_recovery_without_repair(tmp_path, revisions):
    from harness.improvement import SourceEntrypoint
    from harness.source_promotion import adopt_source
    session, journal, result = await ready(tmp_path, revisions)
    with session:
        adopt_source(journal, result.id, 'trial', SourceEntrypoint(module='entry', function='main'))
    log = session.base / 'sessions' / f'{session.id}.jsonl'
    with log.open('ab') as output:
        output.write(b'{"v":1')
    before = log.read_bytes()
    result = subprocess.run([sys.executable, '-c', 'from harness.cli import main; main()',
        'run-source', '--base-dir', str(session.base), str(session.id), 'trial'],
        capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'Traceback' not in result.stderr and 'resume' in result.stderr
    assert log.read_bytes() == before and not (session.base / 'source-launches').exists()
