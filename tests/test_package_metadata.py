"""The installed-package contract: version, and the console entry point.

These assertions are about *installed metadata*, not about source files, so they
are what tells us a built wheel is usable. They run against whatever `harness`
distribution is importable in the current environment.
"""

import importlib.metadata

from packaging.version import Version

import harness
import harness.cli

DIST_NAME = "harness"


def test_installed_version_is_pep440():
    raw = importlib.metadata.version(DIST_NAME)
    assert raw, "distribution reports an empty version"
    Version(raw)  # raises InvalidVersion if the metadata version is not PEP 440


def test_dunder_version_matches_installed_metadata():
    assert harness.__version__ == importlib.metadata.version(DIST_NAME)


def _console_script(name):
    eps = [
        ep
        for ep in importlib.metadata.distribution(DIST_NAME).entry_points
        if ep.group == "console_scripts" and ep.name == name
    ]
    assert len(eps) == 1, f"expected exactly one {name!r} console script, got {eps!r}"
    return eps[0]


def test_console_script_target_is_declared():
    assert _console_script("harness").value == "harness.cli:main"


def test_console_script_resolves_to_cli_main():
    assert _console_script("harness").load() is harness.cli.main
