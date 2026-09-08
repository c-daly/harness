"""Discovery is metadata-only; registration must preserve operator state."""

import asyncio
import fcntl
import hashlib
import json
import os
import struct
import tomllib
from pathlib import Path

import httpx
import pytest

from harness.catalog import Catalog
from harness.model_management import (
    HubFile, HubInventory, ModelSetupError, inspect_gguf, inspect_hub, register_model,
    render_catalog, render_hub,
)
from harness.models_cli import perform


REV = "a" * 40


@pytest.fixture
def setup_files(tmp_path):
    model = tmp_path / "model with spaces.gguf"
    model.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 0) + b"fixture")
    runtime = tmp_path / "llama-server"
    runtime.write_text("#!/bin/sh\nexit 97\n")  # Registration must never launch it.
    runtime.chmod(0o700)
    return dict(catalog_path=tmp_path / "models.toml", alias="test-local", model_file=model,
                runtime=runtime)


def metadata(**updates):
    return {"id": "owner/repo", "sha": REV, "siblings": [
        {"rfilename": "README.md", "size": 99},
        {"rfilename": "model.gguf", "size": 42, "blobId": "b" * 40,
         "lfs": {"sha256": "c" * 64, "size": 42}},
    ], **updates}


async def test_public_metadata_and_pinned_offline_cache(tmp_path, monkeypatch):
    calls = []

    def request(req):
        calls.append(req)
        assert str(req.url) == "https://huggingface.co/api/models/owner/repo/revision/main?blobs=true"
        assert "authorization" not in req.headers
        return httpx.Response(200, json=metadata())

    monkeypatch.setenv("HF_TOKEN", "must-not-be-read")
    inventory, cached = await inspect_hub("owner/repo", "main", cache_dir=tmp_path,
                                          transport=httpx.MockTransport(request))
    assert not cached and len(calls) == 1
    assert inventory.files == (HubFile(filename="model.gguf", size_bytes=42, sha256="c" * 64),)
    assert (await inspect_hub("owner/repo", REV, cache_dir=tmp_path, offline=True))[0] == inventory
    monkeypatch.setenv("HF_HUB_OFFLINE", "true")
    assert (await inspect_hub("owner/repo", "main", cache_dir=tmp_path))[1]
    assert "Cached metadata" in render_hub(inventory, True)
    assert "excludes memory" in render_hub(inventory, False)


async def test_network_failure_uses_only_matching_cache(tmp_path):
    await inspect_hub("owner/repo", "main", cache_dir=tmp_path,
                      transport=httpx.MockTransport(lambda r: httpx.Response(200, json=metadata())))

    def fail(req):
        raise httpx.ConnectError("private-network-secret", request=req)

    transport = httpx.MockTransport(fail)
    assert (await inspect_hub("owner/repo", "main", cache_dir=tmp_path, transport=transport))[1]
    with pytest.raises(ModelSetupError, match="unreachable") as exc:
        await inspect_hub("owner/other", "main", cache_dir=tmp_path, transport=transport)
    assert "secret" not in str(exc.value)


async def test_requested_commit_must_match_response_commit(tmp_path):
    with pytest.raises(ModelSetupError, match="invalid"):
        await inspect_hub("owner/repo", "b" * 40, cache_dir=tmp_path,
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json=metadata())))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("data", [
    metadata(id="different/repo"), metadata(sha="main"), metadata(siblings={}),
    metadata(siblings=[{"rfilename": "../model.gguf"}]),
    metadata(siblings=[{"rfilename": "model.gguf", "size": True}]),
    metadata(siblings=[{"rfilename": "model.gguf", "size": 42, "lfs": {"size": 43}}]),
    metadata(siblings=[{"rfilename": "model.gguf"}] * 2),
])
async def test_invalid_remote_data_never_cached(tmp_path, data):
    with pytest.raises(ModelSetupError, match="invalid"):
        await inspect_hub("owner/repo", "main", cache_dir=tmp_path,
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("code", [302, 401, 403, 404, 503])
async def test_http_failure_is_bounded_and_does_not_follow_redirects(tmp_path, code):
    calls = []

    def reply(req):
        calls.append(req)
        return httpx.Response(code, headers={"location": "https://other.test/weights"}, text="secret")

    with pytest.raises(ModelSetupError) as exc:
        await inspect_hub("owner/repo", "main", cache_dir=tmp_path, transport=httpx.MockTransport(reply))
    assert "secret" not in str(exc.value) and len(calls) == 1
    assert not list(tmp_path.iterdir())


async def test_metadata_size_limit_and_cancellation(tmp_path):
    with pytest.raises(ModelSetupError, match="2 MiB"):
        await inspect_hub("owner/repo", "main", cache_dir=tmp_path, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))))
    entered = asyncio.Event()

    async def hang(req):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(inspect_hub("owner/repo", "main", cache_dir=tmp_path,
                                         transport=httpx.MockTransport(hang)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.iterdir())


async def test_registration_preserves_catalog_and_builds_on_demand_profile(setup_files):
    path = setup_files["catalog_path"]
    original = b"# operator comment\n[models.cloud]\nroute='provider/model'\n\n"
    path.write_bytes(original)
    result = await register_model(**setup_files, port=8188, library_path=path.parent, disable_thinking=True)
    assert path.read_bytes().startswith(original)
    assert Path(result["backup"]).read_bytes() == original
    resolved = Catalog.load(path).resolve("test-local")
    assert resolved.route == "openai/test-local" and resolved.execution_kind == "inference"
    assert not resolved.verified and resolved.local.auto_start
    assert resolved.api_base == "http://127.0.0.1:8188/v1"
    command = resolved.local.command
    assert str(setup_files["model_file"]) in command
    assert command[:2] == ("/usr/bin/env", f"LD_LIBRARY_PATH={path.parent}")
    assert '{"enable_thinking":false}' in command
    assert result["artifact"]["sha256"] == hashlib.sha256(setup_files["model_file"].read_bytes()).hexdigest()
    assert "starts on demand" in render_catalog(path)
    with pytest.raises(ModelSetupError, match="already exists"):
        await register_model(**setup_files)


async def test_hub_registration_compares_digest_and_records_commit(setup_files):
    raw = setup_files["model_file"].read_bytes()
    file = HubFile(filename="model.gguf", size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    inventory = HubInventory(repo="owner/repo", requested_revision="main", revision=REV,
                             observed_at=1, files=(file,))
    result = await register_model(**setup_files, expected=file, inventory=inventory)
    assert result["artifact"]["hub_revision"] == REV
    setup_files["catalog_path"].unlink()
    setup_files["model_file"].write_bytes(raw + b"changed")
    with pytest.raises(ModelSetupError, match="size/hash"):
        await register_model(**setup_files, expected=file, inventory=inventory)
    assert not setup_files["catalog_path"].exists()


@pytest.mark.parametrize("kind", ["bad-header", "fifo", "shard", "shard-symlink"])
async def test_invalid_model_never_registered(setup_files, kind):
    path = setup_files["model_file"]
    if kind == "bad-header":
        path.write_bytes(b"not a model")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    else:
        shard = path.with_name("model-00001-of-00002.gguf")
        if kind == "shard":
            path.rename(shard)
        else:
            shard.symlink_to(path)
        setup_files["model_file"] = shard
    with pytest.raises(ModelSetupError):
        await register_model(**setup_files)
    assert not setup_files["catalog_path"].exists()


@pytest.mark.parametrize("action", ["cancel", "model-change", "catalog-change"])
async def test_interrupted_verification_cannot_publish(setup_files, action):
    path = setup_files["catalog_path"]
    original = b"[models.existing]\nroute='old/model'\n"
    path.write_bytes(original)

    def progress(_):
        if action == "cancel":
            asyncio.current_task().cancel()
        elif action == "model-change":
            setup_files["model_file"].write_bytes(b"changed")
        else:
            path.write_bytes(original + b"# concurrent edit\n")

    task = asyncio.create_task(register_model(**setup_files, progress=progress))
    with pytest.raises(asyncio.CancelledError if action == "cancel" else ModelSetupError):
        await task
    assert "test-local" not in tomllib.loads(path.read_text())["models"]
    assert not list(path.parent.glob("*.bak"))


@pytest.mark.parametrize("kind", ["symlink", "fifo", "invalid", "scalar-models", "scalar-entry", "locked"])
async def test_catalog_problems_fail_without_replacing_it(setup_files, kind):
    path = setup_files["catalog_path"]
    if kind == "symlink":
        path.symlink_to(setup_files["model_file"])
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "locked":
        with path.with_name(path.name + ".guard").open("w") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            with pytest.raises(ModelSetupError, match="Another"):
                await register_model(**setup_files)
        assert not path.exists()
        return
    else:
        path.write_text({"invalid": "[", "scalar-models": "models=1", "scalar-entry": "[models]\nx=1"}[kind])
    with pytest.raises(ModelSetupError):
        await register_model(**setup_files)


@pytest.mark.parametrize("option,value", [("alias", "bad.alias"), ("port", 0), ("context", 12),
                                         ("gpu_layers", -1), ("threads", True)])
async def test_invalid_options_do_not_create_catalog(setup_files, option, value):
    with pytest.raises(ModelSetupError):
        await register_model(**{**setup_files, option: value})
    assert not setup_files["catalog_path"].exists()


async def test_missing_runtime_gives_setup_guidance(setup_files):
    setup_files["runtime"].unlink()
    with pytest.raises(ModelSetupError, match="Install it first"):
        await register_model(**setup_files)


async def test_shared_cli_offline_registration_and_errors(setup_files, monkeypatch, capsys):
    from harness.cli import main
    path = setup_files["catalog_path"]
    words = ["--catalog", str(path), "add", "test-local", "--file", str(setup_files["model_file"]),
             "--runtime", str(setup_files["runtime"])]
    assert "inference quality" in await perform(words)
    assert "test-local" in await perform(["--catalog", str(path), "list"])
    with pytest.raises(ModelSetupError):
        await perform(["add"])
    assert "metadata only" in await perform(["help"])
    monkeypatch.setattr("sys.argv", ["harness", "models", "--help"])
    # CLI main uses asyncio.run; invoke outside this test's running event loop.
    await asyncio.to_thread(main)
    assert "Model setup" in capsys.readouterr().out


async def test_corrupt_cache_is_not_offline_evidence(tmp_path):
    inventory, _ = await inspect_hub("owner/repo", "main", cache_dir=tmp_path,
                                    transport=httpx.MockTransport(lambda r: httpx.Response(200, json=metadata())))
    for path in tmp_path.iterdir():
        data = json.loads(inventory.model_dump_json())
        data["repo"] = "owner/other"
        path.write_text(json.dumps(data))
    with pytest.raises(ModelSetupError, match="No usable cached"):
        await inspect_hub("owner/repo", "main", cache_dir=tmp_path, offline=True)


async def test_hash_is_cancellable_between_chunks(setup_files):
    path = setup_files["model_file"]
    with path.open("ab") as output:
        output.write(b"x" * (9 * 1024 * 1024))
    seen = []

    def progress(text):
        seen.append(text)
        asyncio.current_task().cancel()

    task = asyncio.create_task(inspect_gguf(path, progress=progress))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) == 1
