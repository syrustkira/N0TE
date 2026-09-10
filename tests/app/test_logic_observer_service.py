from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from n0te.app_runtime import ApplicationRuntime
from n0te.instance import ProcessIdentity
from n0te.logic_midi_bridge import LogicMidiBridgeError
from n0te.logic_observer_service import (
    STATUS_SCHEMA,
    LogicObserverService,
    LogicObserverServiceError,
)
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment


class _Probe:
    def status(self, process):
        return "UNKNOWN"


def _process(pid=1401):
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=f"logic-observer-service:{pid}",
    )


def _profile(data_root: Path) -> str:
    headquarters = HeadquartersMemory.create(data_root, "Logic Observer Artist")
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
    with pytest.raises(LogicObserverServiceError, match="RUNNING"):
        LogicObserverService.from_runtime(runtime, provider_id="session-local")

    assert runtime.launch(
        profile_id=profile_id,
        process=_process(),
        probe=_Probe(),
    ).status == "STARTED"
    try:
        with pytest.raises(LogicObserverServiceError, match="active Song"):
            LogicObserverService.from_runtime(runtime, provider_id="session-local")
        runtime.headquarters.store.create_song("Observed Logic Song")
        service = LogicObserverService.from_runtime(
            runtime,
            provider_id="session-local",
            coordinator_endpoint="http://127.0.0.1:8000/mcp",
        )
        assert service.headquarters is runtime.headquarters
        assert service.state == "READY"
        assert opens == [profile_id]
    finally:
        runtime.quit()


class _FlakyMidi:
    def __init__(self):
        self.opened_port_name = None
        self.open_calls = 0
        self.close_calls = 0

    def open(self):
        self.open_calls += 1
        if self.open_calls == 1:
            raise LogicMidiBridgeError("Logic Pro Virtual Out unavailable")
        self.opened_port_name = "Logic Pro Virtual Out"
        return self.opened_port_name

    def close(self):
        self.close_calls += 1
        self.opened_port_name = None


class _Observer:
    interval_seconds = 0.05

    def __init__(self, cycle):
        self.cycle = cycle
        self.calls = 0
        self.force_values = []

    async def poll_once(self, *, force_discovery=False):
        self.calls += 1
        self.force_values.append(force_discovery)
        return self.cycle


def _cycle():
    snapshot = SimpleNamespace(
        runtime=SimpleNamespace(family="LOGIC_PRO", version="UNOBSERVED"),
        tempo_bpm=126.0,
        is_playing=True,
        is_cycling=False,
        meter_numerator=4,
        meter_denominator=4,
    )
    observation = SimpleNamespace(
        binding=SimpleNamespace(song_id="song_logic", workspace_id="wsp_logic")
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
            "primary": {"title": "Reference Logic"},
            "ranked": [{"title": "Reference Logic"}],
        },
    )


def test_service_recovers_from_missing_logic_port_without_busy_loop(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Logic Recovery Artist")
    midi = _FlakyMidi()
    observer = _Observer(_cycle())
    service = LogicObserverService(
        headquarters,
        observer,
        midi,
        reconnect_interval_seconds=0.05,
    )

    async def scenario():
        stop = asyncio.Event()
        def on_cycle(cycle):
            stop.set()
        await service.run(stop, on_cycle=on_cycle)

    try:
        asyncio.run(scenario())
        assert midi.open_calls == 2
        assert observer.calls == 1
        assert service.bridge_failure_count == 1
        assert service.last_bridge_error_class == "LogicMidiBridgeError"
        assert service.latest_cycle is observer.cycle
        assert service.state == "STOPPED"
    finally:
        headquarters.close()


def test_status_projection_and_explicit_refresh_are_read_only(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Logic Status Artist")
    midi = _FlakyMidi()
    midi.open_calls = 1
    observer = _Observer(_cycle())
    service = LogicObserverService(headquarters, observer, midi)
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
        assert status["session"]["host_family"] == "LOGIC_PRO"
        assert status["session"]["project_identity_scope"] == "SCRIPTER_MONITOR_SESSION_ONLY"
        assert status["session"]["project_name"] == "UNOBSERVED"
        assert status["session"]["project_path"] == "UNOBSERVED"
        assert status["session"]["focus"] == "UNOBSERVED"
        assert status["reference_discovery"]["primary"]["title"] == "Reference Logic"
    finally:
        headquarters.close()
