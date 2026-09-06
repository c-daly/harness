"""Subject validation must precede workspace setup or memory-plugin access."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def checker():
    path = Path(__file__).parent.parent / "scripts" / "check_memory_context.py"
    spec = importlib.util.spec_from_file_location("check_memory_context", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("subject", ["", " \t\n"])
def test_cli_rejects_blank_subject_before_setup(checker, monkeypatch, tmp_path, capsys, subject):
    setup = Mock(side_effect=AssertionError("must reject the subject before setup"))
    monkeypatch.setattr(checker.tempfile, "TemporaryDirectory", setup)
    monkeypatch.setattr(checker, "check", setup)
    monkeypatch.setattr(
        "sys.argv",
        ["check_memory_context.py", "--memory-root", str(tmp_path), "--subject", subject],
    )

    with pytest.raises(SystemExit) as exc:
        checker.main()

    assert exc.value.code == 2
    assert "--subject: subject must contain non-whitespace characters" in capsys.readouterr().err
    setup.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("subject", ["", " \t\n"])
async def test_check_rejects_blank_subject_before_side_effects(checker, monkeypatch, tmp_path, subject):
    build = Mock(side_effect=AssertionError("must reject the subject before building a kernel"))
    monkeypatch.setattr(checker, "build_kernel", build)

    with pytest.raises(ValueError, match="subject must contain non-whitespace characters"):
        await checker.check(tmp_path, tmp_path / "missing-plugin", subject)

    build.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("subject_args", [[], ["--subject", "project with spaces"]])
def test_valid_subject_reaches_plugin_preflight(checker, monkeypatch, tmp_path, capsys, subject_args):
    monkeypatch.setattr(
        "sys.argv", ["check_memory_context.py", "--memory-root", str(tmp_path), *subject_args]
    )

    with pytest.raises(SystemExit) as exc:
        checker.main()

    assert exc.value.code == 2
    assert "memory plugin's preinstalled Python is unavailable" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
