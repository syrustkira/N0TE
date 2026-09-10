from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from n0te.ableton_host_bridge import AbletonHostBridgeError
from n0te.ableton_observer_service import (
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
