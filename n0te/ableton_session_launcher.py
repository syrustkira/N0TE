from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import os
import platform as host_platform
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

from .ableton_host_bridge import DEFAULT_ABLETON_BRIDGE_ENDPOINT, AbletonHostBridgeError
from .ableton_observer_service import AbletonObserverService, AbletonObserverServiceError
from .app_runtime import ApplicationRuntime, ApplicationRuntimeError
from .coordinator_gateway import DEFAULT_COORDINATOR_MCP_ENDPOINT, CoordinatorGatewayError
from .instance import ProcessIdentity
from .lineage import LineageError, LineageStore
from .platforms import PlatformEnvironment, resolve_application_roots

_PROFILE = re.compile(r"^prf_[0-9a-f]{32}$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class AbletonSessionLauncherError(RuntimeError):
    pass


@dataclass(frozen=True)
class AbletonSessionConfig:
    data_root: Path
    state_root: Path
    profile_id: str
    provider_id: str
    bridge_endpoint: str = DEFAULT_ABLETON_BRIDGE_ENDPOINT
    coordinator_endpoint: str = DEFAULT_COORDINATOR_MCP_ENDPOINT
    poll_interval_seconds: float = 1.0
    reconnect_interval_seconds: float = 2.0
    discovery_retry_seconds: float = 30.0
    once: bool = False

    def __post_init__(self) -> None:
        data, state = Path(self.data_root), Path(self.state_root)
        if not data.is_absolute() or not state.is_absolute():
            raise AbletonSessionLauncherError("session roots must be absolute")
        if not _PROFILE.fullmatch(str(self.profile_id)):
            raise AbletonSessionLauncherError("profile_id is invalid")
        provider = str(self.provider_id).strip()
        if not provider:
            raise AbletonSessionLauncherError("provider_id must not be empty")
        object.__setattr__(self, "data_root", data)
        object.__setattr__(self, "state_root", state)
        object.__setattr__(self, "provider_id", provider)


class SystemProcessProbe:
    """Conservative liveness proof that rejects PID reuse by process start token."""

    def __init__(self, platform: PlatformEnvironment | None = None) -> None:
        self.platform = platform or PlatformEnvironment.from_runtime_labels(
            host_platform.system(), host_platform.machine()
        )

    @staticmethod
    def _posix_start(pid: int) -> tuple[str, str | None]:
        try:
            done = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False, capture_output=True, text=True, timeout=1.0,
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
            return ("DEAD", None) if int(kernel32.GetLastError()) == 87 else ("UNKNOWN", None)
        try:
            values = [ctypes.c_ulonglong() for _ in range(4)]
            ok = kernel32.GetProcessTimes(handle, *(ctypes.byref(v) for v in values))
            return ("ALIVE", str(int(values[0].value))) if ok else ("UNKNOWN", None)
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
            raise AbletonSessionLauncherError("cannot prove current N0TE process start identity")
        return ProcessIdentity.from_start_token(
            self.platform, pid=os.getpid(), start_token=self._token(raw)
        )

    def status(self, process: ProcessIdentity) -> str:
        if process.platform.os_family != self.platform.os_family:
            return "UNKNOWN"
        status, raw = self._raw_start(process.pid)
        if status != "ALIVE" or raw is None:
            return status
        digest = hashlib.sha256(self._token(raw).encode()).hexdigest()
        return "ALIVE" if digest == process.start_token_fingerprint else "DEAD"


def _profiles(data_root: Path) -> tuple[str, ...]:
    root = Path(data_root) / "profiles"
    if not root.is_dir():
        return ()
    try:
        return tuple(sorted(
            item.name for item in root.iterdir()
            if _PROFILE.fullmatch(item.name) and (item / LineageStore.DB_NAME).is_file()
        ))
    except OSError as exc:
        raise AbletonSessionLauncherError("cannot inspect N0TE profiles") from exc


def resolve_profile_id(data_root: Path, *, explicit: str | None, environment: Mapping[str, str]) -> str:
    requested = (explicit or environment.get("N0TE_PROFILE_ID") or "").strip()
    candidates = _profiles(data_root)
    if requested:
        if not _PROFILE.fullmatch(requested) or requested not in candidates:
            raise AbletonSessionLauncherError("configured N0TE profile does not exist or is invalid")
        return requested
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AbletonSessionLauncherError("no existing N0TE profile was found")
    raise AbletonSessionLauncherError("multiple N0TE profiles exist; choose --profile-id")


def _reference_backend(environment: Mapping[str, str]) -> str | None:
    raw = (environment.get("N0TE_REFERENCE_SEARCH_BACKEND") or "").strip()
    if raw:
        return raw.casefold().replace("-", "_")
    if (environment.get("N0TE_REFERENCE_SEARCH_ENDPOINT") or "").strip():
        return "http_json"
    if (environment.get("N0TE_OPENAI_API_KEY") or environment.get("OPENAI_API_KEY") or "").strip():
        return "openai_web"
    return None


def resolve_provider_id(*, explicit: str | None, environment: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    child_env = dict(environment)
    requested = (explicit or environment.get("N0TE_REFERENCE_SEARCH_PROVIDER_ID") or "").strip()
    if requested:
        return requested, child_env
    backend = _reference_backend(environment)
    if backend is None:
        raise AbletonSessionLauncherError("no reference provider is configured")
    if not (environment.get("N0TE_REFERENCE_SEARCH_BACKEND") or "").strip():
        child_env["N0TE_REFERENCE_SEARCH_BACKEND"] = backend
    if backend == "openai_web":
        return "openai-web-reference-search", child_env
    if backend == "http_json":
        return "configured-reference-search", child_env
    raise AbletonSessionLauncherError("provider id must be explicit for this backend")


def _coordinator_address(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(str(endpoint).strip())
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "http" or host not in _LOOPBACK or parsed.path.rstrip("/") != "/mcp":
        raise AbletonSessionLauncherError("coordinator endpoint must be loopback http /mcp")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise AbletonSessionLauncherError("coordinator endpoint contains unsupported components")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise AbletonSessionLauncherError("coordinator endpoint port is invalid") from exc
    return ("127.0.0.1" if host in {"localhost", "127.0.0.1"} else "::1", port)


class CoordinatorProcessSupervisor:
    def __init__(self, endpoint: str, *, environment: Mapping[str, str], popen_factory=subprocess.Popen) -> None:
        self.host, self.port = _coordinator_address(endpoint)
        self.environment = dict(environment)
        self._popen_factory = popen_factory
        self._process = None
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def _listening(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.15):
                return True
        except OSError:
            return False

    def start(self) -> str:
        if self._listening():
            return "REUSED"
        if _reference_backend(self.environment) is None:
            raise AbletonSessionLauncherError("cannot self-start coordinator without a reference provider")
        env = dict(self.environment)
        env.update(N0TE_MCP_TRANSPORT="streamable-http", N0TE_MCP_HOST=self.host, N0TE_MCP_PORT=str(self.port))
        try:
            process = self._popen_factory([sys.executable, "-m", "n0te.coordinator_mcp"], env=env, stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise AbletonSessionLauncherError("cannot start local N0TE coordinator") from exc
        self._process, self._owned = process, True
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._process, self._owned = None, False
                raise AbletonSessionLauncherError("local N0TE coordinator exited during startup")
            if self._listening():
                return "STARTED"
            time.sleep(0.05)
        self.stop()
        raise AbletonSessionLauncherError("local N0TE coordinator did not become reachable")

    def stop(self) -> None:
        process, owned = self._process, self._owned
        self._process, self._owned = None, False
        if not owned or process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=3.0)


async def _run_observer(service: AbletonObserverService, *, once: bool, output: Callable[[str], None]) -> None:
    if once:
        cycle = await service.refresh_references()
        error_class = getattr(cycle, "discovery_error_class", None)
        if error_class:
            raise AbletonSessionLauncherError(f"reference discovery failed: {error_class}")
        primary = None if cycle.references is None else cycle.references.get("primary")
        title = primary.get("title") if isinstance(primary, dict) else None
        suffix = f" • reference: {title}" if title else " • no reference candidate returned"
        output("N0TE Ableton smoke test complete" + suffix)
        return
    stop = asyncio.Event(); loop = asyncio.get_running_loop(); previous = {}
    def request_stop(signum, frame):  # noqa: ARG001
        loop.call_soon_threadsafe(stop.set)
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[int(sig)] = signal.getsignal(sig); signal.signal(sig, request_stop)
            except (ValueError, OSError):
                pass
    last = None
    def on_cycle(cycle):
        nonlocal last
        primary = cycle.references.get("primary") if cycle.discovery_performed and isinstance(cycle.references, dict) else None
        title = primary.get("title") if isinstance(primary, dict) else None
        if title and title != last:
            last = title; output(f"N0TE reference • {title}")
    try:
        await service.run(stop, on_cycle=on_cycle)
    finally:
        for raw, handler in previous.items():
            try: signal.signal(raw, handler)
            except (ValueError, OSError): pass


def run_session(config: AbletonSessionConfig, *, process_probe=None, runtime_factory=ApplicationRuntime,
                service_factory=AbletonObserverService.from_runtime,
                coordinator_factory=CoordinatorProcessSupervisor,
                coordinator_environment: Mapping[str, str] | None = None,
                output: Callable[[str], None] = print) -> int:
    probe = process_probe or SystemProcessProbe()
    runtime = runtime_factory(data_root=config.data_root, state_root=config.state_root)
    launch = runtime.launch(profile_id=config.profile_id, process=probe.current_process(), probe=probe)
    if launch.status != "STARTED":
        reason = f" ({launch.reason})" if getattr(launch, "reason", None) else ""
        raise AbletonSessionLauncherError(f"cannot own N0TE profile runtime: {launch.status}{reason}")
    coordinator = None; primary_error = None
    try:
        song = runtime.headquarters.store.active_song()
        if song is None:
            raise AbletonSessionLauncherError("selected profile has no active Song")
        coordinator = coordinator_factory(config.coordinator_endpoint, environment=dict(os.environ if coordinator_environment is None else coordinator_environment))
        status = coordinator.start()
        service = service_factory(runtime, provider_id=config.provider_id, bridge_endpoint=config.bridge_endpoint,
                                  coordinator_endpoint=config.coordinator_endpoint,
                                  poll_interval_seconds=config.poll_interval_seconds,
                                  reconnect_interval_seconds=config.reconnect_interval_seconds,
                                  discovery_retry_seconds=config.discovery_retry_seconds)
        output(f"N0TE Ableton session • {song.title} • coordinator {status.lower()}")
        if not config.once:
            output("N0TE Live bridge • reconnect is automatic while Ableton/N0TEBridge is unavailable")
        asyncio.run(_run_observer(service, once=config.once, output=output)); return 0
    except BaseException as exc:
        primary_error = exc; raise
    finally:
        coordinator_error = None
        if coordinator is not None:
            try: coordinator.stop()
            except BaseException as exc: coordinator_error = exc
        quit_result = runtime.quit()
        if primary_error is None and quit_result.status != "STOPPED":
            raise AbletonSessionLauncherError("N0TE runtime could not release its profile lease cleanly")
        if primary_error is None and coordinator_error is not None:
            raise AbletonSessionLauncherError("local coordinator could not stop cleanly") from coordinator_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m n0te.ableton_session_launcher")
    parser.add_argument("--profile-id"); parser.add_argument("--provider-id")
    parser.add_argument("--data-root"); parser.add_argument("--state-root")
    parser.add_argument("--bridge-endpoint", default=DEFAULT_ABLETON_BRIDGE_ENDPOINT)
    parser.add_argument("--coordinator-endpoint", default=DEFAULT_COORDINATOR_MCP_ENDPOINT)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--reconnect-interval", type=float, default=2.0)
    parser.add_argument("--discovery-retry", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    return parser


def build_config(args: argparse.Namespace, *, environment: Mapping[str, str] | None = None):
    env = dict(os.environ if environment is None else environment)
    platform = PlatformEnvironment.from_runtime_labels(host_platform.system(), host_platform.machine())
    roots = resolve_application_roots(platform, home=str(Path.home()), environment=env)
    data = Path(args.data_root).expanduser() if args.data_root else Path(str(roots.data_root))
    state = Path(args.state_root).expanduser() if args.state_root else Path(str(roots.state_root))
    profile = resolve_profile_id(data, explicit=args.profile_id, environment=env)
    provider, coordinator_env = resolve_provider_id(explicit=args.provider_id, environment=env)
    return AbletonSessionConfig(data, state, profile, provider, args.bridge_endpoint, args.coordinator_endpoint,
                                args.poll_interval, args.reconnect_interval, args.discovery_retry, args.once), coordinator_env


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config, coordinator_env = build_config(_parser().parse_args(argv))
        return run_session(config, coordinator_environment=coordinator_env)
    except (
        AbletonSessionLauncherError,
        AbletonHostBridgeError,
        AbletonObserverServiceError,
        ApplicationRuntimeError,
        CoordinatorGatewayError,
        LineageError,
    ) as exc:
        print(f"N0TE Ableton session error: {exc}", file=sys.stderr); return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())