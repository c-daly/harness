"""Replay provenance records the effective command before trying the runtime."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import replay_context_selection as replay


def test_report_freezes_effective_runtime_command(tmp_path, monkeypatch):
    model_file = tmp_path / "fixture.gguf"
    model_file.write_bytes(b"test weights")
    profiles = deepcopy(replay.MODEL_PROFILES)
    weights = profiles["qwen3-8b"]
    weights.update(bytes=model_file.stat().st_size, sha256=replay.sha256(model_file))
    original_weights = deepcopy(weights)
    monkeypatch.setattr(replay, "MODEL_PROFILES", profiles)
    monkeypatch.setattr(replay, "isolation", lambda: {"test_fixture": True})
    candidate = tmp_path / "candidate.json"
    candidate.write_text(replay.public_experiments()[0].candidate.model_dump_json())
    output = tmp_path / "attempt"
    monkeypatch.setattr(sys, "argv", ["replay_context_selection.py", "--candidate", str(candidate),
        "--output", str(output), "--model-file", str(model_file)])
    read_text = Path.read_text

    def fixture_read_text(path, *args, **kwargs):
        if path == Path("/sys/fs/cgroup/memory.peak"):
            return "0"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fixture_read_text)
    frozen = {}

    def unavailable_runtime(*args, **kwargs):
        frozen.update(json.loads((output / "report.json").read_text()))
        raise RuntimeError("test runtime unavailable")

    monkeypatch.setattr(replay.subprocess, "run", unavailable_runtime)
    with pytest.raises(SystemExit) as stopped:
        replay.main()
    assert stopped.value.code == 1
    saved = json.loads((output / "report.json").read_text())
    command = json.loads((output / "catalog.json").read_text())["local-small"]["local"]["command"]
    for report in (frozen, saved):
        assert report["runtime_command"] == command
        for flag, expected in (("--presence-penalty", "0"), ("--ctx-size", "4096"),
                               ("--n-gpu-layers", "28"), ("--model", str(model_file))):
            assert report["runtime_command"][command.index(flag) + 1] == expected
        assert report["weights"] == {key: original_weights[key]
            for key in ("repo", "filename", "revision", "bytes", "sha256")}
    assert weights == original_weights
    assert saved["error_type"] == "RuntimeError"
    assert not saved["mechanics_passed"]
