from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.platforms import PlatformEnvironment
from n0te.reaper_session_launcher import (
    ReaperSessionConfig,
    ReaperSessionLauncherError,
    _run_observer,
    resolve_snapshot_path,
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
            store=_FakeStore(SimpleNamespace(title="REAPER Launcher Song"))
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
            references={"primary": {"title": "REAPER Reference"}},
            discovery_error_class=None,
        )


def _config(tmp_path: Path) -> ReaperSessionConfig:
    return ReaperSessionConfig(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        profile_id="prf_" + "a" * 32,
        provider_id="provider",
        snapshot_path=tmp_path / "snapshot.json",
        once=True,
    )


def test_snapshot_resolution_prefers_explicit_and_uses_resource_bridge_location(tmp_path):
    platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
    explicit = tmp_path / "custom" / "snapshot.json"
    assert resolve_snapshot_path(
        explicit_snapshot=explicit,
        explicit_resource_path=None,
        environment={},
        platform=platform,
        home=tmp_path,
    ) == explicit

    resource = tmp_path / ".config" / "REAPER"
    resource.mkdir(parents=True)
    (resource / "reaper.ini").write_text("[reaper]\n", encoding="utf-8")
    assert resolve_snapshot_path(
        explicit_snapshot=None,
        explicit_resource_path=resource,
        environment={},
        platform=platform,
        home=tmp_path,
    ) == resource / "Scripts" / "N0TEBridge" / "n0te_snapshot.json"

    with pytest.raises(ReaperSessionLauncherError, match="absolute"):
        resolve_snapshot_path(
            explicit_snapshot="relative.json",
            explicit_resource_path=None,
            environment={},
            platform=platform,
            home=tmp_path,
        )


def test_run_session_owns_runtime_coordinator_and_one_shot_cycle(tmp_path):
    _FakeRuntime.instances.clear()
    _FakeCoordinator.instances.clear()
    service = _FakeService()
    calls = []
    output = []

    def service_factory(runtime, **kwargs):
        calls.append((runtime, kwargs))
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
    assert calls[0][1]["snapshot_path"] == _config(tmp_path).snapshot_path
    assert any("REAPER Launcher Song" in line for line in output)
    assert any("REAPER Reference" in line for line in output)


def test_run_session_releases_runtime_when_active_song_is_missing(tmp_path):
    class NoSongRuntime(_FakeRuntime):
        instances = []

        def __init__(self, *, data_root, state_root):
            super().__init__(data_root=data_root, state_root=state_root)
            self.headquarters = SimpleNamespace(store=_FakeStore(None))
            self.__class__.instances.append(self)

    with pytest.raises(ReaperSessionLauncherError, match="no active Song"):
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

    with pytest.raises(ReaperSessionLauncherError, match="reference discovery failed"):
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

    with pytest.raises(ReaperSessionLauncherError, match="another verified-live"):
        run_session(
            _config(tmp_path),
            process_probe=_FakeProbe(),
            runtime_factory=HeldRuntime,
            coordinator_factory=_FakeCoordinator,
            coordinator_environment={},
        )
    assert HeldRuntime.instances[-1].quit_calls == 0
