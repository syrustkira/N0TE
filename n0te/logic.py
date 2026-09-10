from __future__ import annotations

import os
import sys
from typing import Mapping, Sequence

from .app_runtime import ApplicationRuntimeError
from .coordinator_gateway import CoordinatorGatewayError
from .host_runtime import reference_route_kind
from .lineage import LineageError
from .logic_midi_bridge import LogicMidiBridgeError
from .logic_observer_service import LogicObserverServiceError
from .logic_session_launcher import (
    LogicSessionLauncherError,
    _parser as _session_parser,
    build_config,
    run_session,
)


class LogicCommandError(RuntimeError):
    """The artist-facing Logic command cannot proceed safely."""


def validate_network_preflight(environment: Mapping[str, str]) -> None:
    if reference_route_kind(environment) != "INTERNET":
        return
    mode = (environment.get("N0TE_NETWORK_MODE") or "OFFLINE").strip().upper()
    if mode != "CONNECTED":
        raise LogicCommandError(
            "internet reference discovery is configured while N0TE network policy "
            "is not CONNECTED; set N0TE_NETWORK_MODE=CONNECTED to allow this session"
        )


def setup_text() -> str:
    return "\n".join(
        (
            "N0TE Logic Pro timing setup:",
            "1. Create a dedicated software-instrument channel strip named N0TE Monitor.",
            "2. Insert Scripter in the MIDI FX slot and load/copy integrations/logic/N0TETimingProbe.js.",
            "3. Insert External Instrument in the Instrument slot and choose Logic Pro Virtual Out as the MIDI Destination.",
            "4. Leave this monitor strip free of musical material; it reports timing evidence only.",
            "5. Run: python -m n0te.logic --once",
        )
    )


def run_command(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> int:
    validate_network_preflight(environment)
    parser = _session_parser()
    parser.prog = "python -m n0te.logic"
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
            print("N0TE Logic Pro setup error: setup takes no arguments", file=sys.stderr)
            return 2
        print(setup_text())
        return 0

    env = dict(os.environ if environment is None else environment)
    try:
        return run_command(rest, environment=env)
    except LogicMidiBridgeError as exc:
        print(f"N0TE Logic Pro session error: {exc}", file=sys.stderr)
        print("Run setup guidance with: python -m n0te.logic setup", file=sys.stderr)
        return 2
    except (
        LogicCommandError,
        LogicSessionLauncherError,
        LogicObserverServiceError,
        ApplicationRuntimeError,
        CoordinatorGatewayError,
        LineageError,
    ) as exc:
        print(f"N0TE Logic Pro session error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
