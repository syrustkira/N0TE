from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.fl_studio_session_launcher import (
    FLStudioSessionConfig,
    FLStudioSessionLauncherError,
    _run_observer,
    resolve_snapshot_path,
    run_session,
)
from n0te.platforms import PlatformEnvironment


def test_snapshot_resolution_prefers_explicit_path_and_uses_installed_bridge_location(tmp_path: Path):
    platform = PlatformEnvironment.from_runtime_labels("Windows", "x86_64")
    explicit = tmp_path / "custom" / "snapshot.json"
    assert resolve_snapshot_path(
        explicit_snapshot=explicit,
        explicit_user_data=None,
        environment={},
        platform=platform,
        home=tmp_path,
    ) == explicit

    user_data = tmp_path / "FL Studio"
    user_data.mkdir()
    assert resolve_snapshot_path(
        explicit_snapshot=None,
        explicit_user_data=user_data,
        environment={},
        platform=platform,
        home=tmp_path,
    ) == user_data / "Settings" / "Hardware" / "N0TEBridge" / "n0te_snapshot.json"

    with pytest.raises(FLStudioSessionLauncherError, match="absolute"):
        resolve_snapshot_path(
            explicit_snapshot="relative.json",
            explicit_user_data=None,
            environment={},
            platform=platform,
            home=tmp_path,
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
        self.data_root = data_root
        self.state_root = state_root
        self.headquarters = SimpleNamespace(
            store=_FakeStore(SimpleNamespace(title="FL Launcher Song"))
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
            references={"primary": {"title": "FL Reference"}},
            discovery_error_class=None,
        )


def test_run_session_owns_runtime_coordinator_and_one_shot_reference_cycle(tmp_path: Path):
    _FakeRuntime.instances.clear()
    _FakeCoordinator.instances.clear()
    service = _FakeService()
    service_calls = []
    output = []

    def service_factory(runtime, **kwargs):
        service_calls.append((runtime, kwargs))
        return service

    config = FLStudioSessionConfig(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        profile_id="prf_" + "a" * 32,
        provider_id="provider",
        snapshot_path=tmp_path / "snapshot.json",
        once=True,
    )
    result = run_session(
        config,
        process_probe=_FakeProbe(),
        runtime_factory=_FakeRuntime,
        service_factory=service_factory,
        coordinator_factory=_FakeCoordinator,
        coordinator_environment={"N0TE_REFERENCE_SEARCH_BACKEND": "openai_web"},
        output=output.append,
    )

    assert result == 0
    runtime = _FakeRuntime.instances[-1]
    coordinator = _FakeCoordinator.instances[-1]
    assert runtime.quit_calls == 1
    assert coordinator.started == 1
    assert coordinator.stopped == 1
    assert service.refresh_calls == 1
    assert service_calls[0][1]["provider_id"] == "provider"
    assert service_calls[0][1]["snapshot_path"] == config.snapshot_path
    assert any("FL Launcher Song" in line for line in output)
    assert any("FL Reference" in line for line in output)


def test_run_session_releases_runtime_when_active_song_is_missing(tmp_path: Path):
    class NoSongRuntime(_FakeRuntime):
        instances = []

        def __init__(self, *, data_root, state_root):
            super().__init__(data_root=data_root, state_root=state_root)
            self.headquarters = SimpleNamespace(store=_FakeStore(None))
            self.__class__.instances.append(self)

    config = FLStudioSessionConfig(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        profile_id="prf_" + "b" * 32,
        provider_id="provider",
        snapshot_path=tmp_path / "snapshot.json",
        once=True,
    )
    with pytest.raises(FLStudioSessionLauncherError, match="no active Song"):
        run_session(
            config,
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

    with pytest.raises(FLStudioSessionLauncherError, match="reference discovery failed"):
        asyncio.run(
            _run_observer(BrokenService(), once=True, output=lambda message: None)
        )


def test_foreign_profile_lease_is_never_released_by_unowned_launcher(tmp_path: Path):
    class HeldRuntime(_FakeRuntime):
        instances = []

        def launch(self, *, profile_id, process, probe):
            return SimpleNamespace(
                status="HELD_BY_OTHER",
                reason="another verified-live exact launch owns this profile",
            )

    config = FLStudioSessionConfig(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        profile_id="prf_" + "c" * 32,
        provider_id="provider",
        snapshot_path=tmp_path / "snapshot.json",
        once=True,
    )
    with pytest.raises(FLStudioSessionLauncherError, match="another verified-live"):
        run_session(
            config,
            process_probe=_FakeProbe(),
            runtime_factory=HeldRuntime,
            coordinator_factory=_FakeCoordinator,
            coordinator_environment={},
        )
    assert HeldRuntime.instances[-1].quit_calls == 0
