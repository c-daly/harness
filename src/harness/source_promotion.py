"""Explicit source selection and fresh-process launch, outside the running installation."""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from harness.blobs import BlobStore
from harness.events import EvaluationRunFinished, EvaluationRunStarted
from harness.improvement import EvaluationPlan, SourceChange, SourceEntrypoint
from harness.improvement_journal import read_improvements
from harness.log import read_session
from harness.source_improvement import (
    SourceSnapshot, SourceSuite, _encoded, _load, evaluator_version, load_patch,
)
from harness.types import SessionId


def _entry_file(snapshot, entry):
    """Require source-owned regular modules/packages; never fall back to an installed copy."""
    prefix = '' if entry.import_root == '.' else entry.import_root + '/'
    parts = entry.module.split('.')
    for end in range(1, len(parts)):
        if prefix + '/'.join(parts[:end]) + '/__init__.py' not in snapshot.files:
            raise ValueError('source entrypoint requires regular packages with __init__.py')
    stem = prefix + '/'.join(parts)
    for path in (stem + '/__init__.py', stem + '.py'):
        if path in snapshot.files:
            return path
    raise ValueError('source entrypoint module is missing from the snapshot')


def validate_change(session, state, change):
    """Check artifacts and authority before the one durable selection append."""
    before = _load(session.blobs, change.previous, SourceSnapshot)
    after = _load(session.blobs, change.source, SourceSnapshot)
    _entry_file(before, change.previous_entrypoint)
    _entry_file(after, change.entrypoint)
    if change.action == 'rollback':
        return
    if any(status == 'running' for status in state.runs.values()):
        raise ValueError('finish or recover running evaluations before source adoption')
    result = state.results[change.result_id]
    plan = state.plans[result.plan_id]
    candidate = state.candidates[plan.candidate_id]
    patch, incumbent, selected = load_patch(session.blobs, candidate.artifact)
    suite = _load(session.blobs, plan.suite, SourceSuite)
    expected = EvaluationPlan(**(plan.model_dump() | suite.plan_fields()))
    if (patch.incumbent != change.previous or incumbent != before or selected != after
            or session.blobs.get(change.source) != _encoded(selected)
            or plan != expected or evaluator_version(suite) != plan.evaluator_version):
        raise ValueError('source evaluator, fixed suite or exact snapshots differ; prepare and evaluate a new plan')
    events = read_session(session.base, session.id, repair=False)
    starts = [e.event for e in events if isinstance(e.event, EvaluationRunStarted) and e.event.run_id == result.run_id]
    finishes = [e.event for e in events if isinstance(e.event, EvaluationRunFinished) and e.event.run_id == result.run_id]
    if (len(starts) != 1 or len(finishes) != 1 or starts[0].plan_id != plan.id
            or starts[0].incumbent != change.previous or starts[0].configuration != plan.suite
            or finishes[0].status != 'completed' or finishes[0].result_id != result.id):
        raise ValueError('source adoption requires its completed paired evaluation run')
    session.blobs.get(result.artifact)


def adopt_source(journal, result_id, slot, entrypoint):
    entrypoint = SourceEntrypoint.model_validate(entrypoint.model_dump())
    state = read_improvements(journal.session.base, journal.session.id)
    result = state.results.get(result_id)
    if result is None:
        raise ValueError('unknown source result; prepare and evaluate a source candidate first')
    candidate = state.candidates[state.plans[result.plan_id].candidate_id]
    if candidate.target != 'code':
        raise ValueError('source adoption requires a code candidate')
    patch, _, selected = load_patch(journal.session.blobs, candidate.artifact)
    previous = state.source_changes.get(state.active_sources.get(slot))
    change = SourceChange(id=str(uuid4()), action='adopt', slot=slot,
        previous_id=previous.id if previous else None, previous=patch.incumbent,
        source=journal.session.blobs.put(_encoded(selected)),
        previous_entrypoint=previous.entrypoint if previous else entrypoint,
        entrypoint=entrypoint, result_id=result.id, evaluator_version=result.evaluator_version)
    journal.record(change)
    return change


def rollback_source(journal, slot):
    state = read_improvements(journal.session.base, journal.session.id)
    previous = state.source_changes.get(state.active_sources.get(slot))
    if previous is None:
        raise ValueError('no selected source in this slot to roll back')
    change = SourceChange(id=str(uuid4()), action='rollback', slot=slot, previous_id=previous.id,
        previous=previous.source, source=previous.previous, previous_entrypoint=previous.entrypoint,
        entrypoint=previous.previous_entrypoint)
    journal.record(change)
    return change


# This bootstrap is supplied by the management installation, never by the candidate.
# -I starts a new interpreter without ambient PYTHONPATH or preloaded Harness modules.
_BOOTSTRAP = """import importlib, pathlib, sys
root, import_root, module, function, expected, *arguments = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(root) / import_root))
sys.argv = [module, *arguments]
loaded = importlib.import_module(module)
if not getattr(loaded, '__file__', None) or pathlib.Path(loaded.__file__).resolve() != pathlib.Path(root, expected).resolve():
    raise RuntimeError('source entrypoint resolved outside the selected snapshot')
sys.exit(getattr(loaded, function)())
"""


def launch_source(base, session_id, slot, arguments, *, execute=True):
    """Pin one journal selection and give this process its own retained source copy.

    The new program keeps cwd, environment and stdio. It owns its lifetime after
    exec; no harness supervisor or arbitrary execution deadline is inserted.
    """
    base = Path(base).absolute()
    if any(p.is_symlink() for p in (base, *base.parents)):
        raise ValueError('source launch base paths must not follow symlinks')
    state = read_improvements(base, session_id)
    change = state.source_changes.get(state.active_sources.get(slot))
    if change is None:
        raise ValueError('no selected source in this slot; explicitly adopt a passing result first')
    blob_root = base / 'sessions' / str(session_id) / 'blobs'
    if any(p.is_symlink() for p in (blob_root, *blob_root.parents)):
        raise ValueError('source launch blob paths must not follow symlinks')
    snapshot = _load(BlobStore(blob_root, create=False), change.source, SourceSnapshot)
    expected = _entry_file(snapshot, change.entrypoint)
    directory = base / 'source-launches'
    if directory.is_symlink():
        raise ValueError('source launch directory must not be a symlink')
    directory.mkdir(exist_ok=True)
    tree = Path(tempfile.mkdtemp(prefix='launch-', dir=directory))
    try:
        for path, value in snapshot.files.items():
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value.bytes())
            target.chmod(0o755 if value.executable else 0o644)
        command = [sys.executable, '-I', '-B', '-c', _BOOTSTRAP, str(tree),
                   change.entrypoint.import_root, change.entrypoint.module,
                   change.entrypoint.function, expected, *arguments]
        if not execute:
            return command, tree
        from harness.telemetry import _safe
        print(_safe(f'Source launch {slot}: change={change.id}; snapshot={change.source.sha256}; '
                    f'tree={tree}'), file=sys.stderr, flush=True)
        os.execv(sys.executable, command)
    except BaseException:
        shutil.rmtree(tree)
        raise


def main(argv):
    from harness.blobs import BlobIntegrityError, MissingBlobError
    from harness.log import TornLogError
    parser = argparse.ArgumentParser(prog='harness run-source', description=__doc__)
    parser.add_argument('--base-dir', type=Path, default=Path.home() / '.local/share/harness')
    parser.add_argument('session_id')
    parser.add_argument('slot')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    try:
        launch_source(args.base_dir, SessionId(args.session_id), args.slot, arguments)
    except TornLogError:
        raise SystemExit('source launch refused: journal has an incomplete record; resume the improvement '
                         'session with the management installation to recover it, then retry') from None
    except (ValueError, OSError, BlobIntegrityError, MissingBlobError) as exc:
        raise SystemExit(f'source launch refused: {exc}') from None
