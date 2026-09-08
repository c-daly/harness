"""Operator-owned model discovery and registration; never an inference tool."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import time
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from harness.catalog import Catalog
from harness.persistence import atomic_write


class ModelSetupError(ValueError):
    pass


class HubFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    filename: str
    size_bytes: int | None = Field(default=None, ge=0, strict=True)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class HubInventory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    repo: str
    requested_revision: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    observed_at: float = Field(gt=0, le=253402300799)
    files: tuple[HubFile, ...] = Field(max_length=4096)


_CAP = 2 * 1024 * 1024
_SPLIT = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.I)


def _identifier(value: str, *, repo=False) -> str:
    pattern = r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*" if repo else r"[A-Za-z0-9][A-Za-z0-9._/-]*"
    if (len(value) > 192 or not re.fullmatch(pattern, value)
            or any(part in (".", "..", "") for part in value.split("/"))):
        raise ModelSetupError("Use a Hub owner/repository and a valid revision name or commit.")
    return value


def _filename(value: str) -> bool:
    return (bool(value) and len(value) <= 1024 and not PurePosixPath(value).is_absolute()
            and "\\" not in value and all(c.isprintable() for c in value)
            and all(p not in ("", ".", "..") for p in value.split("/")))


def _cache_path(directory: Path, repo: str, revision: str) -> Path:
    key = hashlib.sha256(json.dumps([repo, revision]).encode()).hexdigest()
    return directory / f"{key}.json"


def _read_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ModelSetupError("Model metadata and catalogs must be regular files.")
        raw = source.read(_CAP + 1)
    if len(raw) > _CAP:
        raise ModelSetupError("Model metadata or catalog exceeds the 2 MiB limit.")
    return raw


def _read_inventory(directory: Path, repo: str, revision: str) -> HubInventory:
    try:
        raw = _read_regular(_cache_path(directory, repo, revision))
        inventory = HubInventory.model_validate_json(raw)
        if (inventory.repo != repo or revision not in (inventory.requested_revision, inventory.revision)
                or len({f.filename for f in inventory.files}) != len(inventory.files)
                or any(not _filename(f.filename) or not f.filename.lower().endswith(".gguf") for f in inventory.files)):
            raise ValueError
        return inventory
    except (OSError, ValueError):
        raise ModelSetupError("No usable cached Hub metadata for that repository and revision.") from None


async def inspect_hub(repo: str, revision: str, *, cache_dir: Path, offline=False,
                      transport=None) -> tuple[HubInventory, bool]:
    """Public metadata only. No tokens, weight downloads, redirects or remote code."""
    _identifier(repo, repo=True)
    _identifier(revision)
    if offline or os.environ.get("HF_HUB_OFFLINE", "").lower() in {"1", "true", "yes", "on"}:
        return _read_inventory(cache_dir, repo, revision), True
    try:
        async with asyncio.timeout(15), httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=transport,
        ) as client:
            url = f"https://huggingface.co/api/models/{quote(repo, safe='/')}/revision/{quote(revision, safe='')}"
            async with client.stream("GET", url, params={"blobs": "true"}) as response:
                if response.status_code in (401, 403):
                    raise ModelSetupError("Hub access denied; this command supports public repositories.")
                if response.status_code == 404:
                    raise ModelSetupError("Hub repository or revision was not found.")
                if response.status_code != 200:
                    raise ModelSetupError(f"Hub metadata unavailable (HTTP {response.status_code}).")
                raw = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    raw.extend(chunk)
                    if len(raw) > _CAP:
                        raise ModelSetupError("Hub metadata exceeds the 2 MiB limit.")
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("id") != repo
                or re.fullmatch(r"[0-9a-f]{40}", revision) and data.get("sha") != revision):
            raise ValueError
        files = []
        siblings = data.get("siblings", [])
        if not isinstance(siblings, list):
            raise ValueError
        for item in siblings:
            if not isinstance(item, dict):
                raise ValueError
            name = item.get("rfilename", "")
            if not isinstance(name, str) or not _filename(name):
                raise ValueError
            if not name.lower().endswith(".gguf"):
                continue
            lfs = item.get("lfs") or {}
            if not isinstance(lfs, dict):
                raise ValueError
            size = item.get("size", lfs.get("size"))
            if size is not None and lfs.get("size", size) != size:
                raise ValueError
            files.append(HubFile(filename=name, size_bytes=size, sha256=lfs.get("sha256")))
        if len({f.filename for f in files}) != len(files):
            raise ValueError
        inventory = HubInventory(repo=repo, requested_revision=revision, revision=data["sha"],
                                 observed_at=time.time(), files=tuple(sorted(files, key=lambda f: f.filename)))
    except (httpx.HTTPError, TimeoutError):
        try:
            return _read_inventory(cache_dir, repo, revision), True
        except ModelSetupError:
            raise ModelSetupError("Hub is unreachable and no cached metadata is available. Local registration still works.") from None
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ModelSetupError):
            raise
        raise ModelSetupError("Hub returned invalid model metadata.") from None
    cache_dir.mkdir(parents=True, exist_ok=True)
    encoded = inventory.model_dump_json().encode()
    atomic_write(_cache_path(cache_dir, repo, revision), encoded)
    if inventory.revision != revision:
        atomic_write(_cache_path(cache_dir, repo, inventory.revision), encoded)
    return inventory, False


def render_hub(inventory: HubInventory, cached: bool) -> str:
    seen = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(inventory.observed_at))
    lines = [f"{inventory.repo} @ {inventory.revision}",
             f"{'Cached' if cached else 'Live'} metadata observed {seen}."]
    for item in inventory.files[:100]:
        size = "unknown size" if item.size_bytes is None else f"{item.size_bytes / 1024**3:.2f} GiB"
        kind = "split GGUF; all shards required" if _SPLIT.search(item.filename) else "GGUF"
        lines.append(f"  {item.filename} | {size} | {kind}")
    if not inventory.files:
        lines.append("No GGUF files listed.")
    elif len(inventory.files) > 100:
        lines.append(f"Showing 100 of {len(inventory.files)} files; use --json for the complete inventory.")
    lines.append("File size excludes memory needed to run the model. This command downloads metadata only.")
    return "\n".join(lines)


def _path(value: Path) -> Path:
    if any(not c.isprintable() for c in str(value)):
        raise ModelSetupError("Paths must not contain control characters.")
    try:
        return value.expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise ModelSetupError(f"File or directory not found: {value}. Install it first, then retry registration.") from None
    except PermissionError:
        raise ModelSetupError(f"Permission denied while accessing: {value}.") from None


async def inspect_gguf(path: Path, *, progress=lambda text: None) -> dict:
    supplied_name = path.name
    path = _path(path)
    if _SPLIT.search(supplied_name) or _SPLIT.search(path.name):
        raise ModelSetupError("Registration currently supports single-file GGUF models; this file is a shard.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ModelSetupError("The model must be a regular GGUF file.")
        header = source.read(24)
        if len(header) != 24 or header[:4] != b"GGUF" or struct.unpack("<I", header[4:8])[0] not in (2, 3):
            raise ModelSetupError("The file does not have a supported GGUF v2/v3 header.")
        source.seek(0)
        digest, completed, last_update = hashlib.sha256(), 0, float("-inf")
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
            completed += len(chunk)
            if completed > before.st_size:
                raise ModelSetupError("The model changed while it was being verified; retry after the write finishes.")
            if time.monotonic() - last_update >= 0.2 or completed == before.st_size:
                progress(f"Verifying {path.name}: {completed / 1024**2:.0f}/{before.st_size / 1024**2:.0f} MiB")
                last_update = time.monotonic()
            await asyncio.sleep(0)
        def signature(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(os.fstat(source.fileno())) or signature(before) != signature(path.stat()):
            raise ModelSetupError("The model changed while it was being verified; retry after the write finishes.")
    return {"path": str(path), "size_bytes": completed, "sha256": digest.hexdigest(),
            "gguf_version": struct.unpack("<I", header[4:8])[0], "checked_at": time.time()}


def _catalog_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise ModelSetupError("Choose the catalog's actual path rather than a symlink.")
    try:
        raw = _read_regular(path)
    except FileNotFoundError:
        return b""
    try:
        data = tomllib.loads(raw.decode())
        entries = data.get("models", {})
        if not isinstance(entries, dict) or any(not isinstance(v, dict) for v in entries.values()):
            raise ValueError
        for entry in entries.values():
            if "local" in entry and not isinstance(entry["local"], dict):
                raise ValueError
            if not isinstance(entry.get("tags", []), list):
                raise ValueError
    except ValueError:
        raise ModelSetupError("The existing catalog is invalid; repair it before registering a model.") from None
    return raw


def _entry_text(alias: str, entry: dict) -> str:
    lines = [f"[models.{alias}]"]
    for key, value in entry.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {json.dumps(value)}")
    for section in ("local", "artifact"):
        lines.extend(["", f"[models.{alias}.{section}]"])
        for key, value in entry[section].items():
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def _publish_alias(path: Path, expected: bytes, alias: str, entry: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".guard"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ModelSetupError("Another model registration is updating this catalog; retry shortly.") from None
        if _catalog_bytes(path) != expected:
            raise ModelSetupError("The catalog changed during verification; retry to preserve the newer changes.")
        original = tomllib.loads(expected.decode())
        candidate = expected + b"\n\n" + _entry_text(alias, entry).encode()
        parsed = tomllib.loads(candidate.decode())
        rest = dict(parsed.get("models", {}))
        if rest.pop(alias) != entry or rest != original.get("models", {}):
            raise ModelSetupError("Registration would alter existing catalog entries; no changes published.")
        Catalog(parsed["models"]).resolve(alias)
        backup = None
        if path.exists():
            backup = path.with_name(path.name + ".before-" + hashlib.sha256(expected).hexdigest() + ".bak")
            try:
                atomic_write(backup, expected, replace=False)
            except FileExistsError:
                if backup.is_symlink() or _read_regular(backup) != expected:
                    raise ModelSetupError("The catalog backup path already contains different data.") from None
        atomic_write(path, candidate)
        return backup


async def register_model(*, catalog_path: Path, alias: str, model_file: Path, runtime: Path,
                         library_path: Path | None = None, port=8080, context=8192, gpu_layers=99,
                         threads=4, disable_thinking=False, expected: HubFile | None = None,
                         inventory: HubInventory | None = None, progress=lambda text: None) -> dict:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", alias):
        raise ModelSetupError("Alias must start with a letter and use at most 64 letters, digits, hyphens or underscores.")
    if (type(port) is not int or not 1 <= port <= 65535 or type(context) is not int
            or not 512 <= context <= 1048576 or type(gpu_layers) is not int or not 0 <= gpu_layers <= 999
            or type(threads) is not int or not 1 <= threads <= 256):
        raise ModelSetupError("Invalid port, context size, GPU layer count or thread count.")
    catalog_path = catalog_path.expanduser().absolute()
    original = _catalog_bytes(catalog_path)
    if alias in tomllib.loads(original.decode()).get("models", {}):
        raise ModelSetupError(f"Alias {alias!r} already exists; choose a new alias. Existing entries are never replaced.")
    runtime = _path(runtime)
    if not runtime.is_file() or not os.access(runtime, os.X_OK):
        raise ModelSetupError("Choose an executable, preinstalled llama-server binary.")
    if library_path is not None:
        library_path = _path(library_path)
        if not library_path.is_dir():
            raise ModelSetupError("The library path must be a directory.")
    artifact = await inspect_gguf(model_file, progress=progress)
    if expected is not None:
        if inventory is None or expected not in inventory.files or not expected.sha256:
            raise ModelSetupError("Hub metadata must identify this file with a SHA-256 digest.")
        if (artifact["sha256"] != expected.sha256 or expected.size_bytes is not None
                and artifact["size_bytes"] != expected.size_bytes):
            raise ModelSetupError("The local file does not match the selected Hub artifact's size/hash.")
        artifact.update(hub_repo=inventory.repo, hub_revision=inventory.revision, hub_filename=expected.filename)
    elif inventory is not None:
        raise ModelSetupError("Select a specific GGUF file from the Hub inventory.")
    command = [str(runtime), "--model", artifact["path"], "--alias", alias,
               "--host", "127.0.0.1", "--port", str(port), "--ctx-size", str(context),
               "--n-gpu-layers", str(gpu_layers), "--parallel", "1", "--threads", str(threads),
               "--threads-batch", str(threads), "--jinja"]
    if disable_thinking:
        command += ["--chat-template-kwargs", '{"enable_thinking":false}']
    if library_path is not None:
        command = ["/usr/bin/env", f"LD_LIBRARY_PATH={library_path}", *command]
    entry = {"route": f"openai/{alias}", "api_base": f"http://127.0.0.1:{port}/v1",
             "max_input_tokens": context, "input_cost_per_token": 0.0, "output_cost_per_token": 0.0,
             "tags": ["local"], "verified": False,
             "local": {"auto_start": True, "probe_kind": "llamacpp", "resource_group": "local",
                       "cwd": str(runtime.parent), "startup_seconds": 120,
                       "required_files": [str(runtime), artifact["path"]], "command": command},
             "artifact": artifact}
    await asyncio.sleep(0)  # Last cancellation point before the short atomic publication.
    backup = _publish_alias(catalog_path, original, alias, entry)
    return {"alias": alias, "catalog": str(catalog_path), "backup": str(backup) if backup else None,
            "artifact": artifact, "status": "registered; inference not yet checked"}


def render_catalog(path: Path) -> str:
    entries = tomllib.loads(_catalog_bytes(path).decode()).get("models", {})
    lines = []
    for alias, entry in entries.items():
        if entry.get("backend"):
            kind = "agent runtime"
        elif "local" in entry:
            kind = "local model; starts on demand" if entry["local"].get("auto_start") else "local model; server started separately"
        elif "local" in entry.get("tags", ()):
            kind = "local model; no startup profile"
        else:
            kind = "model endpoint"
        lines.append(f"{json.dumps(alias, ensure_ascii=False)}: {kind}")
    lines.append("/model ALIAS selects a model; /resources checks configured local runtimes.")
    return "\n".join(lines) if entries else "No models configured. Use /models add to register an installed GGUF."
