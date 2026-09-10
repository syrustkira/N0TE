from __future__ import annotations

import asyncio
import json
from pathlib import Path

from n0te.fl_studio_continuous_observer import FLStudioContinuousObserver
from n0te.fl_studio_host_bridge import FLStudioSnapshotFileClient
from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.memory import HeadquartersMemory


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
        primary = {"title": f"Reference {len(self.calls)}", "source_locator": "catalog:test"}
        return {
            "provider_id": provider_id,
            "read_only": True,
            "action_authority_granted": False,
            "session_calibration": {
                "workspace_id": binding.workspace_id,
                "song_id": binding.song_id,
                "workspace_observation_id": binding.workspace_observation_id,
                "features": {"TEMPO_BPM": next(
                    fact.value
                    for fact in shadow.facts
                    if fact.object_kind == "TEMPO" and fact.field == "bpm"
                )},
                "evidence": [],
            },
            "ranked": [primary],
            "primary": primary,
        }


def _payload(
    *,
    session: str = "session-a",
    tempo: float = 128.0,
    playing: bool = True,
    position: float = 0.1,
    loop_mode: int = 1,
    mixer_index: int = 1,
    mixer_name: str = "Drums",
    window_id: int = 7,
    window_caption: str = "Serum",
    plugin_name: str = "Serum",
    project_title: str = "Project A",
):
    return {
        "schema": "n0te.fl-studio-observation/v1",
        "adapter": {"id": "N0TEBridge", "version": "1"},
        "bridge_session_id": session,
        "runtime": {
            "host_family": "FL_STUDIO",
            "version": "2026.1.4.1234",
            "edition": "Producer Edition",
            "os_name": "Windows",
            "machine": "AMD64",
        },
        "observed_at_epoch_seconds": 100,
        "project": {"title": project_title, "changed_flag": 0},
        "tempo_bpm": tempo,
        "transport": {
            "is_playing": playing,
            "song_position": position,
            "loop_mode": loop_mode,
        },
        "selected_mixer_track": {"index": mixer_index, "name": mixer_name},
        "selected_channel": {"index": 2, "name": "Serum"},
        "active_window": {
            "form_id": window_id,
            "caption": window_caption,
            "plugin_name": plugin_name,
        },
    }


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _observer(tmp_path: Path, reference_client: _ReferenceClient, clock: _Clock):
    snapshot_path = tmp_path / "snapshot.json"
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "FL Observer Artist")
    headquarters.store.create_song("Observer Song")
    workflow = HostSessionReferenceWorkflow(
        headquarters.host_observation,
        reference_client,
    )
    client = FLStudioSnapshotFileClient(
        snapshot_path,
        now_epoch_seconds=lambda: 100.0,
    )
    observer = FLStudioContinuousObserver(
        client,
        workflow,
        provider_id="session-local",
        discovery_retry_seconds=30.0,
        monotonic_clock=clock,
    )
    return headquarters, snapshot_path, observer


def test_observer_separates_hot_polling_host_commits_and_reference_searches(tmp_path: Path):
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
        assert len(references.calls) == 1

        _write(path, _payload(position=0.55))
        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is False
        assert second.discovery_performed is False
        assert second.discovery_reason is None
        assert second.observation.binding.workspace_id == workspace_id
        assert len(references.calls) == 1

        _write(
            path,
            _payload(
                position=0.75,
                playing=False,
                loop_mode=0,
                mixer_index=3,
                mixer_name="Bass",
                window_id=8,
                window_caption="Piano roll",
                plugin_name="",
            ),
        )
        third = asyncio.run(observer.poll_once())
        assert third.observation_committed is True
        assert third.discovery_performed is False
        assert len(references.calls) == 1

        _write(path, _payload(tempo=130.2, playing=False))
        fourth = asyncio.run(observer.poll_once())
        assert fourth.observation_committed is True
        assert fourth.discovery_performed is True
        assert fourth.discovery_reason == "MUSICAL_CHANGE"
        assert len(references.calls) == 2
    finally:
        headquarters.close()


def test_provider_failure_backs_off_without_stopping_host_observation(tmp_path: Path):
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

        _write(path, _payload(playing=False))
        clock.value = 20.0
        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is True
        assert second.discovery_deferred is True
        assert second.discovery_reason == "RETRY"
        assert len(references.calls) == 1

        third = asyncio.run(observer.poll_once(force_discovery=True))
        assert third.discovery_performed is True
        assert third.discovery_reason == "EXPLICIT"
        assert third.references["primary"]["title"] == "Reference 2"
        assert len(references.calls) == 2
    finally:
        headquarters.close()


def test_new_fl_project_session_creates_new_workspace_and_researches(tmp_path: Path):
    clock = _Clock(0.0)
    references = _ReferenceClient()
    headquarters, path, observer = _observer(tmp_path, references, clock)
    try:
        _write(path, _payload(session="session-a", project_title="Project A"))
        first = asyncio.run(observer.poll_once())

        _write(path, _payload(session="session-b", project_title="Project B"))
        second = asyncio.run(observer.poll_once())

        assert second.observation_committed is True
        assert second.discovery_performed is True
        assert second.discovery_reason == "MUSICAL_CHANGE"
        assert second.observation.binding.workspace_id != first.observation.binding.workspace_id
        assert len(references.calls) == 2
    finally:
        headquarters.close()
