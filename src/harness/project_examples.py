"""Discover same-repository episodes in Saoirse's native session journals."""

import asyncio
import os
import re
import stat

from harness.events import AgentRunStarted, SessionStarted
from harness.log import TornLogError
from harness.project_binding import current_project, project_key, same_project

_AUTOMATIC_BYTES = 2 * 1024 * 1024
_INDEX_TAIL_BYTES = 4096


def projected_scope(project, event):
    if isinstance(event, SessionStarted):
        return event.project if event.parent_session_id is None and project_key(event.project) else None
    if isinstance(event, AgentRunStarted) and event.parent_run_id is None:
        if not same_project(event.capabilities.get("project"), project):
            return None
    return project


def _index_descriptor(base, project, *, write=False):
    # The index holds only immutable source locations. All evidence is
    # reconstructed from the native journal and checked against its own scope.
    directory = base / "project-sessions"
    if write:
        directory.mkdir(exist_ok=True, mode=0o700)
    parent = os.open(directory, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        name = project_key(project)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT if write else os.O_RDONLY
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("A native project session index must be a regular file.")
        return fd
    finally:
        os.close(parent)


def index_prefix(base, project, session_id, size, seq):
    if size > _AUTOMATIC_BYTES or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return
    line = f"{session_id} {size} {seq}\n".encode()
    # One O_APPEND write allows concurrent sessions to add whole references.
    with os.fdopen(_index_descriptor(base, project, write=True), "ab", buffering=0) as stream:
        stream.write(line)


def _prior_prefixes(base, project, current_id):
    with os.fdopen(_index_descriptor(base, project), "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - _INDEX_TAIL_BYTES))
        if size > _INDEX_TAIL_BYTES:
            stream.readline(_INDEX_TAIL_BYTES)
        lines = stream.read(_INDEX_TAIL_BYTES).splitlines(keepends=True)
    seen = {current_id}
    for line in reversed(lines):
        match = re.fullmatch(rb"([A-Za-z0-9_-]{1,128}) ([1-9][0-9]{0,18}) ([1-9][0-9]{0,18})\n", line)
        if not match:
            continue
        source = match[1].decode()
        if source not in seen:
            seen.add(source)
            yield source, int(match[2]), int(match[3])
            if len(seen) > 16:
                break


def _examples(base, project, current_id):
    from harness.improvement_examples import session_examples
    examples = []
    remaining = _AUTOMATIC_BYTES
    try:
        for source, size, seq in _prior_prefixes(base, project, current_id):
            if size > remaining:
                continue
            remaining -= size
            try:
                values = session_examples(base, source, project=project, max_bytes=size, prefix=(size, seq))
            except (OSError, ValueError, TornLogError):
                continue
            examples.extend(values[:16 - len(examples)])
            if len(examples) == 16:
                break
    except (OSError, ValueError):
        pass
    return tuple(examples)


async def project_examples(loop, *, project=None):
    if loop.dispatcher.scope.depth:
        return ()
    project = project or await current_project(loop.workspace_root)
    if not project_key(project):
        return ()
    return await asyncio.to_thread(_examples, loop.session.base, project, loop.session.id)
