"""Owned runtimes may depend on an explicit working directory for their libraries."""

import os
import sys

import pytest

from harness.errors import ProviderError
from harness.resources import LocalResources
from tests.test_local_resources import owned_catalog


async def test_owned_runtime_starts_in_configured_directory(tmp_path, unused_tcp_port):
    workdir = tmp_path / "runtime"
    workdir.mkdir()
    (workdir / "required-relative-file").write_text("present")
    catalog = owned_catalog(tmp_path, unused_tcp_port, cwd=workdir)
    command = catalog.entries["local"]["local"]["command"]
    catalog.entries["local"]["local"]["command"] = (
        sys.executable, "-c",
        "from pathlib import Path; import os; "
        "assert Path('required-relative-file').read_text() == 'present'; "
        f"os.execv({sys.executable!r}, {command!r})",
    )
    resources = LocalResources()
    events = []
    try:
        async with resources.use(catalog.resolve("local"), emit=events.append) as observed:
            assert observed.status == "ready" and observed.ownership == "harness"
            pid = int((tmp_path / "runtime.pid").read_text())
            assert os.readlink(f"/proc/{pid}/cwd") == str(workdir)
    finally:
        await resources.close(emit=events.append)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("kind", ["missing", "file"])
async def test_bad_runtime_directory_is_reported_before_launch(tmp_path, unused_tcp_port, kind):
    workdir = tmp_path / "runtime"
    if kind == "file":
        workdir.write_text("not a directory")
    catalog = owned_catalog(tmp_path, unused_tcp_port, cwd=workdir)
    events = []
    resources = LocalResources()
    try:
        with pytest.raises(ProviderError, match="local_working_directory_missing"):
            async with resources.use(catalog.resolve("local"), emit=events.append):
                pytest.fail("invalid working directory must prevent launch")
        assert not (tmp_path / "runtime.pid").exists()
        assert not any(e.type == "local_runtime_requested" for e in events)
        assert resources.snapshot(catalog.resolve("local")).status == "missing_configuration"
    finally:
        await resources.close(emit=events.append)
