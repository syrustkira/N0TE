from __future__ import annotations

import argparse
import os
import platform as host_platform
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .consumer_shell import ConsumerShell, ConsumerShellError
from .host_runtime import SystemProcessProbe
from .platforms import PlatformEnvironment, PlatformError, resolve_application_roots


class DesktopLauncherError(RuntimeError):
    """The local N0TE desktop front door cannot proceed safely."""


@dataclass(frozen=True)
class DesktopConfig:
    platform: PlatformEnvironment
    data_root: Path
    state_root: Path
    port: int = 0
    open_browser: bool = True
    smoke_test: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.platform, PlatformEnvironment):
            raise TypeError("platform must be PlatformEnvironment")
        data = Path(self.data_root)
        state = Path(self.state_root)
        if not data.is_absolute() or not state.is_absolute():
            raise DesktopLauncherError("desktop data/state roots must be absolute")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 0 <= self.port <= 65535:
            raise DesktopLauncherError("desktop port must be an integer from 0 to 65535")
        if type(self.open_browser) is not bool or type(self.smoke_test) is not bool:
            raise DesktopLauncherError("desktop boolean options must be bool")
        object.__setattr__(self, "data_root", data)
        object.__setattr__(self, "state_root", state)


def resolve_desktop_config(
    *,
    data_root: str | Path | None,
    state_root: str | Path | None,
    port: int,
    open_browser: bool,
    smoke_test: bool,
    environment: Mapping[str, str],
    os_name: str | None = None,
    machine: str | None = None,
    home: str | Path | None = None,
) -> DesktopConfig:
    platform = PlatformEnvironment.from_runtime_labels(
        host_platform.system() if os_name is None else os_name,
        host_platform.machine() if machine is None else machine,
    )
    home_path = Path.home() if home is None else Path(home).expanduser()
    roots = resolve_application_roots(
        platform,
        home=str(home_path),
        environment=environment,
    )
    resolved_data = (
        Path(str(roots.data_root))
        if data_root is None
        else Path(data_root).expanduser()
    )
    resolved_state = (
        Path(str(roots.state_root))
        if state_root is None
        else Path(state_root).expanduser()
    )
    return DesktopConfig(
        platform=platform,
        data_root=resolved_data,
        state_root=resolved_state,
        port=port,
        open_browser=open_browser,
        smoke_test=smoke_test,
    )


def run_desktop(
    config: DesktopConfig,
    *,
    probe=None,
    shell_factory=ConsumerShell,
    browser_open: Callable[[str], bool] = webbrowser.open,
    output: Callable[[str], None] = print,
) -> int:
    """Start the existing loopback ConsumerShell as the desktop lifecycle owner.

    This launcher owns process identity and user-visible launch/quit mechanics only.
    ConsumerShell remains the local HTTP/security boundary and ApplicationRuntime
    remains canonical Artist/Profile/Song ownership. No DAW or provider mutation
    authority is introduced here.
    """

    if not isinstance(config, DesktopConfig):
        raise TypeError("config must be DesktopConfig")
    if not callable(shell_factory) or not callable(browser_open) or not callable(output):
        raise TypeError("desktop factories/output must be callable")

    process_probe = probe or SystemProcessProbe(
        config.platform,
        error_type=DesktopLauncherError,
    )
    if not callable(getattr(process_probe, "current_process", None)):
        raise TypeError("probe must expose current_process()")
    process = process_probe.current_process()
    shell = shell_factory(
        data_root=config.data_root,
        state_root=config.state_root,
        process=process,
        probe=process_probe,
        port=config.port,
    )

    started = False
    try:
        shell.start()
        started = True
        output(
            f"N0TE desktop • {config.platform.os_family} {config.platform.architecture} • {shell.url}"
        )
        if config.open_browser:
            opened = False
            try:
                opened = bool(browser_open(shell.url))
            except Exception:
                opened = False
            if not opened:
                output(f"N0TE desktop is ready locally • open {shell.url}")

        if config.smoke_test:
            shell.stop()
            return 0

        try:
            while not shell.wait_stopped(timeout=0.25):
                pass
        except KeyboardInterrupt:
            shell.stop()
        return 0
    finally:
        if started and shell.is_running:
            shell.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m n0te.desktop")
    parser.add_argument("--data-root")
    parser.add_argument("--state-root")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="start the local desktop shell, prove it entered its serve loop, then quit cleanly",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    env = dict(os.environ if environment is None else environment)
    try:
        config = resolve_desktop_config(
            data_root=args.data_root,
            state_root=args.state_root,
            port=args.port,
            open_browser=not args.no_browser,
            smoke_test=args.smoke_test,
            environment=env,
        )
        return run_desktop(config)
    except (DesktopLauncherError, ConsumerShellError, PlatformError) as exc:
        print(f"N0TE desktop error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
