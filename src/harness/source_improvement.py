"""Operator-controlled paired source experiments, independent of model providers.

Copies separate edits; they are not a security sandbox. Executable checks and
candidate code run with the operator's OS authority. No result activates code.
"""

import asyncio
import base64
import difflib
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.blobs import BlobRef
from harness.events import EvaluationRunFinished, EvaluationRunStarted
from harness.fold import fold
from harness.improvement import Candidate, Digest, EvaluationCase, EvaluationPlan, ExperimentResult, Measurement
from harness.improvement_journal import read_improvements
from harness.log import read_session

MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 24 * 1024 * 1024
MAX_FILES = 2048


class _Data(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def _path(value):
    parts = value.split("/")
    if (not value or len(value.encode()) > 1024 or "\\" in value
            or any(p in {"", ".", ".."} or p.lower() == ".git" for p in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("source paths must be relative files without traversal or Git metadata")
    return value


class SourceFile(_Data):
    content: str = Field(max_length=MAX_ARTIFACT_BYTES)  # canonical base64; binary files are portable
    executable: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def valid_content(self):
        try:
            data = base64.b64decode(self.content, validate=True)
        except ValueError as exc:
            raise ValueError("source content must be canonical base64") from exc
        if len(data) > MAX_TREE_BYTES or base64.b64encode(data).decode() != self.content:
            raise ValueError("source content exceeds its limit or is not canonical base64")
        return self

    def bytes(self):
        return base64.b64decode(self.content, validate=True)


class SourceSnapshot(_Data):
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    files: dict[str, SourceFile] = Field(max_length=MAX_FILES)

    @model_validator(mode="after")
    def safe_tree(self):
        for path in self.files:
            _path(path)
            if any(str(p) in self.files for p in PurePosixPath(path).parents):
                raise ValueError("source files cannot overlap directory paths")
        if sum(len(f.bytes()) for f in self.files.values()) > MAX_TREE_BYTES:
            raise ValueError("source snapshot exceeds 16 MiB")
        return self


class SourceEdit(_Data):
    path: str
    before: Digest | None = None
    after: SourceFile | None = None


class SourcePatch(_Data):
    format: Literal["harness-source-patch-v1"] = "harness-source-patch-v1"
    version: int = Field(default=1, ge=1, le=1, strict=True)
    incumbent: BlobRef
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    edits: tuple[SourceEdit, ...] = Field(min_length=1, max_length=MAX_FILES * 2)


class SourceCase(EvaluationCase):
    script: str = Field(min_length=1, max_length=32768)


class SourceSuite(_Data):
    format: Literal["harness-source-suite-v1"] = "harness-source-suite-v1"
    version: int = Field(default=1, ge=1, le=1, strict=True)
    cases: tuple[SourceCase, ...] = Field(min_length=2, max_length=32)
    case_timeout_seconds: float = Field(default=30, gt=0, le=300)
    timeout_seconds: float = Field(default=600, gt=0, le=3600)
    max_output_bytes: int = Field(default=65536, ge=256, le=1024 * 1024, strict=True)
    min_improved_cases: int = Field(default=1, ge=1, strict=True)
    min_held_out_correct: int = Field(default=1, ge=1, strict=True)
    min_held_out_improved: int = Field(default=0, ge=0, strict=True)
    max_latency_ratio: float = Field(default=1.2, gt=0)
    max_case_latency_ms: float = Field(default=30000, gt=0)

    def plan_fields(self):
        return {**self.model_dump(include={"min_improved_cases", "min_held_out_correct",
                "min_held_out_improved", "max_latency_ratio", "max_case_latency_ms"}),
                "cases": tuple(EvaluationCase.model_validate(c.model_dump(exclude={"script"}))
                               for c in self.cases)}


class SourceProposal(SourceSuite):
    repository: str = Field(min_length=1, max_length=4096)
    incumbent_revision: str = Field(min_length=1, max_length=256)
    candidate_revision: str = Field(min_length=1, max_length=256)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    hypothesis: str = Field(min_length=1, max_length=4096)
    expected_benefit: str = Field(min_length=1, max_length=4096)


def _encoded(data):
    return json.dumps(data.model_dump(mode="json"), sort_keys=True, allow_nan=False).encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _load(blobs, ref, model):
    if ref.size > MAX_ARTIFACT_BYTES:
        raise ValueError("source artifact exceeds 24 MiB")
    return model.model_validate_json(blobs.get(ref))


def load_patch(blobs, artifact):
    patch = _load(blobs, artifact, SourcePatch)
    incumbent = _load(blobs, patch.incumbent, SourceSnapshot)
    files = dict(incumbent.files)
    seen = set()
    for edit in patch.edits:
        _path(edit.path)
        if edit.path in seen:
            raise ValueError("source patch repeats a path")
        seen.add(edit.path)
        before = files.get(edit.path)
        if edit.before != (_digest(_encoded(before)) if before else None) or before == edit.after:
            raise ValueError("source patch does not match its incumbent or contains an empty edit")
        if edit.after is None:
            files.pop(edit.path)
        else:
            files[edit.path] = edit.after
    candidate = SourceSnapshot(revision=patch.revision, files=files)
    return patch, incumbent, candidate


def is_source_patch(blobs, artifact):
    """Older generic code candidates stay inspectable without assuming a schema."""
    if artifact.size > MAX_ARTIFACT_BYTES:
        return False
    try:
        data = json.loads(blobs.get(artifact))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("format") == "harness-source-patch-v1"


def inspect_patch(blobs, artifact):
    patch, incumbent, candidate = load_patch(blobs, artifact)
    lines = ["Source patch (data; no activation):", patch.model_dump_json(exclude={"edits"}, indent=2)]
    for edit in patch.edits:
        left, right = incumbent.files.get(edit.path), candidate.files.get(edit.path)
        lines.append(f"File: {edit.path}; executable: {left.executable if left else None} -> "
                     f"{right.executable if right else None}")
        try:
            before = left.bytes().decode("utf-8").splitlines(keepends=True) if left else []
            after = right.bytes().decode("utf-8").splitlines(keepends=True) if right else []
        except UnicodeDecodeError:
            lines.append("Binary content changed; exact bytes retained in the patch artifact.")
        else:
            lines.append("".join(difflib.unified_diff(before, after, fromfile=f"a/{edit.path}",
                                                   tofile=f"b/{edit.path}")))
    text = "\n".join(lines)
    if len(text) > 65536:
        text = text[:65536] + "\nPatch preview truncated at 65536 characters; full artifact retained."
    return text


async def _settle(task):
    """Do not release owned processes/directories on repeated cancellation."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


class _Output(asyncio.SubprocessProtocol):
    """Bound bytes at receipt, without a paused StreamReader blocking process reaping."""

    def __init__(self, limit):
        loop = asyncio.get_running_loop()
        self.done, self.exited = loop.create_future(), loop.create_future()
        self.output = {1: bytearray(), 2: bytearray()}
        self.open_pipes = {1, 2}
        self.limit, self.total = limit, 0

    def pipe_data_received(self, fd, data):
        self.output[fd].extend(data[:max(0, self.limit - self.total)])
        self.total += len(data)
        if self.total > self.limit and not self.done.done():
            self.done.set_result("output_limit")

    def _finish(self):
        if not self.open_pipes and self.exited.done() and not self.done.done():
            self.done.set_result("completed")

    def pipe_connection_lost(self, fd, exc):
        self.open_pipes.discard(fd)
        if exc is not None and fd in {1, 2} and not self.done.done():
            self.done.set_result("io_error")
        self._finish()

    def process_exited(self):
        self.exited.set_result(None)
        self._finish()


async def _process(argv, *, cwd, env, timeout, limit, data=None):
    """Bound combined raw output, reap the leader and kill its group on every exit."""
    started = time.monotonic()
    protocol = _Output(limit)
    launch = asyncio.create_task(asyncio.get_running_loop().subprocess_exec(lambda: protocol,
        *argv, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True))
    transport = None
    status = "completed"

    async def cleanup():
        nonlocal transport
        if transport is None:
            transport, _ = await launch
        try:
            os.killpg(transport.get_pid(), signal.SIGKILL)
        except ProcessLookupError:
            pass
        transport.close()  # also close pipes inherited by a detached, unowned process
        await protocol.exited

    try:
        async with asyncio.timeout(timeout):
            transport, _ = await asyncio.shield(launch)
            if data is not None:
                transport.get_pipe_transport(0).write(data)
                transport.get_pipe_transport(0).close()
            status = await asyncio.shield(protocol.done)
    except TimeoutError:
        status = "timed_out"
    finally:
        await _settle(asyncio.create_task(cleanup()))
    return {"status": status, "returncode": transport.get_returncode(),
            "duration_ms": (time.monotonic() - started) * 1000,
            "stdout": bytes(protocol.output[1]), "stderr": bytes(protocol.output[2])}


async def _git(repo, *args, data=None, limit=MAX_ARTIFACT_BYTES):
    env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "LC_ALL": "C"}
    result = await _process(["git", "--no-replace-objects", "-C", str(repo), *args],
                            cwd=repo, env=env, timeout=30, limit=limit, data=data)
    if result["status"] != "completed" or result["returncode"] != 0:
        raise ValueError(f"cannot read committed source: Git {args[0]} status={result['status']}, "
                         f"exit={result['returncode']}; verify the local repository/revisions "
                         "and provision missing commit objects locally")
    return result["stdout"]


async def _snapshot(repo, revision):
    oid = (await _git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}",
                      limit=1024)).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise ValueError("source revision must identify one commit")
    listing = await _git(repo, "ls-tree", "-rz", "--full-tree", oid, limit=1024 * 1024)
    entries = []
    for row in listing.split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("source snapshots refuse symlinks and submodules; use regular tracked files")
        entries.append((_path(raw_path.decode("utf-8")), mode, object_id))
    if len(entries) > MAX_FILES:
        raise ValueError("source snapshot exceeds 2048 files")
    objects = await _git(repo, "cat-file", "--batch", data="".join(e[2] + "\n" for e in entries).encode())
    offset, files = 0, {}
    for path, mode, oid_expected in entries:
        end = objects.index(b"\n", offset)
        object_id, kind, size = objects[offset:end].decode().split()
        size = int(size)
        offset = end + 1
        content = objects[offset:offset + size]
        if (object_id != oid_expected or kind != "blob" or len(content) != size
                or objects[offset + size:offset + size + 1] != b"\n"):
            raise ValueError("incomplete source object stream")
        offset += size + 1
        files[path] = SourceFile(content=base64.b64encode(content).decode(), executable=mode == "100755")
    if offset != len(objects):
        raise ValueError("unexpected source object stream")
    return SourceSnapshot(revision=oid, files=files)


def evaluator_version(suite):
    source = Path(__file__).parent
    return _digest(json.dumps({"configuration": suite.model_dump(mode="json"),
        "python": sys.version, "executable": sys.executable,
        "implementation": {name: _digest((source / name).read_bytes()) for name in
            ("source_improvement.py", "improvement.py", "improvement_journal.py", "blobs.py")}},
        sort_keys=True).encode())


async def prepare_source(journal, proposal):
    proposal = SourceProposal.model_validate(proposal.model_dump())
    state = read_improvements(journal.session.base, journal.session.id)
    if any(key not in state.evidence for key in proposal.evidence_ids):
        raise ValueError("source candidate requires existing improvement evidence IDs")
    suite = SourceSuite.model_validate(proposal.model_dump(include=set(SourceSuite.model_fields)))
    repo = Path(proposal.repository).absolute()
    # Refuse symlink traversal even though only Git objects, not worktree files, are read.
    if any(p.is_symlink() for p in (repo, *repo.parents)):
        raise ValueError("source repository paths must not follow symlinks")
    incumbent = await _snapshot(repo, proposal.incumbent_revision)
    changed = await _snapshot(repo, proposal.candidate_revision)
    blobs = journal.session.blobs
    incumbent_ref = blobs.put(_encoded(incumbent))
    edits = tuple(SourceEdit(path=path,
        before=_digest(_encoded(incumbent.files[path])) if path in incumbent.files else None,
        after=changed.files.get(path))
        for path in sorted(incumbent.files.keys() | changed.files.keys())
        if incumbent.files.get(path) != changed.files.get(path))
    patch = SourcePatch(incumbent=incumbent_ref, revision=changed.revision, edits=edits)
    artifact = blobs.put(_encoded(patch))
    load_patch(blobs, artifact)
    candidate = Candidate(id=str(uuid4()), target="code", incumbent_version=incumbent_ref.sha256,
        artifact=artifact, evidence_ids=proposal.evidence_ids, hypothesis=proposal.hypothesis,
        expected_benefit=proposal.expected_benefit)
    plan = EvaluationPlan(id=str(uuid4()), candidate_id=candidate.id,
        incumbent_version=candidate.incumbent_version, candidate_version=artifact.sha256,
        suite=blobs.put(_encoded(suite)), evaluator_version=evaluator_version(suite), **suite.plan_fields())
    journal.record(candidate)
    journal.record(plan)
    return plan


def _materialize(root, snapshot, case):
    tree = root / "source"
    tree.mkdir()
    for path, value in snapshot.files.items():
        destination = tree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value.bytes())
        destination.chmod(0o755 if value.executable else 0o644)
    (root / "check.py").write_text(case.script, encoding="utf-8")
    for name in ("home", "tmp"):
        (root / name).mkdir()


async def _run_check(snapshot, case, suite):
    temporary = tempfile.TemporaryDirectory(prefix="harness-source-")
    root = Path(temporary.name)
    try:
        # Keep the actual executor future alive through cancellation, so cleanup
        # cannot race a thread still populating this directory.
        await _settle(asyncio.get_running_loop().run_in_executor(None, _materialize, root, snapshot, case))
        env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
               "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"), "LC_ALL": "C.UTF-8",
               "GIT_CEILING_DIRECTORIES": str(root)}
        result = await _process([sys.executable, "-I", "-B", str(root / "check.py")], cwd=root / "source", env=env,
                               timeout=suite.case_timeout_seconds, limit=suite.max_output_bytes)
        return {**result, "stdout": result["stdout"].decode("utf-8", errors="replace"),
                "stderr": result["stderr"].decode("utf-8", errors="replace")}
    finally:
        await _settle(asyncio.get_running_loop().run_in_executor(None, temporary.cleanup))


async def run_source_evaluation(journal, plan_id):
    session = journal.session
    state = read_improvements(session.base, session.id)
    if plan_id not in state.plans:
        raise ValueError("unknown source evaluation plan; prepare a source candidate first")
    plan = state.plans[plan_id]
    candidate = state.candidates[plan.candidate_id]
    if candidate.target != "code":
        raise ValueError("source evaluation requires a code candidate")
    patch, incumbent, changed = load_patch(session.blobs, candidate.artifact)
    suite = _load(session.blobs, plan.suite, SourceSuite)
    version = evaluator_version(suite)
    expected = EvaluationPlan(**{**plan.model_dump(), **suite.plan_fields()})
    if (plan != expected or patch.incumbent.sha256 != plan.incumbent_version
            or version != plan.evaluator_version):
        raise ValueError("source evaluator or fixed suite differs from the recorded plan; prepare a new plan")
    if fold(read_session(session.base, session.id)).open_evaluations:
        raise ValueError("an evaluation is already running; resume interrupted records before retrying")
    run_id = str(uuid4())
    session.append(EvaluationRunStarted(run_id=run_id, plan_id=plan.id,
                                       incumbent=patch.incumbent, configuration=plan.suite))
    rows = {case.id: {} for case in suite.cases}
    status = "failed"
    try:
        async with asyncio.timeout(suite.timeout_seconds):
            for index, case in enumerate(suite.cases):
                order = ("incumbent", "candidate") if index % 2 == 0 else ("candidate", "incumbent")
                for side in order:
                    if evaluator_version(suite) != version:
                        raise ValueError("source evaluator changed during the experiment")
                    rows[case.id][side] = await _run_check(
                        incumbent if side == "incumbent" else changed, case, suite)
                    if evaluator_version(suite) != version:
                        raise ValueError("source evaluator changed during the experiment")
        status = "completed"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except TimeoutError:
        status = "timed_out"
    finally:
        observations = []
        for case in suite.cases:
            values = {}
            for side in ("incumbent", "candidate"):
                row = rows[case.id].get(side)
                values[f"{side}_passed"] = (row["status"] == "completed" and row["returncode"] == 0
                                             if row else None)
                values[f"{side}_latency_ms"] = row["duration_ms"] if row else None
            observations.append(Measurement(case_id=case.id, **values))
        report = {"schema_version": 1, "function": "source_patch", "run_id": run_id,
            "completion": status, "activation_qualified": False,
            "isolation": "fresh_directory_not_security_sandbox", "held_out_provenance": "operator_declared",
            "incumbent_revision": incumbent.revision, "candidate_revision": changed.revision,
            "candidate_tree_sha256": _digest(_encoded(changed)), "evaluator_version": version,
            "suite": plan.suite.model_dump(), "python": sys.version, "executable": sys.executable,
            "cases": [{**case.model_dump(), **{side: rows[case.id].get(side)
                       for side in ("incumbent", "candidate")}} for case in suite.cases]}
        result = ExperimentResult(id=str(uuid4()), plan_id=plan.id, run_id=run_id, completion=status,
            incumbent_version=plan.incumbent_version, candidate_version=plan.candidate_version,
            evaluator_version=version, observations=tuple(observations),
            artifact=session.blobs.put(json.dumps(report, sort_keys=True, allow_nan=False).encode()))
        journal.record(result)
        session.append(EvaluationRunFinished(run_id=run_id, status=status, result_id=result.id))
    return result
