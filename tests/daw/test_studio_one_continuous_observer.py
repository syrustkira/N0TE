from __future__ import annotations

import asyncio
from pathlib import Path

from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.memory import HeadquartersMemory
from n0te.studio_one_continuous_observer import StudioOneContinuousObserver
from n0te.studio_one_midi_bridge import (
    StudioOneClockSnapshot,
    unobserved_studio_one_runtime,
)


class _Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class _SnapshotClient:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.index = 0

    def fetch_snapshot(self):
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return value


class _ReferenceClient:
    def __init__(self, *, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("provider down")
        primary = {
            "title": f"Studio One Reference {len(self.calls)}",
            "source_locator": "catalog:studio-one-test",
        }
        tempo = next(
            fact.value
            for fact in shadow.facts
            if fact.object_kind == "TEMPO" and fact.field == "bpm"
        )
        return {
            "provider_id": provider_id,
            "read_only": True,
            "action_authority_granted": False,
            "session_calibration": {
                "workspace_id": binding.workspace_id,
                "song_id": binding.song_id,
                "workspace_observation_id": binding.workspace_observation_id,
                "features": {"TEMPO_BPM": tempo},
                "evidence": [],
            },
            "ranked": [primary],
            "primary": primary,
        }


def _snapshot(*, session="session-one", tempo=128.0, transport="UNKNOWN", observed=100):
    return StudioOneClockSnapshot(
        bridge_session_id=session,
        runtime=unobserved_studio_one_runtime(),
        observed_at_epoch_seconds=observed,
        tempo_bpm=tempo,
        transport_state=transport,
    )


def _observer(tmp_path: Path, snapshots, references, clock):
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "Studio One Observer Artist")
    headquarters.store.create_song("Studio One Observer Song")
    workflow = HostSessionReferenceWorkflow(
        headquarters.host_observation,
        references,
    )
    client = _SnapshotClient(snapshots)
    observer = StudioOneContinuousObserver(
        client,
        workflow,
        provider_id="session-local",
        discovery_retry_seconds=30.0,
        monotonic_clock=clock,
    )
    return headquarters, observer


def test_observer_separates_clock_churn_host_commits_and_reference_searches(tmp_path: Path):
    clock = _Clock(0.0)
    references = _ReferenceClient()
    snapshots = [
        _snapshot(tempo=128.001, transport="UNKNOWN", observed=100),
        _snapshot(tempo=128.004, transport="UNKNOWN", observed=101),
        _snapshot(tempo=128.004, transport="PLAYING", observed=102),
        _snapshot(tempo=129.2, transport="PLAYING", observed=103),
    ]
    headquarters, observer = _observer(tmp_path, snapshots, references, clock)
    try:
        first = asyncio.run(observer.poll_once())
        assert first.observation_committed is True
        assert first.discovery_performed is True
        assert first.discovery_reason == "INITIAL"
        workspace_id = first.observation.binding.workspace_id
        assert len(references.calls) == 1

        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is False
        assert second.discovery_performed is False
        assert second.discovery_reason is None
        assert second.observation.binding.workspace_id == workspace_id
        assert len(references.calls) == 1

        third = asyncio.run(observer.poll_once())
        assert third.observation_committed is True
        assert third.discovery_performed is False
        assert third.observation.binding.workspace_id == workspace_id
        assert len(references.calls) == 1

        fourth = asyncio.run(observer.poll_once())
        assert fourth.observation_committed is True
        assert fourth.discovery_performed is True
        assert fourth.discovery_reason == "MUSICAL_CHANGE"
        assert len(references.calls) == 2
    finally:
        headquarters.close()


def test_provider_failure_backs_off_without_stopping_timing_observation(tmp_path: Path):
    clock = _Clock(10.0)
    references = _ReferenceClient(fail_first=True)
    snapshots = [
        _snapshot(tempo=124.0, transport="PLAYING", observed=100),
        _snapshot(tempo=124.0, transport="STOPPED", observed=101),
        _snapshot(tempo=124.0, transport="STOPPED", observed=102),
    ]
    headquarters, observer = _observer(tmp_path, snapshots, references, clock)
    try:
        first = asyncio.run(observer.poll_once())
        assert first.observation_committed is True
        assert first.discovery_performed is False
        assert first.discovery_error_class == "RuntimeError"
        assert first.observation.shadow.status == "CURRENT"
        assert len(references.calls) == 1

        clock.value = 20.0
        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is True
        assert second.discovery_deferred is True
        assert second.discovery_reason == "RETRY"
        assert len(references.calls) == 1

        third = asyncio.run(observer.poll_once(force_discovery=True))
        assert third.discovery_performed is True
        assert third.discovery_reason == "EXPLICIT"
        assert third.references["primary"]["title"] == "Studio One Reference 2"
        assert len(references.calls) == 2
    finally:
        headquarters.close()


def test_new_monitor_session_creates_new_workspace_and_researches(tmp_path: Path):
    clock = _Clock(0.0)
    references = _ReferenceClient()
    snapshots = [
        _snapshot(session="session-a", tempo=120.0, observed=100),
        _snapshot(session="session-b", tempo=120.0, observed=101),
    ]
    headquarters, observer = _observer(tmp_path, snapshots, references, clock)
    try:
        first = asyncio.run(observer.poll_once())
        second = asyncio.run(observer.poll_once())

        assert second.observation_committed is True
        assert second.discovery_performed is True
        assert second.discovery_reason == "MUSICAL_CHANGE"
        assert second.observation.binding.workspace_id != first.observation.binding.workspace_id
        assert len(references.calls) == 2
    finally:
        headquarters.close()
