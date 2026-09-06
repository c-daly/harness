"""Content-addressed blob sidecar. Log + blobs = the unit of truth.

Payloads over INLINE_THRESHOLD bytes (and all binary content) live here;
events reference them by BlobRef. Fold passes BlobRefs through untouched;
dereferencing (and loud MissingBlobError failure) happens in consumers that
need the bytes — provider adapters and telemetry, not the fold.
"""

import hashlib
import os
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from harness.persistence import atomic_write

INLINE_THRESHOLD = 16 * 1024  # bytes; payloads above this spill to the sidecar


class MissingBlobError(Exception):
    """A BlobRef points at content the sidecar does not have. Replay must not guess."""


class BlobIntegrityError(RuntimeError):
    """Stored bytes do not match their content-addressed reference."""


class BlobRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        path = self._root / digest
        ref = BlobRef(sha256=digest, size=len(data))
        try:
            atomic_write(path, data, replace=False)
        except FileExistsError:
            self.get(ref)  # Never reuse or replace a corrupt existing object.
        return ref

    def get(self, ref: BlobRef) -> bytes:
        # model_copy/model_construct can bypass Pydantic's construction checks.
        ref = BlobRef.model_validate(ref.model_dump())
        path = self._root / ref.sha256
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise MissingBlobError(ref) from None
        except OSError as exc:
            raise BlobIntegrityError(f"blob integrity: cannot open {ref.sha256}") from exc
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != ref.size:
                raise BlobIntegrityError(f"blob integrity: invalid size or type for {ref.sha256}")
            data = source.read(ref.size + 1)
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise BlobIntegrityError(f"blob integrity: digest or size mismatch for {ref.sha256}")
        return data
