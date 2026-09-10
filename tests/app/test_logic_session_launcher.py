from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.logic_session_launcher import (
    LogicSessionConfig,
    LogicSessionLauncherError,
    _run_observer,
    run_session,
)


class _FakeProbe:
    def current_process(self):
        return object()

    def status(self, process):
        return "ALIVE"


class _FakeStore:
    def __init__(self, song):
        self._song = song

    def active_song(self):
        return self._song


class _FakeRuntime:
    instances = []

    def __init__(self, *, data_root, state_root):
        self.headquarters = SimpleNamespace(
            store=_FakeStore(SimpleNamespace(title="Logic Launcher Song"))
        )
        self.quit_calls = 0
        self.__class__.instances.append(self)

    def launch(self, *, profile_id, process, probe):
        self.launched = (profile_id, process, probe)
        return SimpleNamespace(status="STARTED", reason=None)

    def quit(self):
        self.quit_calls += 1
        return SimpleNamespace(status="STOPPED", reason=None)


class _FakeCoordinator:
    instances = []

    def __init__(self, endpoint, *, environment):
        self.endpoint = endpoint
        self.environment = environment
        self.started = 0
        self.stopped = 0
        self.__class__.instances.append(self)

    def start(self):
        self.started += 1
        return "STARTED"

    def stop(self):
        self.stopped += 1


class _FakeService:
    def __init__(self):
        self.refresh_calls = 0

    async def refresh_references(self):
        self.refresh_calls += 1
        return SimpleNamespace(
            references={"primary": {"title": "Logic Reference"}},
            discovery_error_class=None,
        )


def _config(tmp_path: Path) -> LogicSessionConfig:
    return LogicSessionConfig(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        profile_id="prf_" + "a" * 32,
        provider_id="provider",
        once=True,
    )


def test_run_session_owns_runtime_coordinator_and_one_shot_cycle(tmp_path):
    _FakeRuntime.instances.clear()
    _FakeCoordinator.instances.clear()
    service = _FakeService()
    service_calls = []
    output = []

    def service_factory(runtime, **kwargs):
        service_calls.append((runtime, kwargs))
        return service

    result = run_session(
        _config(tmp_path),
        process_probe=_FakeProbe(),
        runtime_factory=_FakeRuntime,
        service_factory=service_factory,
        coordinator_factory=_FakeCoordinator,
        coordinator_environment={"N0TE_REFERENCE_SEARCH_BACKEND": "openai_web"},
        output=output.append,
    )

    assert result == 0
    assert _FakeRuntime.instances[-1].quit_calls == 1
    assert _FakeCoordinator.instances[-1].started == 1
    assert _FakeCoordinator.instances[-1].stopped == 1
    assert service.refresh_calls == 1
    assert service_calls[0][1]["midi_port_name"] == "Logic Pro Virtual Out"
    assert any("Logic Launcher Song" in line for line in output)
    assert any("Logic Reference" in line for line in output)


def test_run_session_releases_runtime_when_active_song_is_missing(tmp_path):
    class NoSongRuntime(_FakeRuntime):
        instances = []

        def __init__(self, *, data_root, state_root):
            super().__init__(data_root=data_root, state_root=state_root)
            self.headquarters = SimpleNamespace(store=_FakeStore(None))
            self.__class__.instances.append(self)

    with pytest.raises(LogicSessionLauncherError, match="no active Song"):
        run_session(
            _config(tmp_path),
            process_probe=_FakeProbe(),
            runtime_factory=NoSongRuntime,
            coordinator_factory=_FakeCoordinator,
            coordinator_environment={},
        )
    assert NoSongRuntime.instances[-1].quit_calls == 1


def test_once_smoke_test_surfaces_reference_discovery_failure():
    class BrokenService:
        async def refresh_references(self):
            return SimpleNamespace(
                references=None,
                discovery_error_class="CoordinatorReferenceClientError",
            )

    with pytest.raises(LogicSessionLauncherError, match="reference discovery failed"):
        asyncio.run(
            _run_observer(BrokenService(), once=True, output=lambda message: None)
        )


def test_foreign_profile_lease_is_never_released(tmp_path):
    class HeldRuntime(_FakeRuntime):
        instances = []

        def launch(self, *, profile_id, process, probe):
            return SimpleNamespace(
                status="HELD_BY_OTHER",
                reason="another verified-live exact launch owns this profile",
            )

    with pytest.raises(LogicSessionLauncherError, match="another verified-live"):
        run_session(
            _config(tmp_path),
            process_probe=_FakeProbe(),
            runtime_factory=HeldRuntime,
            coordinator_factory=_FakeCoordinator,
            coordinator_environment={},
        )
    assert HeldRuntime.instances[-1].quit_calls == 0
