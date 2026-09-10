from __future__ import annotations

from pathlib import Path

import pytest

from n0te.desktop import (
    DesktopConfig,
    DesktopLauncherError,
    resolve_desktop_config,
    run_desktop,
)
from n0te.platforms import PlatformEnvironment


def test_desktop_config_uses_native_macos_application_roots() -> None:
    config = resolve_desktop_config(
        data_root=None,
        state_root=None,
        port=0,
        open_browser=False,
        smoke_test=True,
        environment={},
        os_name="Darwin",
        machine="arm64",
        home="/Users/n0te-test",
    )

    assert config.platform.os_family == "MACOS"
    assert config.platform.architecture == "ARM64"
    assert config.data_root == Path("/Users/n0te-test/Library/Application Support/N0TE")
    assert config.state_root == Path("/Users/n0te-test/Library/Application Support/N0TE/State")


def test_desktop_config_rejects_relative_storage_override() -> None:
    with pytest.raises(DesktopLauncherError, match="absolute"):
        DesktopConfig(
            platform=PlatformEnvironment.from_runtime_labels("Darwin", "arm64"),
            data_root=Path("relative/data"),
            state_root=Path("/tmp/n0te-state"),
        )


class _Probe:
    def __init__(self):
        self.process = object()

    def current_process(self):
        return self.process

    def status(self, process):
        return "ALIVE" if process is self.process else "UNKNOWN"


class _Shell:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    @property
    def url(self):
        return "http://127.0.0.1:41234/"

    @property
    def is_running(self):
        return self.started and not self.stopped

    def start(self):
        self.started = True
        return object()

    def stop(self, *, timeout=2.0):
        self.stopped = True

    def wait_stopped(self, *, timeout=2.0):
        return self.stopped


def _config(tmp_path: Path, *, browser: bool = False, smoke: bool = True) -> DesktopConfig:
    return DesktopConfig(
        platform=PlatformEnvironment.from_runtime_labels("Darwin", "arm64"),
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        open_browser=browser,
        smoke_test=smoke,
    )


def test_desktop_smoke_owns_one_loopback_shell_and_quits_cleanly(tmp_path: Path) -> None:
    _Shell.instances.clear()
    probe = _Probe()
    output = []

    result = run_desktop(
        _config(tmp_path),
        probe=probe,
        shell_factory=_Shell,
        browser_open=lambda url: (_ for _ in ()).throw(AssertionError("browser must stay closed")),
        output=output.append,
    )

    assert result == 0
    assert len(_Shell.instances) == 1
    shell = _Shell.instances[0]
    assert shell.kwargs["process"] is probe.process
    assert shell.kwargs["probe"] is probe
    assert shell.kwargs["port"] == 0
    assert shell.started is True
    assert shell.stopped is True
    assert output == ["N0TE desktop • MACOS ARM64 • http://127.0.0.1:41234/"]


def test_desktop_browser_failure_keeps_local_url_visible_and_stops(tmp_path: Path) -> None:
    _Shell.instances.clear()
    output = []
    opened = []

    result = run_desktop(
        _config(tmp_path, browser=True),
        probe=_Probe(),
        shell_factory=_Shell,
        browser_open=lambda url: opened.append(url) or False,
        output=output.append,
    )

    assert result == 0
    assert opened == ["http://127.0.0.1:41234/"]
    assert output[-1] == "N0TE desktop is ready locally • open http://127.0.0.1:41234/"
    assert _Shell.instances[0].stopped is True
