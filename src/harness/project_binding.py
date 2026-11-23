"""Identify a workspace locally and read only its own native session records."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from harness.events import AgentRunStarted, SessionStarted, parse_envelope_line
from harness.log import TornLogError, _scan, _session_directory


def project_key(project):
    key = project.get("git_common_dir") if isinstance(project, dict) else None
    if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key):
        return key
    return None


def same_project(left, right):
    if left is None or right is None:
        return left is right
    key = project_key(left)
    return key is not None and key == project_key(right)


def _repository(workspace):
    # Ask Git about this workspace, including linked worktrees and subdirectories.
    # Ambient GIT_DIR/GIT_WORK_TREE must not substitute an unrelated checkout.
    git = subprocess.run(["git", "--no-replace-objects", "-C", workspace, "rev-parse", "--path-format=absolute",
                          "--git-common-dir", "--is-inside-work-tree"],
                         env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                         capture_output=True, timeout=5, check=False)
    if git.returncode:
        return None
    lines = git.stdout.decode().splitlines()
    if len(lines) != 2 or lines[1] != "true":
        return None
    common = Path(lines[0]).resolve(strict=True)
    info = common.stat()
    if not stat.S_ISDIR(info.st_mode) or not os.access(common, os.R_OK | os.X_OK):
        return None
    identity = json.dumps([str(common), info.st_dev, info.st_ino]).encode()
    return {"git_common_dir": hashlib.sha256(identity).hexdigest()}


async def current_project(workspace):
    if workspace is None:
        return None
    try:
        return await asyncio.to_thread(_repository, workspace)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def read_project_session(base, session_id, project, *, max_bytes=None, prefix=None):
    """Check the native root header before reading a bounded prefix on the same descriptor."""
    path = _session_directory(base, session_id) / f"{session_id}.jsonl"
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return []
        size = prefix[0] if prefix is not None else info.st_size
        if size <= 0 or size > info.st_size:
            return []
        header = stream.readline(min(size, 16385))
        if len(header) > 16384 or not header.endswith(b"\n"):
            return []
        first = parse_envelope_line(header.decode())
        if (first.session_id != session_id or first.seq != 1 or not isinstance(first.event, SessionStarted)
                or first.event.parent_session_id is not None or not same_project(first.event.project, project)):
            return []
        if max_bytes is not None and size > max_bytes:
            raise ValueError("Project experience exceeds the automatic history byte limit.")
        if prefix is not None:
            # A forged/stale index entry must not extend a single-project prefix
            # into a resumed turn from another project. Stop at its root start.
            envelopes = [first]
            while stream.tell() < size:
                line = stream.readline(size - stream.tell())
                if not line.endswith(b"\n"):
                    raise TornLogError("The source experience has an incomplete native event.")
                env = parse_envelope_line(line.decode())
                if env.session_id != session_id or env.seq <= envelopes[-1].seq:
                    return []
                event = env.event
                if isinstance(event, SessionStarted):
                    return []
                if isinstance(event, AgentRunStarted) and event.parent_run_id is None and not same_project(
                        event.capabilities.get("project"), project):
                    return []
                envelopes.append(env)
            return envelopes if envelopes[-1].seq == prefix[1] else []
        tail = stream.read(size - len(header))
    events, offset = _scan(header + tail, session_id)
    if offset is not None:
        raise TornLogError("The source experience has an incomplete or invalid native journal.")
    return events
