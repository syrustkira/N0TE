from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.ableton_session_launcher import (
    AbletonSessionConfig,
    AbletonSessionLauncherError,
    CoordinatorProcessSupervisor,
    SystemProcessProbe,
    resolve_profile_id,
    resolve_provider_id,
    run_session,
)
from n0te.instance import ProcessIdentity
from n0te.platforms import PlatformEnvironment


def _profile(root: Path, suffix: str) -> str:
    profile_id = "prf_" + suffix * 32
    path = root / "profiles" / profile_id
    path.mkdir(parents=True)
    (path / "lineage.sqlite3").write_bytes(b"placeholder")
    return profile_id


def test_resolve_profile_id_auto_selects_only_existing_profile(tmp_path: Path) -> None:
    profile_id = _profile(tmp_path, "a")
    assert resolve_profile_id(tmp_path, explicit=None, environment={}) == profile_id

    second = _profile(tmp_path, "b")
    with pytest.raises(AbletonSessionLauncherError, match="multiple N0TE profiles"):
        resolve_profile_id(tmp_path, explicit=None, environment={})

    assert (
        resolve_profile_id(
            tmp_path,
            explicit=None,
            environment={"N0TE_PROFILE_ID": second},
        )
        == second
    )


def test_resolve_profile_id_fails_closed_for_missing_profile(tmp_path: Path) -> None:
    missing = "prf_" + "c" * 32
    with pytest.raises(AbletonSessionLauncherError, match="does not exist"):
        resolve_profile_id(tmp_path, explicit=missing, environment={})


def test_provider_resolution_infers_openai_backend_without_overriding_explicit_choice() -> None:
    provider, env = resolve_provider_id(
        explicit=None,
        environment={"OPENAI_API_KEY": "test-key"},
    )
    assert provider == "openai-web-reference-search"
    assert env["N0TE_REFERENCE_SEARCH_BACKEND"] == "openai_web"

    provider, env = resolve_provider_id(
        explicit="session-local",
        environment={},
    )
    assert provider == "session-local"
    assert "N0TE_REFERENCE_SEARCH_BACKEND" not in env


def test_system_process_probe_detects_pid_reuse_from_start_token(monkeypatch) -> None:
    platform = PlatformEnvironment.from_runtime_labels("Linux", "x86_64")
    probe = SystemProcessProbe(platform)
    process = ProcessIdentity.from_start_token(
        platform,
        pid=4242,
        start_token="LINUX:111",
        launch_marker="old-launch",
    )

    monkeypatch.setattr(probe, "_raw_start", lambda pid: ("ALIVE", "111"))
    assert probe.status(process) == "ALIVE"

    monkeypatch.setattr(probe, "_raw_start", lambda pid: ("ALIVE", "222"))
    assert probe.status(process) == "DEAD"

    monkeypatch.setattr(probe, "_raw_start", lambda pid: ("UNKNOWN", None))
    assert probe.status(process) == "UNKNOWN"


def test_coordinator_reuses_existing_listener_without_spawning(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("existing coordinator must not spawn a child")

    supervisor = CoordinatorProcessSupervisor(
        "http://127.0.0.1:8000/mcp",
        environment={},
        popen_factory=forbidden,
    )
    monkeypatch.setattr(supervisor, "_listening", lambda: True)
    assert supervisor.start() == "REUSED"
    assert supervisor.owned is False


def test_coordinator_refuses_self_start_without_provider_configuration(monkeypatch) -> None:
    supervisor = CoordinatorProcessSupervisor(
        "http://127.0.0.1:8000/mcp",
        environment={},
    )
    monkeypatch.setattr(supervisor, "_listening", lambda: False)
    with pytest.raises(AbletonSessionLauncherError, match="reference provider"):
        supervisor.start()


class _FakeProbe:
    def __init__(self) -> None:
        self.process = object()

    def current_process(self):
        return self.process

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
        self.state = "STOPPED"
        self.headquarters = SimpleNamespace(
            store=_FakeStore(SimpleNamespace(title="Launcher Song"))
        )
        self.launched = None
        self.quit_calls = 0
        self.__class__.instances.append(self)

    def launch(self, *, profile_id, process, probe):
        self.state = "RUNNING"
        self.launched = (profile_id, process, probe)
        return SimpleNamespace(status="STARTED", reason=None)

    def quit(self):
        self.quit_calls += 1
        self.state = "STOPPED"
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
            references={"primary": {"title": "Reference Song"}}
        )


def test_run_session_owns_lifecycle_and_once_smoke_test(tmp_path: Path) -> None:
    _FakeRuntime.instances.clear()
    _FakeCoordinator.instances.clear()
    service = _FakeService()
    service_calls = []
    output = []

    def service_factory(runtime, **kwargs):
        service_calls.append((runtime, kwargs))
        return service

    config = AbletonSessionConfig(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        profile_id="prf_" + "d" * 32,
        provider_id="provider",
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
    assert any("Launcher Song" in line for line in output)
    assert any("Reference Song" in line for line in output)


def test_run_session_releases_runtime_when_active_song_is_missing(tmp_path: Path) -> None:
    class NoSongRuntime(_FakeRuntime):
        instances = []

        def __init__(self, *, data_root, state_root):
            super().__init__(data_root=data_root, state_root=state_root)
            self.headquarters = SimpleNamespace(store=_FakeStore(None))
            self.__class__.instances.append(self)

    config = AbletonSessionConfig(
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        profile_id="prf_" + "e" * 32,
        provider_id="provider",
        once=True,
    )
    with pytest.raises(AbletonSessionLauncherError, match="no active Song"):
        run_session(
            config,
            process_probe=_FakeProbe(),
            runtime_factory=NoSongRuntime,
            coordinator_factory=_FakeCoordinator,
            coordinator_environment={"N0TE_REFERENCE_SEARCH_BACKEND": "openai_web"},
        )
    assert NoSongRuntime.instances[-1].quit_calls == 1