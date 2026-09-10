from __future__ import annotations

import ctypes
import hashlib
import os
import platform as host_platform
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from .instance import ProcessIdentity
from .lineage import LineageStore
from .platforms import PlatformEnvironment

_PROFILE = re.compile(r"^prf_[0-9a-f]{32}$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class HostRuntimeError(RuntimeError):
    """Host-neutral N0TE runtime composition could not proceed safely."""


def is_valid_profile_id(value: object) -> bool:
    return isinstance(value, str) and _PROFILE.fullmatch(value) is not None


def _profiles(
    data_root: Path,
    *,
    error_type: type[Exception] = HostRuntimeError,
) -> tuple[str, ...]:
    root = Path(data_root) / "profiles"
    if not root.is_dir():
        return ()
    try:
        return tuple(
            sorted(
                item.name
                for item in root.iterdir()
                if is_valid_profile_id(item.name)
                and (item / LineageStore.DB_NAME).is_file()
            )
        )
    except OSError as exc:
        raise error_type("cannot inspect N0TE profiles") from exc


def resolve_profile_id(
    data_root: Path,
    *,
    explicit: str | None,
    environment: Mapping[str, str],
    error_type: type[Exception] = HostRuntimeError,
) -> str:
    requested = (explicit or environment.get("N0TE_PROFILE_ID") or "").strip()
    candidates = _profiles(data_root, error_type=error_type)
    if requested:
        if not is_valid_profile_id(requested) or requested not in candidates:
            raise error_type(
                "configured N0TE profile does not exist or is invalid"
            )
        return requested
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise error_type("no existing N0TE profile was found")
    raise error_type("multiple N0TE profiles exist; choose --profile-id")


def reference_backend(environment: Mapping[str, str]) -> str | None:
    raw = (environment.get("N0TE_REFERENCE_SEARCH_BACKEND") or "").strip()
    if raw:
        return raw.casefold().replace("-", "_")
    if (environment.get("N0TE_REFERENCE_SEARCH_ENDPOINT") or "").strip():
        return "http_json"
    if (
        environment.get("N0TE_OPENAI_API_KEY")
        or environment.get("OPENAI_API_KEY")
        or ""
    ).strip():
        return "openai_web"
    return None


def reference_route_kind(environment: Mapping[str, str]) -> str | None:
    backend = reference_backend(environment)
    if backend == "openai_web":
        return "INTERNET"
    if backend != "http_json":
        return None
    endpoint = (environment.get("N0TE_REFERENCE_SEARCH_ENDPOINT") or "").strip()
    if not endpoint:
        return None
    host = (urlparse(endpoint).hostname or "").casefold()
    return "LOCALHOST" if host in _LOOPBACK else "INTERNET"


def resolve_provider_id(
    *,
    explicit: str | None,
    environment: Mapping[str, str],
    error_type: type[Exception] = HostRuntimeError,
) -> tuple[str, dict[str, str]]:
    child_env = dict(environment)
    requested = (
        explicit
        or environment.get("N0TE_REFERENCE_SEARCH_PROVIDER_ID")
        or ""
    ).strip()
    if requested:
        return requested, child_env
    backend = reference_backend(environment)
    if backend is None:
        raise error_type("no reference provider is configured")
    if not (environment.get("N0TE_REFERENCE_SEARCH_BACKEND") or "").strip():
        child_env["N0TE_REFERENCE_SEARCH_BACKEND"] = backend
    if backend == "openai_web":
        return "openai-web-reference-search", child_env
    if backend == "http_json":
        return "configured-reference-search", child_env
    raise error_type("provider id must be explicit for this backend")


def _coordinator_address(
    endpoint: str,
    *,
    error_type: type[Exception] = HostRuntimeError,
) -> tuple[str, int]:
    parsed = urlparse(str(endpoint).strip())
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "http"
        or host not in _LOOPBACK
        or parsed.path.rstrip("/") != "/mcp"
    ):
        raise error_type("coordinator endpoint must be loopback http /mcp")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise error_type("coordinator endpoint contains unsupported components")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise error_type("coordinator endpoint port is invalid") from exc
    return (
        "127.0.0.1" if host in {"localhost", "127.0.0.1"} else "::1",
        port,
    )


class SystemProcessProbe:
    """Conservative liveness proof that rejects PID reuse by start token."""

    def __init__(
        self,
        platform: PlatformEnvironment | None = None,
        *,
        error_type: type[Exception] = HostRuntimeError,
    ) -> None:
        self.platform = platform or PlatformEnvironment.from_runtime_labels(
            host_platform.system(),
            host_platform.machine(),
        )
        self.error_type = error_type

    @staticmethod
    def _posix_start(pid: int) -> tuple[str, str | None]:
        try:
            done = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            return "UNKNOWN", None
        if done.returncode != 0:
            return "DEAD", None
        token = " ".join(done.stdout.split())
        return ("ALIVE", token) if token else ("DEAD", None)

    @staticmethod
    def _windows_start(pid: int) -> tuple[str, str | None]:
        if os.name != "nt":
            return "UNKNOWN", None
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return (
                ("DEAD", None)
                if int(kernel32.GetLastError()) == 87
                else ("UNKNOWN", None)
            )
        try:
            values = [ctypes.c_ulonglong() for _ in range(4)]
            ok = kernel32.GetProcessTimes(
                handle,
                *(ctypes.byref(value) for value in values),
            )
            return (
                ("ALIVE", str(int(values[0].value)))
                if ok
                else ("UNKNOWN", None)
            )
        finally:
            kernel32.CloseHandle(handle)

    def _raw_start(self, pid: int) -> tuple[str, str | None]:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return "DEAD", None
        if self.platform.os_family in {"MACOS", "LINUX"}:
            return self._posix_start(pid)
        if self.platform.os_family == "WINDOWS":
            return self._windows_start(pid)
        return "UNKNOWN", None

    def _token(self, raw: str) -> str:
        return f"{self.platform.os_family}:{raw}"

    def current_process(self) -> ProcessIdentity:
        status, raw = self._raw_start(os.getpid())
        if status != "ALIVE" or raw is None:
            raise self.error_type(
                "cannot prove current N0TE process start identity"
            )
        return ProcessIdentity.from_start_token(
            self.platform,
            pid=os.getpid(),
            start_token=self._token(raw),
        )

    def status(self, process: ProcessIdentity) -> str:
        if process.platform.os_family != self.platform.os_family:
            return "UNKNOWN"
        status, raw = self._raw_start(process.pid)
        if status != "ALIVE" or raw is None:
            return status
        digest = hashlib.sha256(self._token(raw).encode()).hexdigest()
        return (
            "ALIVE"
            if digest == process.start_token_fingerprint
            else "DEAD"
        )


class CoordinatorProcessSupervisor:
    """Reuse or own one loopback coordinator without leaking host semantics."""

    def __init__(
        self,
        endpoint: str,
        *,
        environment: Mapping[str, str],
        popen_factory=subprocess.Popen,
        error_type: type[Exception] = HostRuntimeError,
    ) -> None:
        self.error_type = error_type
        self.host, self.port = _coordinator_address(
            endpoint,
            error_type=error_type,
        )
        self.environment = dict(environment)
        self._popen_factory = popen_factory
        self._process = None
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def _listening(self) -> bool:
        try:
            with socket.create_connection(
                (self.host, self.port),
                timeout=0.15,
            ):
                return True
        except OSError:
            return False

    def start(self) -> str:
        if self._listening():
            return "REUSED"
        if reference_backend(self.environment) is None:
            raise self.error_type(
                "cannot self-start coordinator without a reference provider"
            )
        env = dict(self.environment)
        env.update(
            N0TE_MCP_TRANSPORT="streamable-http",
            N0TE_MCP_HOST=self.host,
            N0TE_MCP_PORT=str(self.port),
        )
        try:
            process = self._popen_factory(
                [sys.executable, "-m", "n0te.coordinator_mcp"],
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise self.error_type(
                "cannot start local N0TE coordinator"
            ) from exc
        self._process = process
        self._owned = True
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._process = None
                self._owned = False
                raise self.error_type(
                    "local N0TE coordinator exited during startup"
                )
            if self._listening():
                return "STARTED"
            time.sleep(0.05)
        self.stop()
        raise self.error_type(
            "local N0TE coordinator did not become reachable"
        )

    def stop(self) -> None:
        process, owned = self._process, self._owned
        self._process = None
        self._owned = False
        if not owned or process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
