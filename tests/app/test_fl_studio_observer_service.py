from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.app_runtime import ApplicationRuntime
from n0te.fl_studio_host_bridge import FLStudioHostBridgeError
from n0te.fl_studio_observer_service import (
    STATUS_SCHEMA,
    FLStudioObserverService,
    FLStudioObserverServiceError,
)
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment


class _Probe:
    def status(self, process):
        return "UNKNOWN"


def _process(pid=1201):
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=f"fl-observer-service:{pid}",
    )


def _profile(data_root: Path) -> str:
    headquarters = HeadquartersMemory.create(data_root, "FL Observer Service Artist")
    try:
        return headquarters.store.profile_id
    finally:
        headquarters.close()


def test_from_runtime_reuses_exact_owned_headquarters_and_stops_with_runtime(tmp_path):
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    profile_id = _profile(data_root)
    opens = []

    def opener(root, profile):
        opens.append(profile)
        return HeadquartersMemory.open(root, profile)

    runtime = ApplicationRuntime(
        data_root=data_root,
        state_root=state_root,
        memory_opener=opener,
    )
    assert runtime.launch(
        profile_id=profile_id,
        process=_process(),
        probe=_Probe(),
    ).status == "STARTED"
    runtime.headquarters.store.create_song("Observed FL Song")

    service = FLStudioObserverService.from_runtime(
        runtime,
        provider_id="session-local",
        snapshot_path=tmp_path / "n0te_snapshot.json",
        coordinator_endpoint="http://127.0.0.1:8000/mcp",
    )
    assert service.headquarters is runtime.headquarters
    assert service.state == "READY"
    assert opens == [profile_id]

    assert runtime.quit().status == "STOPPED"
    asyncio.run(service.run(asyncio.Event()))
    assert service.state == "STOPPED"
    assert opens == [profile_id]


def test_from_runtime_requires_running_runtime_and_explicit_active_song(tmp_path):
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    profile_id = _profile(data_root)
    runtime = ApplicationRuntime(data_root=data_root, state_root=state_root)

    with pytest.raises(FLStudioObserverServiceError, match="RUNNING"):
        FLStudioObserverService.from_runtime(
            runtime,
            provider_id="session-local",
            snapshot_path=tmp_path / "snapshot.json",
        )

    assert runtime.launch(
        profile_id=profile_id,
        process=_process(1202),
        probe=_Probe(),
    ).status == "STARTED"
    try:
        with pytest.raises(FLStudioObserverServiceError, match="active Song"):
            FLStudioObserverService.from_runtime(
                runtime,
                provider_id="session-local",
                snapshot_path=tmp_path / "snapshot.json",
            )
    finally:
        runtime.quit()


class _FlakyObserver:
    interval_seconds = 0.05

    def __init__(self):
        self.calls = 0
        self.sentinel = object()

    async def poll_once(self, *, force_discovery=False):
        self.calls += 1
        if self.calls == 1:
            raise FLStudioHostBridgeError("snapshot unavailable")
        return self.sentinel


def test_service_recovers_from_missing_snapshot_without_busy_loop(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "FL Recovery Artist")
    observer = _FlakyObserver()
    service = FLStudioObserverService(
        headquarters,
        observer,
        reconnect_interval_seconds=0.05,
    )

    async def scenario():
        stop = asyncio.Event()

        def on_cycle(cycle):
            stop.set()

        await service.run(stop, on_cycle=on_cycle)

    try:
        asyncio.run(scenario())
        assert observer.calls == 2
        assert service.bridge_failure_count == 1
        assert service.last_bridge_error_class == "FLStudioHostBridgeError"
        assert service.latest_cycle is observer.sentinel
        assert service.state == "STOPPED"
    finally:
        headquarters.close()


def _projection_cycle():
    runtime = SimpleNamespace(
        family="FL_STUDIO",
        version="2026.1.4.1234",
        display_name="FL Studio 2026.1.4.1234",
    )
    snapshot = SimpleNamespace(
        runtime=runtime,
        project_title="TellMeN0TE Project",
        tempo_bpm=132.5,
        is_playing=True,
        song_position=0.25,
        loop_mode=1,
        selected_mixer_track=SimpleNamespace(index=4, name="Drum Bus"),
        selected_channel=SimpleNamespace(index=2, name="Serum"),
        active_window=SimpleNamespace(form_id=7, caption="Serum", plugin_name="Serum"),
    )
    observation = SimpleNamespace(
        binding=SimpleNamespace(song_id="song_fl", workspace_id="wsp_fl")
    )
    references = {
        "provider_id": "openai-web",
        "primary": {"title": "Reference One", "source_locator": "https://example.test/ref"},
        "ranked": [{"title": "Reference One", "source_locator": "https://example.test/ref"}],
    }
    return SimpleNamespace(
        snapshot=snapshot,
        observation=observation,
        observation_committed=True,
        discovery_performed=True,
        discovery_deferred=False,
        discovery_reason="EXPLICIT",
        discovery_error_class=None,
        references=references,
    )


class _ProjectionObserver:
    interval_seconds = 0.05

    def __init__(self, cycle):
        self.cycle = cycle
        self.force_values = []

    async def poll_once(self, *, force_discovery=False):
        self.force_values.append(force_discovery)
        return self.cycle


def test_status_projection_and_explicit_refresh_are_read_only_consumer_surfaces(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "FL Status Artist")
    observer = _ProjectionObserver(_projection_cycle())
    service = FLStudioObserverService(headquarters, observer)
    try:
        empty = service.status_projection()
        assert empty["schema"] == STATUS_SCHEMA
        assert empty["read_only"] is True
        assert empty["action_authority_granted"] is False
        assert empty["session"] is None

        cycle = asyncio.run(service.refresh_references())
        assert cycle is observer.cycle
        assert observer.force_values == [True]
        status = service.status_projection()
        assert status["service_state"] == "OBSERVING"
        assert status["connected"] is True
        assert status["session"] == {
            "song_id": "song_fl",
            "workspace_id": "wsp_fl",
            "host_family": "FL_STUDIO",
            "host_version": "2026.1.4.1234",
            "host_display_name": "FL Studio 2026.1.4.1234",
            "project_title": "TellMeN0TE Project",
            "tempo_bpm": 132.5,
            "is_playing": True,
            "song_position": 0.25,
            "loop_mode": 1,
            "selected_mixer_track": {"index": 4, "name": "Drum Bus"},
            "selected_channel": {"index": 2, "name": "Serum"},
            "active_window": {"form_id": 7, "caption": "Serum", "plugin_name": "Serum"},
            "observation_committed": True,
        }
        assert status["reference_discovery"]["primary"]["title"] == "Reference One"
        encoded = repr(status).lower()
        assert "snapshot_path" not in encoded
        assert "bridge_session_id" not in encoded
        assert "permit" not in encoded
    finally:
        headquarters.close()


def test_explicit_refresh_refuses_after_runtime_ownership_is_lost(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "FL Ownership Artist")
    observer = _ProjectionObserver(_projection_cycle())
    service = FLStudioObserverService(
        headquarters,
        observer,
        runtime_guard=lambda: False,
    )
    try:
        with pytest.raises(FLStudioObserverServiceError, match="ApplicationRuntime stopped"):
            asyncio.run(service.refresh_references())
        assert observer.force_values == []
    finally:
        headquarters.close()
