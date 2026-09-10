from __future__ import annotations

import asyncio
import json
from pathlib import Path

from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.memory import HeadquartersMemory
from n0te.reaper_continuous_observer import ReaperContinuousObserver
from n0te.reaper_host_bridge import ReaperSnapshotFileClient


class _Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class _ReferenceClient:
    def __init__(self, *, fail_first: bool = False):
        self.calls = []
        self.fail_first = fail_first

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("provider down")
        tempo = next(
            fact.value
            for fact in shadow.facts
            if fact.object_kind == "TEMPO" and fact.field == "bpm"
        )
        primary = {
            "title": f"REAPER Reference {len(self.calls)}",
            "source_locator": "catalog:reaper-test",
        }
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


def _payload(
    *,
    session="reaper-session-a",
    project_name="Project A.rpp",
    saved=True,
    state_change_count=1,
    tempo=128.0,
    play_state=1,
    position=0.0,
    repeat=False,
    selected=None,
    track_count=8,
):
    return {
        "schema": "n0te.reaper-observation/v1",
        "adapter": {"id": "n0te-reaper-reascript", "version": "1"},
        "bridge_session_id": session,
        "observed_at_epoch_seconds": 100,
        "runtime": {
            "host_family": "REAPER",
            "version": "7.79/x64",
            "edition": "REAPER",
            "os_name": "Linux",
            "machine": "x86_64",
        },
        "project": {
            "name": project_name,
            "saved": saved,
            "state_change_count": state_change_count,
        },
        "tempo_bpm": tempo,
        "transport": {
            "play_state": play_state,
            "play_position_seconds": position,
            "repeat_enabled": repeat,
        },
        "track_count": track_count,
        "selected_tracks": selected
        if selected is not None
        else [{"index": 1, "guid": "{TRACK-A}", "name": "Drums"}],
        "selection_truncated": False,
    }


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _observer(tmp_path: Path, reference_client: _ReferenceClient, clock: _Clock):
    snapshot_path = tmp_path / "snapshot.json"
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "REAPER Observer Artist")
    headquarters.store.create_song("Observer Song")
    workflow = HostSessionReferenceWorkflow(
        headquarters.host_observation,
        reference_client,
    )
    client = ReaperSnapshotFileClient(
        snapshot_path,
        now_epoch_seconds=lambda: 100.0,
    )
    observer = ReaperContinuousObserver(
        client,
        workflow,
        provider_id="session-local",
        discovery_retry_seconds=30.0,
        monotonic_clock=clock,
    )
    return headquarters, snapshot_path, observer


def test_hot_playhead_and_project_counter_do_not_append_or_research(tmp_path: Path):
    clock = _Clock(0.0)
    references = _ReferenceClient()
    headquarters, path, observer = _observer(tmp_path, references, clock)
    try:
        _write(path, _payload())
        first = asyncio.run(observer.poll_once())
        assert first.observation_committed is True
        assert first.discovery_performed is True
        assert first.discovery_reason == "INITIAL"
        workspace_id = first.observation.binding.workspace_id
        first_batch = first.observation.recorded_shadow_batch_id

        _write(path, _payload(position=47.5, state_change_count=99, track_count=15))
        second = asyncio.run(observer.poll_once())
        assert second.snapshot.play_position_seconds == 47.5
        assert second.snapshot.project_state_change_count == 99
        assert second.snapshot.track_count == 15
        assert second.observation_committed is False
        assert second.discovery_performed is False
        assert second.discovery_reason is None
        assert second.observation.binding.workspace_id == workspace_id
        assert second.observation.recorded_shadow_batch_id == first_batch
        assert len(references.calls) == 1
    finally:
        headquarters.close()


def test_transport_focus_and_project_name_commit_without_search(tmp_path: Path):
    clock = _Clock(0.0)
    references = _ReferenceClient()
    headquarters, path, observer = _observer(tmp_path, references, clock)
    try:
        _write(path, _payload())
        asyncio.run(observer.poll_once())

        _write(
            path,
            _payload(
                project_name="Renamed Project.rpp",
                play_state=0,
                repeat=True,
                selected=[{"index": 3, "guid": "{TRACK-B}", "name": "Bass"}],
            ),
        )
        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is True
        assert second.discovery_performed is False
        assert len(references.calls) == 1
        focus = second.observation.focus.get("TRACK")
        assert focus is not None
        assert focus.refs == ("reaper-track:{TRACK-B}",)
    finally:
        headquarters.close()


def test_meaningful_tempo_change_researches_and_new_session_gets_new_workspace(tmp_path: Path):
    clock = _Clock(0.0)
    references = _ReferenceClient()
    headquarters, path, observer = _observer(tmp_path, references, clock)
    try:
        _write(path, _payload())
        first = asyncio.run(observer.poll_once())

        _write(path, _payload(tempo=130.2))
        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is True
        assert second.discovery_performed is True
        assert second.discovery_reason == "MUSICAL_CHANGE"
        assert len(references.calls) == 2

        _write(path, _payload(session="reaper-session-b", project_name="Project B.rpp", tempo=130.2))
        third = asyncio.run(observer.poll_once())
        assert third.observation_committed is True
        assert third.discovery_performed is True
        assert third.discovery_reason == "MUSICAL_CHANGE"
        assert third.observation.binding.workspace_id != first.observation.binding.workspace_id
        assert len(references.calls) == 3
    finally:
        headquarters.close()


def test_provider_failure_backs_off_while_host_observation_continues(tmp_path: Path):
    clock = _Clock(10.0)
    references = _ReferenceClient(fail_first=True)
    headquarters, path, observer = _observer(tmp_path, references, clock)
    try:
        _write(path, _payload())
        first = asyncio.run(observer.poll_once())
        assert first.observation_committed is True
        assert first.discovery_performed is False
        assert first.discovery_error_class == "RuntimeError"
        assert first.observation.shadow.status == "CURRENT"
        assert len(references.calls) == 1

        _write(path, _payload(play_state=0))
        clock.value = 20.0
        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is True
        assert second.discovery_deferred is True
        assert second.discovery_reason == "RETRY"
        assert len(references.calls) == 1

        third = asyncio.run(observer.poll_once(force_discovery=True))
        assert third.discovery_performed is True
        assert third.discovery_reason == "EXPLICIT"
        assert third.references["primary"]["title"] == "REAPER Reference 2"
        assert len(references.calls) == 2
    finally:
        headquarters.close()
