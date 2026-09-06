"""Core local readiness and owned process lifetime, independent of plugins.

Inventory probes establish availability, not inference quality or feature support.
Only explicitly configured loopback resources are probed. Provisioning is separate.
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from harness.errors import ProviderError


class LocalProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    command: tuple[str, ...] = Field(default=(), max_length=128)
    env_names: tuple[str, ...] = Field(default=(), max_length=32)
    required_files: tuple[Path, ...] = ()
    auto_start: bool = False
    probe_kind: Literal["openai_inventory", "llamacpp"] = "openai_inventory"
    startup_seconds: float = Field(default=120, gt=0, le=600)
    probe_seconds: float = Field(default=2, gt=0, le=10)
    ttl_seconds: float = Field(default=5, ge=0, le=60)

    @field_validator("command", "env_names")
    @classmethod
    def valid_strings(cls, value):
        if any(not part or "\0" in part for part in value):
            raise ValueError("local profile entries must be nonempty strings without NUL")
        return value

    @model_validator(mode="after")
    def start_requires_command(self):
        if self.auto_start and not self.command:
            raise ValueError("local auto_start requires a preinstalled command")
        return self


def local_endpoint(url: str | None) -> str:
    """Reject credentials, redirects, remote hosts, and ambiguous local URLs."""
    try:
        parsed = urlsplit(url or "")
        host = parsed.hostname
        address = "127.0.0.1" if host == "localhost" else host
        if (parsed.scheme not in ("http", "https") or not address
                or not ipaddress.ip_address(address).is_loopback
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port
        netloc = f"[{address}]" if ":" in address else address
        if port is not None:
            netloc += f":{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        raise ValueError("local profile requires a loopback HTTP endpoint without credentials or query") from None


class ResourceObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    alias: str
    config_digest: str
    status: Literal["unknown", "checking", "missing_configuration", "loading", "ready", "busy",
                    "authentication_failed", "unreachable", "denied", "failed", "stopped"]
    reason: str = ""
    observed_at: float = 0
    expires_at: float = 0
    stale: bool = True
    ownership: Literal["external", "harness"] = "external"
    evidence: Literal["configuration", "model_inventory", "health_and_inventory",
                      "local_process", "local_activity"] = "configuration"
    model_id: str | None = None
    context_window: int | None = None
    tool_support: bool | None = None
    structured_output: bool | None = None
    runtime_version: str | None = None


class LocalResources:
    def __init__(self, *, transport=None, max_owned_processes=1):
        if type(max_owned_processes) is not int or max_owned_processes < 1:
            raise ValueError("owned local process limit must be a positive integer")
        self._transport = transport
        self._max_owned = max_owned_processes
        self._reserved = 0
        self._cache: dict[str, tuple[ResourceObservation, float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._owned: dict[str, tuple[asyncio.subprocess.Process, object, str]] = {}
        self._active: dict[str, int] = {}
        self._closing = False

    @staticmethod
    def _identity(resolved):
        data = {"route": str(resolved.route), "endpoint": resolved.api_base,
                "key_env": resolved.api_key_env,
                "profile": resolved.local.model_dump(mode="json") if resolved.local else None}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _credential(resolved):
        key = os.environ.get(resolved.api_key_env, "") if resolved.api_key_env else ""
        return hashlib.sha256(key.encode()).hexdigest()

    def snapshot(self, resolved, *, activity=True) -> ResourceObservation:
        identity = self._identity(resolved)
        cached = self._cache.get(resolved.alias)
        if cached is not None and cached[0].config_digest == identity:
            observation, expires, credential = cached
            fresh = time.monotonic() < expires and credential == self._credential(resolved)
            if activity and self._active.get(resolved.alias):
                return observation.model_copy(update={"status": "busy", "reason": "active_requests",
                                                      "evidence": "local_activity", "stale": False})
            return observation.model_copy(update={"stale": not fresh})
        return ResourceObservation(alias=resolved.alias, config_digest=identity,
                                   status="unknown" if resolved.local else "missing_configuration",
                                   reason="not_checked" if resolved.local else "no_local_profile")

    def _record(self, resolved, status, reason, emit, *, evidence=None, cache=True):
        from harness.events import ResourceObserved
        now = time.time()
        ttl = resolved.local.ttl_seconds if resolved.local and status not in ("stopped", "unknown") else 0
        observation = ResourceObservation(
            alias=resolved.alias, config_digest=self._identity(resolved), status=status, reason=reason,
            observed_at=now, expires_at=now + ttl, stale=False,
            ownership="harness" if resolved.alias in self._owned else "external",
            evidence=evidence or ("health_and_inventory" if resolved.local and
                                 resolved.local.probe_kind == "llamacpp" else "model_inventory"),
            model_id=str(resolved.route).split("/", 1)[-1] if status == "ready" else None,
        )
        emit(ResourceObserved(observation=observation))
        if cache:
            self._cache[resolved.alias] = observation, time.monotonic() + ttl, self._credential(resolved)
        return observation

    async def _probe(self, resolved, emit):
        if resolved.local is None:
            return self._record(resolved, "missing_configuration", "no_local_profile", emit,
                                evidence="configuration")
        self._record(resolved, "checking", "checking_inventory", emit, cache=False)
        headers = {}
        if resolved.api_key_env:
            key = os.environ.get(resolved.api_key_env)
            if not key:
                return self._record(resolved, "authentication_failed", "missing_credential", emit)
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with asyncio.timeout(resolved.local.probe_seconds):
                async with httpx.AsyncClient(transport=self._transport, trust_env=False,
                                             follow_redirects=False) as client:
                    paths = ("health", "models") if resolved.local.probe_kind == "llamacpp" else ("models",)
                    total = 0
                    for path in paths:
                        async with client.stream("GET", local_endpoint(resolved.api_base) + "/" + path,
                                                 headers=headers) as response:
                            payload = bytearray()
                            async for part in response.aiter_bytes():
                                total += len(part)
                                if total > 65536:
                                    return self._record(resolved, "unknown", "inventory_too_large", emit)
                                payload.extend(part)
                            code = response.status_code
                        if code in (401, 403, 429):
                            status = {401: "authentication_failed", 403: "denied", 429: "busy"}[code]
                            return self._record(resolved, status, f"http_{code}", emit)
                        try:
                            data = json.loads(payload)
                        except (ValueError, UnicodeDecodeError):
                            data = None
                        if code == 503 and isinstance(data, dict):
                            error = data.get("error")
                            if (data.get("status") in ("loading", "loading model") or
                                    isinstance(error, dict) and (error.get("type") == "loading" or
                                    error.get("message") == "Loading model")):
                                return self._record(resolved, "loading", "server_loading", emit)
                        if code != 200:
                            return self._record(resolved, "unknown", f"http_{code}", emit)
                        if path == "health":
                            if not isinstance(data, dict) or data.get("status") != "ok":
                                return self._record(resolved, "unknown", "invalid_health", emit)
                            continue
                        inventory = data.get("data") if isinstance(data, dict) else None
                        if not isinstance(inventory, list) or any(not isinstance(row, dict) for row in inventory):
                            return self._record(resolved, "unknown", "invalid_inventory", emit)
                        expected = str(resolved.route).split("/", 1)[-1]
                        found = any(row.get("id") == expected for row in inventory)
                        return self._record(resolved, "ready" if found else "missing_configuration",
                                            "model_listed" if found else "model_not_listed", emit)
        except asyncio.CancelledError:
            self._record(resolved, "unknown", "probe_cancelled", emit)
            raise
        except (TimeoutError, httpx.TimeoutException):
            return self._record(resolved, "unreachable", "probe_timeout", emit)
        except httpx.HTTPError:
            return self._record(resolved, "unreachable", "probe_transport_failed", emit)

    async def check(self, resolved, *, emit):
        """Explicit checks always refresh, never launch processes or inference."""
        async with self._locks.setdefault(resolved.alias, asyncio.Lock()):
            return await self._probe(resolved, emit)

    async def _terminate(self, alias, emit):
        from harness.events import LocalRuntimeRequested
        process, resolved, identity = self._owned[alias]
        log_error = None
        try:
            emit(LocalRuntimeRequested(alias=alias, config_digest=identity, action="stop"))
        except Exception as exc:
            log_error = exc  # cleanup still owns the child when the journal fails
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        # The leader can exit on TERM while a worker ignores it. Terminate any
        # remaining members of this owned process group before reporting stop.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self._record(resolved, "stopped", "owned_process_stopped", emit, evidence="local_process")
        finally:
            self._owned.pop(alias)
            self._reserved -= 1
        if log_error is not None:
            raise log_error

    async def _ready(self, resolved, emit):
        if self._closing:
            raise ProviderError("local resources are closing")
        owned = self._owned.get(resolved.alias)
        if owned and owned[2] != self._identity(resolved):
            raise ProviderError("local runtime configuration changed; stop the owned runtime before retrying")
        if owned and owned[0].returncode is not None:
            await self._terminate(resolved.alias, emit)
            return self._record(resolved, "failed", "owned_process_exited", emit, evidence="local_process")
        observation = self.snapshot(resolved, activity=False)
        if observation.stale:
            observation = await self._probe(resolved, emit)
        if (observation.status != "unreachable" or not resolved.local.auto_start or owned):
            return observation
        profile = resolved.local
        if any(not path.is_file() for path in profile.required_files):
            return self._record(resolved, "missing_configuration", "local_assets_missing", emit,
                                evidence="configuration")
        if self._reserved >= self._max_owned:
            return self._record(resolved, "busy", "owned_process_capacity", emit, evidence="local_activity")
        from harness.events import LocalRuntimeRequested
        emit(LocalRuntimeRequested(alias=resolved.alias, config_digest=self._identity(resolved), action="start"))
        self._reserved += 1  # reserve synchronously before the first spawn await
        env_names = {"PATH", "HOME", "USER", "LANG", "TMPDIR", "XDG_CACHE_HOME", *profile.env_names}
        env = {key: value for key, value in os.environ.items() if key in env_names}
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
                *profile.command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True,
        ))
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Cancellation can race the OS spawn. Recover the handle and settle
            # our process before propagating; never abandon an untracked child.
            try:
                process = await launch
            except BaseException:
                self._reserved -= 1
            else:
                self._owned[resolved.alias] = process, resolved, self._identity(resolved)
                await self._terminate(resolved.alias, emit)
            raise
        except Exception as exc:
            self._reserved -= 1
            status, reason = "failed", "local_launch_failed"
            if isinstance(exc, FileNotFoundError):
                status, reason = "missing_configuration", "local_command_missing"
            elif isinstance(exc, PermissionError):
                status, reason = "denied", "local_command_denied"
            return self._record(resolved, status, reason, emit, evidence="local_process")
        self._owned[resolved.alias] = process, resolved, self._identity(resolved)
        try:
            if self._closing:
                raise ProviderError("local resources are closing")
            self._record(resolved, "loading", "owned_process_started", emit, evidence="local_process")
            async with asyncio.timeout(profile.startup_seconds):
                while True:
                    if process.returncode is not None:
                        raise ProviderError("local runtime exited during startup")
                    observation = await self._probe(resolved, emit)
                    if observation.status == "ready":
                        return observation
                    if observation.status not in ("loading", "unreachable"):
                        raise ProviderError(f"local runtime startup: {observation.status} ({observation.reason})")
                    await asyncio.sleep(0.1)
        except BaseException:
            await self._terminate(resolved.alias, emit)
            raise

    @asynccontextmanager
    async def use(self, resolved, *, emit):
        async with self._locks.setdefault(resolved.alias, asyncio.Lock()):
            observation = await self._ready(resolved, emit)
            if observation.status != "ready":
                raise ProviderError(f"local model {resolved.alias}: {observation.status} ({observation.reason})")
            self._record(resolved, "busy", "active_requests", emit, evidence="local_activity", cache=False)
            self._active[resolved.alias] = self._active.get(resolved.alias, 0) + 1
        try:
            yield observation
        except BaseException:
            self._cache.pop(resolved.alias, None)
            self._record(resolved, "unknown", "request_interrupted_or_failed", emit, cache=False,
                         evidence="local_activity")
            raise
        finally:
            self._active[resolved.alias] -= 1
            if not self._active[resolved.alias] and resolved.alias in self._cache:
                from harness.events import ResourceObserved
                # Releasing activity is not a new health probe. Preserve the
                # original observation time/expiry and its current stale flag.
                emit(ResourceObserved(observation=self.snapshot(resolved, activity=False)))

    async def stop(self, alias, *, emit) -> bool:
        async with self._locks.setdefault(alias, asyncio.Lock()):
            if any(self._active.values()):
                raise ProviderError("local runtime is in use; interrupt its task before stopping")
            if alias not in self._owned:
                return False
            await self._terminate(alias, emit)
            return True

    async def close(self, *, emit):
        self._closing = True
        failure = None
        for alias in list(self._owned):
            try:
                await self.stop(alias, emit=emit)
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure


def render_resources(observations) -> str:
    rows = [f"{o.alias}: {o.status}{' (stale)' if o.stale else ''}; {o.ownership}; {o.reason}"
            for o in observations]
    return "\n".join(rows) if rows else "No local runtime profiles configured."
