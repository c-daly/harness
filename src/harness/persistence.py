"""Durable file publication shared by session repair, blobs, and core state."""

import ctypes
import errno
import os
import uuid
from pathlib import Path

_AT_FDCWD = -100
_RENAME_EXCHANGE = 2


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exchange_paths(first: Path, second: Path) -> None:
    """Atomically exchange two existing names, retaining both files.

    Linux/WSL2 renameat2(RENAME_EXCHANGE); fail closed when unsupported. In
    particular, never emulate this with a sequence of destructive renames.
    Callers must retain the displaced name and sync its directory on success.
    """
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError(errno.ENOTSUP, "Atomic file exchange is unavailable") from None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(_AT_FDCWD, os.fsencode(first), _AT_FDCWD, os.fsencode(second), _RENAME_EXCHANGE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(second))


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
