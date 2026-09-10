from __future__ import annotations

import os
import sys
from typing import Mapping, Sequence

from . import fl_studio_bridge_installer
from .app_runtime import ApplicationRuntimeError
from .coordinator_gateway import CoordinatorGatewayError
from .fl_studio_bridge_installer import FLStudioBridgeInstallerError
from .fl_studio_host_bridge import FLStudioHostBridgeError
from .fl_studio_observer_service import FLStudioObserverServiceError
from .fl_studio_session_launcher import (
    FLStudioSessionLauncherError,
    _parser as _session_parser,
    build_config,
    run_session,
)
from .host_runtime import reference_route_kind
from .lineage import LineageError


class FLStudioCommandError(RuntimeError):
    """The artist-facing FL Studio command cannot proceed safely."""


def _reference_route_kind(
    environment: Mapping[str, str],
) -> str | None:
    return reference_route_kind(environment)


def validate_network_preflight(
    environment: Mapping[str, str],
) -> None:
    if _reference_route_kind(environment) != "INTERNET":
        return
    mode = (
        environment.get("N0TE_NETWORK_MODE") or "OFFLINE"
    ).strip().upper()
    if mode != "CONNECTED":
        raise FLStudioCommandError(
            "internet reference discovery is configured while N0TE "
            "network policy is not CONNECTED; set "
            "N0TE_NETWORK_MODE=CONNECTED to allow this session"
        )


def run_command(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> int:
    validate_network_preflight(environment)
    parser = _session_parser()
    parser.prog = "python -m n0te.fl_studio"
    args = parser.parse_args(list(argv))
    config, coordinator_env = build_config(
        args,
        environment=environment,
    )
    return run_session(
        config,
        coordinator_environment=coordinator_env,
    )


def _split_command(
    argv: Sequence[str],
) -> tuple[str, list[str]]:
    args = list(argv)
    if args and args[0] == "install":
        return "install", args[1:]
    if args and args[0] == "run":
        return "run", args[1:]
    return "run", args


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    command, rest = _split_command(
        sys.argv[1:] if argv is None else argv
    )
    if command == "install":
        return fl_studio_bridge_installer.main(rest)

    env = dict(os.environ if environment is None else environment)
    try:
        return run_command(rest, environment=env)
    except FLStudioHostBridgeError as exc:
        print(
            f"N0TE FL Studio session error: {exc}",
            file=sys.stderr,
        )
        print(
            "Install or refresh the FL bridge with: "
            "python -m n0te.fl_studio install",
            file=sys.stderr,
        )
        print(
            "Then select N0TEBridge for an input port in "
            "FL Studio MIDI Settings.",
            file=sys.stderr,
        )
        return 2
    except (
        FLStudioCommandError,
        FLStudioSessionLauncherError,
        FLStudioBridgeInstallerError,
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
