import hashlib

import pytest

from harness.blobs import INLINE_THRESHOLD, BlobRef, BlobStore, MissingBlobError


def test_put_returns_content_addressed_ref(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"x" * 100
    ref = store.put(data)
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.size == 100


def test_get_round_trips(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    ref = store.put(b"hello world")
    assert store.get(ref) == b"hello world"


def test_put_is_idempotent(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    assert store.put(b"same") == store.put(b"same")


def test_missing_blob_fails_loudly(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    with pytest.raises(MissingBlobError):
        store.get(BlobRef(sha256="0" * 64, size=1))


def test_inline_threshold_is_sane():
    assert 1024 <= INLINE_THRESHOLD <= 64 * 1024
