from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.ableton_host_bridge import AbletonHostBridgeError
from n0te.ableton_observer_service import (
    STATUS_SCHEMA,
    AbletonAdviceClient,
    AbletonObserverService,
    AbletonObserverServiceError,
)
from n0te.app_runtime import ApplicationRuntime
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment


class _Probe:
    def status(self, process):
        return "UNKNOWN"


def _process(pid=901):
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=f"observer-service:{pid}",
    )


def _profile(data_root: Path) -> str:
    headquarters = HeadquartersMemory.create(data_root, "Observer Service Artist")
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
    runtime.headquarters.store.create_song("Observed Song")

    service = AbletonObserverService.from_runtime(
        runtime,
        provider_id="session-local",
        bridge_endpoint="http://127.0.0.1:9799",
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

    with pytest.raises(AbletonObserverServiceError, match="RUNNING"):
        AbletonObserverService.from_runtime(runtime, provider_id="session-local")

    assert runtime.launch(
        profile_id=profile_id,
        process=_process(902),
        probe=_Probe(),
    ).status == "STARTED"
    try:
        assert runtime.headquarters.store.active_song() is None
        with pytest.raises(AbletonObserverServiceError, match="active Song"):
            AbletonObserverService.from_runtime(runtime, provider_id="session-local")
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
            raise AbletonHostBridgeError("bridge unavailable")
        return self.sentinel


def test_service_recovers_from_missing_live_bridge_without_busy_loop(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Bridge Recovery Artist")
    observer = _FlakyObserver()
    service = AbletonObserverService(
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
        assert service.last_bridge_error_class == "AbletonHostBridgeError"
        assert service.latest_cycle is observer.sentinel
        assert service.state == "STOPPED"
    finally:
        headquarters.close()


def _projection_cycle(*, discovery_performed=True):
    track = SimpleNamespace(kind="TRACK", index=2, name="Hook", ref="track:2")
    runtime = SimpleNamespace(
        family="ABLETON_LIVE",
        version="12.4.5",
        display_name="Ableton Live 12.4.5",
    )
    snapshot = SimpleNamespace(
        runtime=runtime,
        tempo_bpm=129.0,
        is_playing=True,
        current_song_time=48.25,
        selected_track=track,
    )
    binding = SimpleNamespace(song_id="song_alpha", workspace_id="wsp_alpha")
    observation = SimpleNamespace(binding=binding)
    references = {
        "provider_id": "openai-web",
        "primary": {
            "title": "Reference One",
            "source_locator": "https://example.test/reference-one",
        },
        "ranked": [
            {
                "title": "Reference One",
                "source_locator": "https://example.test/reference-one",
            },
            {
                "title": "Reference Two",
                "source_locator": "https://example.test/reference-two",
            },
        ],
    }
    return SimpleNamespace(
        snapshot=snapshot,
        observation=observation,
        observation_committed=True,
        discovery_performed=discovery_performed,
        discovery_deferred=False,
        discovery_reason="EXPLICIT" if discovery_performed else None,
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


class _RecordingAdviceClient(AbletonAdviceClient):
    def __init__(self, *, fail=False):
        self.messages = []
        self.fail = fail

    def show_notice(self, message):
        self.messages.append(message)
        if self.fail:
            raise AbletonHostBridgeError("display unavailable")


def test_status_projection_exposes_music_state_without_internal_or_mutation_authority(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Status Projection Artist")
    cycle = _projection_cycle()
    observer = _ProjectionObserver(cycle)
    advice = _RecordingAdviceClient()
    service = AbletonObserverService(headquarters, observer, advice_client=advice)
    try:
        empty = service.status_projection()
        assert empty == {
            "schema": STATUS_SCHEMA,
            "service_state": "READY",
            "connected": False,
            "bridge_failure_count": 0,
            "last_bridge_error_class": None,
            "advice_display": {"failure_count": 0, "last_error_class": None},
            "read_only": True,
            "action_authority_granted": False,
            "session": None,
            "reference_discovery": None,
        }

        refreshed = asyncio.run(service.refresh_references())
        assert refreshed is cycle
        assert observer.force_values == [True]
        assert advice.messages == ["Reference: Reference One"]
        status = service.status_projection()
        assert status["schema"] == STATUS_SCHEMA
        assert status["service_state"] == "OBSERVING"
        assert status["connected"] is True
        assert status["read_only"] is True
        assert status["action_authority_granted"] is False
        assert status["session"] == {
            "song_id": "song_alpha",
            "workspace_id": "wsp_alpha",
            "host_family": "ABLETON_LIVE",
            "host_version": "12.4.5",
            "host_display_name": "Ableton Live 12.4.5",
            "tempo_bpm": 129.0,
            "is_playing": True,
            "current_song_time": 48.25,
            "selected_track": {
                "kind": "TRACK",
                "index": 2,
                "name": "Hook",
                "ref": "track:2",
            },
            "observation_committed": True,
        }
        assert status["reference_discovery"]["provider_id"] == "openai-web"
        assert status["reference_discovery"]["reason"] == "EXPLICIT"
        assert status["reference_discovery"]["primary"]["title"] == "Reference One"
        assert len(status["reference_discovery"]["ranked"]) == 2
        encoded = repr(status)
        assert "set_path_fingerprint" not in encoded
        assert "workspace_observation_id" not in encoded
        assert "bridge_session" not in encoded
        assert "permit" not in encoded.lower()
    finally:
        headquarters.close()


def test_advice_display_failure_never_erases_reference_refresh(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Advice Failure Artist")
    cycle = _projection_cycle()
    observer = _ProjectionObserver(cycle)
    advice = _RecordingAdviceClient(fail=True)
    service = AbletonObserverService(headquarters, observer, advice_client=advice)
    try:
        refreshed = asyncio.run(service.refresh_references())
        assert refreshed is cycle
        assert service.latest_cycle is cycle
        assert service.state == "OBSERVING"
        assert service.notice_failure_count == 1
        assert service.last_notice_error_class == "AbletonHostBridgeError"
        assert service.status_projection()["reference_discovery"]["primary"]["title"] == "Reference One"
    finally:
        headquarters.close()


def test_explicit_refresh_refuses_after_runtime_ownership_is_lost(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Refresh Ownership Artist")
    observer = _ProjectionObserver(_projection_cycle())
    owned = {"value": False}
    service = AbletonObserverService(
        headquarters,
        observer,
        runtime_guard=lambda: owned["value"],
    )
    try:
        with pytest.raises(AbletonObserverServiceError, match="ApplicationRuntime stopped"):
            asyncio.run(service.refresh_references())
        assert observer.force_values == []
    finally:
        headquarters.close()
