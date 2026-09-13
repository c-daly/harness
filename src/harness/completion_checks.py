"""Explicit host command checks for a Git workspace; never registered as tools.

Commands and candidate code run with operator authority. Pinned verifier files
and workspace digests detect drift; they are not isolation from hostile code.
"""

import hashlib
import json
import os
from pathlib import Path
import stat

from pydantic import Field

from harness.completion import (
    _Data,
    CheckObservation,
    CompletionCheck,
    CompletionObservation,
)
from harness.tasks import Digest


class CommandCheck(CompletionCheck):
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    timeout_seconds: float = Field(default=120, gt=0, le=1800)


class CommandChecks(_Data):
    checks: tuple[CommandCheck, ...] = Field(min_length=1, max_length=32)
    # Explicit environment only: API keys and unrelated host variables are not
    # inherited by check subprocesses. The host can supply PYTHONPATH for code.
    environment: dict[str, str] = Field(default_factory=dict, max_length=64)
    files: dict[str, Digest] = Field(min_length=1, max_length=64)
    max_output_bytes: int = Field(default=65536, ge=1024, le=1048576, strict=True)


async def _command(argv, *, cwd, env, timeout=30, limit=1048576):
    # Reuse the tested process-group cleanup and raw-output bound already used
    # for operator-controlled source evaluations.
    from harness.source_improvement import _process

    return await _process(argv, cwd=cwd, env=env, timeout=timeout, limit=limit)


def _regular_bytes(path, *, limit):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError(f"symbolic links are not supported in verification inputs: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError(f"verification input is not a bounded regular file: {path}")
        data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError("verification input exceeds its byte limit")
    return data, stat.S_IMODE(metadata.st_mode)


async def workspace_digest(workspace):
    """Hash HEAD, names, modes and bytes of tracked and nonignored new files.

    Gitignored caches are outside this evidence claim. Deleted tracked files
    remain represented. Repositories with symlinks/submodules require a different
    explicitly supplied identity function, rather than silently following them.
    """
    workspace = Path(workspace).resolve(strict=True)
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    async def git(*args):
        result = await _command(["git", *args], cwd=workspace, env=env)
        if result["status"] != "completed" or result["returncode"] != 0:
            raise ValueError("cannot establish Git workspace identity")
        return result["stdout"]

    digest = hashlib.sha256(await git("rev-parse", "HEAD"))
    names = set(
        (await git("ls-files", "-z", "--cached", "--others", "--exclude-standard")).split(b"\0")
    ) - {b""}
    if len(names) > 16384:
        raise ValueError("workspace identity exceeds 16384 files")
    remaining = 256 * 1024 * 1024
    for name in sorted(names):
        relative = Path(os.fsdecode(name))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Git returned a path outside the workspace")
        path = workspace / relative
        try:
            data, mode = _regular_bytes(path, limit=min(remaining, 32 * 1024 * 1024))
            remaining -= len(data)
            entry = [os.fsdecode(name), mode, hashlib.sha256(data).hexdigest()]
        except FileNotFoundError:
            entry = [os.fsdecode(name), "missing"]
        digest.update(json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
    return digest.hexdigest()


class CommandVerifier:
    def __init__(self, specification, *, workspace, blobs):
        self.specification = CommandChecks.model_validate(specification.model_dump())
        self.workspace = Path(workspace).resolve(strict=True)
        self.blobs = blobs
        self._check_files()

    def _check_files(self):
        for name, expected in self.specification.files.items():
            path = Path(name)
            if not path.is_absolute() or path.resolve(strict=True).is_relative_to(self.workspace):
                raise ValueError(
                    "pinned verifiers must be absolute paths outside the candidate workspace"
                )
            data, _ = _regular_bytes(path, limit=4 * 1024 * 1024)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"pinned verifier changed: {path}")

    async def identify(self):
        self._check_files()
        return await workspace_digest(self.workspace)

    async def __call__(self):
        self._check_files()
        digest = await workspace_digest(self.workspace)
        observations = []
        for check in self.specification.checks:
            self._check_files()
            env = {"PATH": os.defpath, **self.specification.environment}
            try:
                result = await _command(
                    check.argv,
                    cwd=self.workspace,
                    env=env,
                    timeout=check.timeout_seconds,
                    limit=self.specification.max_output_bytes,
                )
                self._check_files()
                result = {
                    **result,
                    "stdout": result["stdout"].decode("utf-8", errors="replace"),
                    "stderr": result["stderr"].decode("utf-8", errors="replace"),
                }
                status = (
                    "error"
                    if result["status"] != "completed"
                    else "passed"
                    if result["returncode"] == 0
                    else "failed"
                )
                artifact = self.blobs.put(json.dumps(result).encode())
                summary = (
                    f"{check.description}\n{result['status']}; exit={result['returncode']}\n"
                    + result["stdout"]
                    + "\n"
                    + result["stderr"]
                )[-8192:]
                observations.append(
                    CheckObservation(id=check.id, status=status, summary=summary, artifact=artifact)
                )
            except (OSError, ValueError) as exc:
                observations.append(
                    CheckObservation(id=check.id, status="error", summary=str(exc)[:8192])
                )
                # Retain a result for every requirement, even when grading is held.
        return CompletionObservation(workspace_sha256=digest, checks=tuple(observations))
