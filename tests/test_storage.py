import pytest

from harness.events import Envelope, SessionStarted, UserMessage
from harness.log import EventLogWriter
from harness.storage import StorageLimits, StorageLimitExceeded, load_storage_limits
from harness.types import SessionId


def _env(seq: int, event):
    return Envelope(session_id=SessionId("s1"), seq=seq, ts=float(seq), event=event)


def test_storage_defaults_and_explicit_runtime_config(tmp_path, monkeypatch):
    # defaults
    limits = load_storage_limits(config_path=tmp_path / "missing.toml")
    assert isinstance(limits, StorageLimits)
    # write a tmp runtime.toml
    cfg = tmp_path / "runtime.toml"
    cfg.write_text("""
[storage]
max_event_bytes = 1000
max_session_log_bytes = 10000
min_free_bytes = 999
""")
    loaded = load_storage_limits(config_path=cfg)
    assert loaded.max_event_bytes == 1000
    assert loaded.max_session_log_bytes == 10000
    assert loaded.min_free_bytes == 999


def test_storage_limits_enforce_event_log_and_free_space_boundaries(tmp_path, monkeypatch):
    # monkeypatch loader to set very small limits and a fake free space probe
    from harness import storage as storage_mod

    def fake_load_storage_limits(config_path=None):
        # Allow small metadata events but fail on a larger user message payload.
        return StorageLimits(max_event_bytes=512, max_session_log_bytes=256, min_free_bytes=10)

    def fake_probe(path):
        return 100  # above threshold initially

    monkeypatch.setattr(storage_mod, "load_storage_limits", fake_load_storage_limits)
    monkeypatch.setattr(storage_mod, "_probe_free_bytes", fake_probe)

    # Writer picks up limits on init
    with EventLogWriter(tmp_path, SessionId("s1")) as w:
        w.append(_env(1, SessionStarted()))
        # Oversized event fails early with StorageLimitExceeded (terminal, small) and leaves prior bytes intact
        before = (tmp_path / "sessions" / "s1.jsonl").read_bytes()
        big_text = "x" * 1000
        with pytest.raises(StorageLimitExceeded):
            w.append(_env(2, UserMessage(text=big_text)))
        # journal not grown by refused append
        assert (tmp_path / "sessions" / "s1.jsonl").read_bytes() == before

    # Switch to low free space to trigger StorageLimitExceeded on new writer
    def low_free(path):
        return 0

    monkeypatch.setattr(storage_mod, "_probe_free_bytes", low_free)

    with EventLogWriter(tmp_path, SessionId("s2")) as w2:
        # First append checks free space and refuses with typed error
        with pytest.raises(StorageLimitExceeded):
            w2.append(_env(1, SessionStarted()))
