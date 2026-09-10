from __future__ import annotations

import argparse
import asyncio
import os
import platform as host_platform
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .app_runtime import ApplicationRuntime, ApplicationRuntimeError
from .coordinator_gateway import (
    DEFAULT_COORDINATOR_MCP_ENDPOINT,
    CoordinatorGatewayError,
)
from .fl_studio_bridge_installer import (
    SCRIPT_DIR_NAME,
    SNAPSHOT_FILE_NAME,
    FLStudioBridgeInstallerError,
    resolve_user_data,
)
from .fl_studio_host_bridge import FLStudioHostBridgeError
from .fl_studio_observer_service import (
    FLStudioObserverService,
    FLStudioObserverServiceError,
)
from .host_runtime import (
    CoordinatorProcessSupervisor as _CoordinatorProcessSupervisor,
    SystemProcessProbe as _SystemProcessProbe,
    resolve_profile_id as _resolve_profile_id,
    resolve_provider_id as _resolve_provider_id,
)
from .lineage import LineageError
from .platforms import PlatformEnvironment, resolve_application_roots

SNAPSHOT_PATH_ENV = "N0TE_FL_STUDIO_SNAPSHOT_PATH"


class FLStudioSessionLauncherError(RuntimeError):
    pass


class SystemProcessProbe(_SystemProcessProbe):
    def __init__(self, platform: PlatformEnvironment | None = None) -> None:
        super().__init__(
            platform,
            error_type=FLStudioSessionLauncherError,
        )


class CoordinatorProcessSupervisor(_CoordinatorProcessSupervisor):
    def __init__(
        self,
        endpoint: str,
        *,
        environment: Mapping[str, str],
        popen_factory=None,
    ) -> None:
        kwargs = {
            "environment": environment,
            "error_type": FLStudioSessionLauncherError,
        }
        if popen_factory is not None:
            kwargs["popen_factory"] = popen_factory
        super().__init__(endpoint, **kwargs)


def resolve_profile_id(
    data_root: Path,
    *,
    explicit: str | None,
    environment: Mapping[str, str],
) -> str:
    return _resolve_profile_id(
        data_root,
        explicit=explicit,
        environment=environment,
        error_type=FLStudioSessionLauncherError,
    )


def resolve_provider_id(
    *,
    explicit: str | None,
    environment: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    return _resolve_provider_id(
        explicit=explicit,
        environment=environment,
        error_type=FLStudioSessionLauncherError,
    )


@dataclass(frozen=True)
class FLStudioSessionConfig:
    data_root: Path
    state_root: Path
    profile_id: str
    provider_id: str
    snapshot_path: Path
    coordinator_endpoint: str = DEFAULT_COORDINATOR_MCP_ENDPOINT
    poll_interval_seconds: float = 1.0
    reconnect_interval_seconds: float = 2.0
    discovery_retry_seconds: float = 30.0
    once: bool = False

    def __post_init__(self) -> None:
        data = Path(self.data_root)
        state = Path(self.state_root)
        snapshot = Path(self.snapshot_path)
        if (
            not data.is_absolute()
            or not state.is_absolute()
            or not snapshot.is_absolute()
        ):
            raise FLStudioSessionLauncherError(
                "session and snapshot paths must be absolute"
            )
        if not str(self.profile_id).strip():
            raise FLStudioSessionLauncherError(
                "profile_id must not be empty"
            )
        if not str(self.provider_id).strip():
            raise FLStudioSessionLauncherError(
                "provider_id must not be empty"
            )
        object.__setattr__(self, "data_root", data)
        object.__setattr__(self, "state_root", state)
        object.__setattr__(self, "snapshot_path", snapshot)


def resolve_snapshot_path(
    *,
    explicit_snapshot: str | Path | None,
    explicit_user_data: str | Path | None,
    environment: Mapping[str, str],
    platform: PlatformEnvironment,
    home: Path,
) -> Path:
    raw = (
        explicit_snapshot
        if explicit_snapshot is not None
        else environment.get(SNAPSHOT_PATH_ENV)
    )
    if raw is not None and str(raw).strip():
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            raise FLStudioSessionLauncherError(
                "FL Studio snapshot path must be absolute"
            )
        return path
    user_data = resolve_user_data(
        explicit=explicit_user_data,
        environment=environment,
        platform=platform,
        home=home,
    )
    return (
        user_data
        / "Settings"
        / "Hardware"
        / SCRIPT_DIR_NAME
        / SNAPSHOT_FILE_NAME
    )


async def _run_observer(
    service: FLStudioObserverService,
    *,
    once: bool,
    output: Callable[[str], None],
) -> None:
    if once:
        cycle = await service.refresh_references()
        error_class = getattr(cycle, "discovery_error_class", None)
        if error_class:
            raise FLStudioSessionLauncherError(
                f"reference discovery failed: {error_class}"
            )
        primary = (
            None
            if cycle.references is None
            else cycle.references.get("primary")
        )
        title = primary.get("title") if isinstance(primary, dict) else None
        suffix = (
            f" • reference: {title}"
            if title
            else " • no reference candidate returned"
        )
        output("N0TE FL Studio smoke test complete" + suffix)
        return

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}

    def request_stop(signum, frame):  # noqa: ARG001
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                previous[int(sig)] = signal.getsignal(sig)
                signal.signal(sig, request_stop)
            except (ValueError, OSError):
                pass

    last = None

    def on_cycle(cycle):
        nonlocal last
        primary = (
            cycle.references.get("primary")
            if cycle.discovery_performed
            and isinstance(cycle.references, dict)
            else None
        )
        title = primary.get("title") if isinstance(primary, dict) else None
        if title and title != last:
            last = title
            output(f"N0TE reference • {title}")

    try:
        await service.run(stop, on_cycle=on_cycle)
    finally:
        for raw, handler in previous.items():
            try:
                signal.signal(raw, handler)
            except (ValueError, OSError):
                pass


def run_session(
    config: FLStudioSessionConfig,
    *,
    process_probe=None,
    runtime_factory=ApplicationRuntime,
    service_factory=FLStudioObserverService.from_runtime,
    coordinator_factory=CoordinatorProcessSupervisor,
    coordinator_environment: Mapping[str, str] | None = None,
    output: Callable[[str], None] = print,
) -> int:
    probe = process_probe or SystemProcessProbe()
    runtime = runtime_factory(
        data_root=config.data_root,
        state_root=config.state_root,
    )
    launch = runtime.launch(
        profile_id=config.profile_id,
        process=probe.current_process(),
        probe=probe,
    )
    if launch.status != "STARTED":
        reason = (
            f" ({launch.reason})"
            if getattr(launch, "reason", None)
            else ""
        )
        raise FLStudioSessionLauncherError(
            f"cannot own N0TE profile runtime: "
            f"{launch.status}{reason}"
        )

    coordinator = None
    primary_error = None
    try:
        song = runtime.headquarters.store.active_song()
        if song is None:
            raise FLStudioSessionLauncherError(
                "selected profile has no active Song"
            )
        coordinator = coordinator_factory(
            config.coordinator_endpoint,
            environment=dict(
                os.environ
                if coordinator_environment is None
                else coordinator_environment
            ),
        )
        status = coordinator.start()
        service = service_factory(
            runtime,
            provider_id=config.provider_id,
            snapshot_path=config.snapshot_path,
            coordinator_endpoint=config.coordinator_endpoint,
            poll_interval_seconds=config.poll_interval_seconds,
            reconnect_interval_seconds=config.reconnect_interval_seconds,
            discovery_retry_seconds=config.discovery_retry_seconds,
        )
        output(
            f"N0TE FL Studio session • {song.title} • "
            f"coordinator {status.lower()}"
        )
        if not config.once:
            output(
                "N0TE FL bridge • reconnect is automatic while "
                "the FL snapshot is unavailable"
            )
        asyncio.run(
            _run_observer(
                service,
                once=config.once,
                output=output,
            )
        )
        return 0
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        coordinator_error = None
        if coordinator is not None:
            try:
                coordinator.stop()
            except BaseException as exc:
                coordinator_error = exc
        quit_result = runtime.quit()
        if primary_error is None and quit_result.status != "STOPPED":
            raise FLStudioSessionLauncherError(
                "N0TE runtime could not release its profile lease cleanly"
            )
        if primary_error is None and coordinator_error is not None:
            raise FLStudioSessionLauncherError(
                "local coordinator could not stop cleanly"
            ) from coordinator_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m n0te.fl_studio_session_launcher"
    )
    parser.add_argument("--profile-id")
    parser.add_argument("--provider-id")
    parser.add_argument("--data-root")
    parser.add_argument("--state-root")
    parser.add_argument("--user-data")
    parser.add_argument("--snapshot-path")
    parser.add_argument(
        "--coordinator-endpoint",
        default=DEFAULT_COORDINATOR_MCP_ENDPOINT,
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--reconnect-interval", type=float, default=2.0)
    parser.add_argument("--discovery-retry", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    return parser


def build_config(
    args: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None = None,
):
    env = dict(os.environ if environment is None else environment)
    platform = PlatformEnvironment.from_runtime_labels(
        host_platform.system(),
        host_platform.machine(),
    )
    roots = resolve_application_roots(
        platform,
        home=str(Path.home()),
        environment=env,
    )
    data = (
        Path(args.data_root).expanduser()
        if args.data_root
        else Path(str(roots.data_root))
    )
    state = (
        Path(args.state_root).expanduser()
        if args.state_root
        else Path(str(roots.state_root))
    )
    profile = resolve_profile_id(
        data,
        explicit=args.profile_id,
        environment=env,
    )
    provider, coordinator_env = resolve_provider_id(
        explicit=args.provider_id,
        environment=env,
    )
    snapshot_path = resolve_snapshot_path(
        explicit_snapshot=args.snapshot_path,
        explicit_user_data=args.user_data,
        environment=env,
        platform=platform,
        home=Path.home(),
    )
    return (
        FLStudioSessionConfig(
            data,
            state,
            profile,
            provider,
            snapshot_path,
            args.coordinator_endpoint,
            args.poll_interval,
            args.reconnect_interval,
            args.discovery_retry,
            args.once,
        ),
        coordinator_env,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config, coordinator_env = build_config(
            _parser().parse_args(argv)
        )
        return run_session(
            config,
            coordinator_environment=coordinator_env,
        )
    except (
        FLStudioSessionLauncherError,
        FLStudioBridgeInstallerError,
        FLStudioHostBridgeError,
        FLStudioObserverServiceError,
        ApplicationRuntimeError,
        CoordinatorGatewayError,
        LineageError,
    ) as exc:
        print(
            f"N0TE FL Studio session error: {exc}",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
