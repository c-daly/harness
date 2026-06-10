"""Content-addressed blob sidecar. Log + blobs = the unit of truth.

Payloads over INLINE_THRESHOLD bytes (and all binary content) live here;
events reference them by BlobRef. Fold operations fail loudly on missing blobs.
"""

import hashlib
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict

INLINE_THRESHOLD = 16 * 1024  # bytes; payloads above this spill to the sidecar


class MissingBlobError(Exception):
    """A BlobRef points at content the sidecar does not have. Replay must not guess."""


class BlobRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    sha256: str
    size: int


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        path = self._root / digest
        if not path.exists():
            tmp = self._root / f"{digest}.{uuid.uuid4().hex}.tmp"
            tmp.write_bytes(data)
            tmp.rename(path)  # atomic publish; racing writers replace identical bytes
        return BlobRef(sha256=digest, size=len(data))

    def get(self, ref: BlobRef) -> bytes:
        path = self._root / ref.sha256
        if not path.exists():
            raise MissingBlobError(ref)
        return path.read_bytes()
