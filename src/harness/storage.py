"""Storage limits and runtime configuration for session logging.

Exposes typed limits used by the event log writer to refuse oversized events
and to stop before exhausting free space. Configuration is loaded from
~/.config/harness/runtime.toml under a [storage] table.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:  # Python 3.11+
    import tomllib as _toml
except Exception:  # pragma: no cover - fallback for older interpreters
    _toml = None  # type: ignore[assignment]


class StorageLimitExceeded(RuntimeError):
    """A storage boundary would be crossed by starting a new turn."""


@dataclass(frozen=True)
class StorageLimits:
    max_event_bytes: int = 1_048_576
    max_session_log_bytes: int = 1_073_741_824
    min_free_bytes: int = 134_217_728

    def check_event_size(self, size: int) -> None:
        if size > self.max_event_bytes:
            # Typed boundary failure used by appenders and tests.
            raise StorageLimitExceeded("event exceeds maximum size")

    def check_session_log_and_free_space(
        self,
        *,
        log_path: Path,
        next_event_bytes: int,
        probe_free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        # Refuse to grow the log past the configured cap.
        try:
            current = log_path.stat().st_size
        except FileNotFoundError:
            current = 0
        if current + next_event_bytes > self.max_session_log_bytes:
            raise StorageLimitExceeded("session log size limit exceeded")
        # Refuse when free space is below the guard-rail.
        probe = probe_free_bytes or _probe_free_bytes
        free = probe(log_path.parent)
        if free < self.min_free_bytes:
            raise StorageLimitExceeded("insufficient free space for a new turn")


def _probe_free_bytes(path: Path) -> int:
    """Return filesystem free bytes for the partition backing `path`.

    Split out for tests to monkeypatch.
    """
    import os

    st = os.statvfs(str(path))
    return int(st.f_bavail) * int(st.f_frsize)


def load_storage_limits(config_path: Path | None = None) -> StorageLimits:
    """Load [storage] limits from the runtime.toml if present.

    Unknown keys are ignored. Missing file falls back to defaults.
    """
    if config_path is None:
        config_path = Path.home() / ".config/harness/runtime.toml"
    try:
        raw = config_path.read_bytes()
    except OSError:
        return StorageLimits()
    if _toml is None:
        return StorageLimits()
    try:
        doc = _toml.loads(raw.decode("utf-8"))
    except Exception:
        return StorageLimits()
    storage = doc.get("storage") or {}
    return StorageLimits(
        max_event_bytes=int(storage.get("max_event_bytes", StorageLimits.max_event_bytes)),
        max_session_log_bytes=int(
            storage.get("max_session_log_bytes", StorageLimits.max_session_log_bytes)
        ),
        min_free_bytes=int(storage.get("min_free_bytes", StorageLimits.min_free_bytes)),
    )
