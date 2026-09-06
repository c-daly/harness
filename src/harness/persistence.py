"""Durable file publication shared by session repair, blobs, and core state."""

import os
import uuid
from pathlib import Path


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, replace: bool = True) -> None:
    """Publish complete 0600 bytes, durable before their directory entry.

    Exclusive publication uses a hard link to the already-synced temporary
    file. It raises FileExistsError without replacing a prior quarantine.
    Callers own any higher-level transaction or comparison of existing bytes.
    """
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
