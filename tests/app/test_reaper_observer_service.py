from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.app_runtime import ApplicationRuntime
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment
from n0te.reaper_host_bridge import ReaperHostBridgeError
from n0te.reaper_observer_service import (
    STATUS_SCHEMA,
    ReaperObserverService,
    ReaperObserverServiceError,
)


class _Probe:
    def status(self, process):
        return "UNKNOWN"


def _process(pid=1501):
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=f"reaper-observer-service:{pid}",
    )


def _profile(data_root: Path) -> str:
    headquarters = HeadquartersMemory.create(data_root, "REAPER Observer Service Artist")
    try:
        return headquarters.store.profile_id
    finally:
        headquarters.close()


def test_from_runtime_reuses_owned_headquarters_and_requires_active_song(tmp_path):
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
    with pytest.raises(ReaperObserverServiceError, match="RUNNING"):
        ReaperObserverService.from_runtime(
            runtime,
            provider_id="session-local",
            snapshot_path=tmp_path / "snapshot.json",
        )

    assert runtime.launch(
        profile_id=profile_id,
        process=_process(),
        probe=_Probe(),
    ).status == "STARTED"
    try:
        with pytest.raises(ReaperObserverServiceError, match="active Song"):
            ReaperObserverService.from_runtime(
                runtime,
                provider_id="session-local",
                snapshot_path=tmp_path / "snapshot.json",
            )
        runtime.headquarters.store.create_song("Observed REAPER Song")
        service = ReaperObserverService.from_runtime(
            runtime,
            provider_id="session-local",
            snapshot_path=tmp_path / "snapshot.json",
            coordinator_endpoint="http://127.0.0.1:8000/mcp",
        )
        assert service.headquarters is runtime.headquarters
        assert service.state == "READY"
        assert opens == [profile_id]
    finally:
        runtime.quit()


class _FlakyObserver:
    interval_seconds = 0.05

    def __init__(self, cycle):
        self.calls = 0
        self.cycle = cycle
        self.force_values = []

    async def poll_once(self, *, force_discovery=False):
        self.calls += 1
        self.force_values.append(force_discovery)
        if self.calls == 1:
            raise ReaperHostBridgeError("snapshot unavailable")
        return self.cycle


def _cycle():
    snapshot = SimpleNamespace(
        runtime=SimpleNamespace(family="REAPER", version="7.79/x64"),
        project_name="TellMeN0TE Project.rpp",
        project_saved=True,
        project_state_change_count=10,
        tempo_bpm=129.0,
        play_state=1,
        is_playing=True,
        is_paused=False,
        is_recording=False,
        play_position_seconds=22.5,
        repeat_enabled=True,
        track_count=7,
        selected_tracks=(
            SimpleNamespace(index=2, guid="{TRACK-2}", name="Bass"),
        ),
        selection_truncated=False,
    )
    observation = SimpleNamespace(
        binding=SimpleNamespace(song_id="song_reaper", workspace_id="wsp_reaper")
    )
    return SimpleNamespace(
        snapshot=snapshot,
        observation=observation,
        observation_committed=True,
        discovery_performed=True,
        discovery_deferred=False,
        discovery_reason="INITIAL",
        discovery_error_class=None,
        references={
            "provider_id": "provider",
            "primary": {"title": "REAPER Reference"},
            "ranked": [{"title": "REAPER Reference"}],
        },
    )


def test_service_recovers_from_missing_snapshot_without_busy_loop(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "REAPER Recovery Artist")
    observer = _FlakyObserver(_cycle())
    service = ReaperObserverService(
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
        assert service.last_bridge_error_class == "ReaperHostBridgeError"
        assert service.latest_cycle is observer.cycle
        assert service.state == "STOPPED"
    finally:
        headquarters.close()


def test_status_projection_and_explicit_refresh_are_read_only(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "REAPER Status Artist")
    observer = _FlakyObserver(_cycle())
    observer.calls = 1
    service = ReaperObserverService(headquarters, observer)
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
        assert status["session"]["host_family"] == "REAPER"
        assert status["session"]["project_identity_scope"] == "REASCRIPT_SESSION_ONLY"
        assert status["session"]["project_name"] == "TellMeN0TE Project.rpp"
        assert status["session"]["play_position_seconds"] == 22.5
        assert status["session"]["selected_tracks"][0]["name"] == "Bass"
        assert status["reference_discovery"]["primary"]["title"] == "REAPER Reference"
    finally:
        headquarters.close()
