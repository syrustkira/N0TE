from __future__ import annotations

import os
import sys
from typing import Mapping, Sequence

from .app_runtime import ApplicationRuntimeError
from .coordinator_gateway import CoordinatorGatewayError
from .host_runtime import reference_route_kind
from .lineage import LineageError
from .studio_one_midi_bridge import (
    DEFAULT_STUDIO_ONE_CLOCK_PORT,
    StudioOneMidiBridgeError,
)
from .studio_one_observer_service import StudioOneObserverServiceError
from .studio_one_session_launcher import (
    StudioOneSessionLauncherError,
    _parser as _session_parser,
    build_config,
    run_session,
)


class StudioOneCommandError(RuntimeError):
    """The artist-facing Studio One command cannot proceed safely."""


def validate_network_preflight(environment: Mapping[str, str]) -> None:
    if reference_route_kind(environment) != "INTERNET":
        return
    mode = (environment.get("N0TE_NETWORK_MODE") or "OFFLINE").strip().upper()
    if mode != "CONNECTED":
        raise StudioOneCommandError(
            "internet reference discovery is configured while N0TE network policy "
            "is not CONNECTED; set N0TE_NETWORK_MODE=CONNECTED to allow this session"
        )


def setup_text() -> str:
    return "\n".join(
        (
            "N0TE Studio One timing setup:",
            f"1. Use a local MIDI destination named {DEFAULT_STUDIO_ONE_CLOCK_PORT}.",
            "   On macOS, running N0TE can expose this virtual destination; on Windows, create a local loopback MIDI port first.",
            "2. In Studio One Preferences/Options > External Devices, add a New Instrument named N0TE Clock.",
            f"3. Set Send To to {DEFAULT_STUDIO_ONE_CLOCK_PORT}; N0TE does not require a Receive From assignment.",
            "4. Enable MIDI Clock and MIDI Clock Start for that External Instrument.",
            "5. Run: python -m n0te.studio_one --once, then press Play long enough for one stable clock window.",
            "6. This route observes timing only. Restart the N0TE Studio One session after switching Studio One Songs.",
        )
    )


def run_command(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> int:
    validate_network_preflight(environment)
    parser = _session_parser()
    parser.prog = "python -m n0te.studio_one"
    args = parser.parse_args(list(argv))
    config, coordinator_env = build_config(args, environment=environment)
    return run_session(config, coordinator_environment=coordinator_env)


def _split_command(argv: Sequence[str]) -> tuple[str, list[str]]:
    args = list(argv)
    if args and args[0] == "setup":
        return "setup", args[1:]
    if args and args[0] == "run":
        return "run", args[1:]
    return "run", args


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    command, rest = _split_command(sys.argv[1:] if argv is None else argv)
    if command == "setup":
        if rest:
            print(
                "N0TE Studio One setup error: setup takes no arguments",
                file=sys.stderr,
            )
            return 2
        print(setup_text())
        return 0

    env = dict(os.environ if environment is None else environment)
    try:
        return run_command(rest, environment=env)
    except StudioOneMidiBridgeError as exc:
        print(f"N0TE Studio One session error: {exc}", file=sys.stderr)
        print(
            "Run setup guidance with: python -m n0te.studio_one setup",
            file=sys.stderr,
        )
        return 2
    except (
        StudioOneCommandError,
        StudioOneSessionLauncherError,
        StudioOneObserverServiceError,
        ApplicationRuntimeError,
        CoordinatorGatewayError,
        LineageError,
    ) as exc:
        print(f"N0TE Studio One session error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
