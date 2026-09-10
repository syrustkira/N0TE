from __future__ import annotations

import asyncio

from integrations.ableton.N0TEBridge import N0TEBridge
from n0te.ableton_continuous_observer import AbletonContinuousObserver
from n0te.ableton_host_bridge import AbletonRemoteScriptClient
from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.memory import HeadquartersMemory


class _Track:
    def __init__(self, name):
        self.name = name


class _View:
    def __init__(self, selected_track):
        self.selected_track = selected_track


class _Song:
    def __init__(self, *, tempo=128.0, file_path="/tmp/TellMeN0TE/Continuous.als"):
        self.tracks = (_Track("Drums"), _Track("Lead"))
        self.return_tracks = ()
        self.master_track = _Track("Master")
        self.view = _View(self.tracks[1])
        self.tempo = tempo
        self.is_playing = True
        self.current_song_time = 1.0
        self.file_path = file_path

    def get_data(self, key, default=None):
        return default


class _Application:
    @staticmethod
    def get_major_version():
        return 12

    @staticmethod
    def get_minor_version():
        return 4

    @staticmethod
    def get_bugfix_version():
        return 5


class _CInstance:
    def __init__(self, song):
        self._song = song

    def song(self):
        return self._song

    def application(self):
        return _Application()

    def log_message(self, message):
        return None


class _ReferenceClient:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        if self.fail:
            raise RuntimeError("provider unavailable")
        primary = {
            "title": "Continuous Reference",
            "source_locator": "catalog:continuous-reference",
        }
        return {
            "provider_id": provider_id,
            "read_only": True,
            "action_authority_granted": False,
            "session_calibration": {
                "workspace_id": binding.workspace_id,
                "song_id": binding.song_id,
                "workspace_observation_id": binding.workspace_observation_id,
                "features": {"TEMPO_BPM": 128.0},
                "evidence": [],
            },
            "ranked": [primary],
            "primary": primary,
        }


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _build(tmp_path, *, reference_client=None, clock=None):
    headquarters = HeadquartersMemory.create(tmp_path, "Continuous Ableton Artist")
    headquarters.store.create_song("Continuous Song")
    song = _Song()
    bridge = N0TEBridge(_CInstance(song), port=0, start_server=True)
    references = reference_client or _ReferenceClient()
    workflow = HostSessionReferenceWorkflow(headquarters.host_observation, references)
    live_client = AbletonRemoteScriptClient(
        endpoint=f"http://127.0.0.1:{bridge.port}",
    )
    observer = AbletonContinuousObserver(
        live_client,
        workflow,
        provider_id="session-local",
        interval_seconds=0.1,
        discovery_retry_seconds=30.0,
        monotonic_clock=clock or _Clock(),
    )
    return headquarters, song, bridge, references, observer


def test_continuous_observer_keeps_hot_transport_without_shadow_or_search_spam(tmp_path):
    headquarters, song, bridge, references, observer = _build(tmp_path)
    try:
        first = asyncio.run(observer.poll_once())
        assert first.observation_committed is True
        assert first.discovery_performed is True
        assert first.discovery_reason == "INITIAL"
        assert len(references.calls) == 1
        first_shadow_batch = first.observation.recorded_shadow_batch_id
        workspace_id = first.observation.binding.workspace_id

        song.current_song_time = 19.25
        second = asyncio.run(observer.poll_once())
        assert second.snapshot.current_song_time == 19.25
        assert observer.latest_snapshot.current_song_time == 19.25
        assert second.observation_committed is False
        assert second.discovery_performed is False
        assert second.discovery_reason is None
        assert second.observation.recorded_shadow_batch_id == first_shadow_batch
        assert len(references.calls) == 1

        song.view.selected_track = song.tracks[0]
        third = asyncio.run(observer.poll_once())
        assert third.observation_committed is True
        assert third.discovery_performed is False
        assert third.discovery_reason is None
        assert third.observation.binding.workspace_id == workspace_id
        assert third.observation.focus.dimensions[0].refs == ("track:0",)
        assert len(references.calls) == 1

        song.tempo = 130.0
        fourth = asyncio.run(observer.poll_once())
        assert fourth.observation_committed is True
        assert fourth.discovery_performed is True
        assert fourth.discovery_reason == "MUSICAL_CHANGE"
        assert fourth.references["primary"]["title"] == "Continuous Reference"
        assert len(references.calls) == 2

        song.tempo = 130.2
        fifth = asyncio.run(observer.poll_once())
        assert fifth.observation_committed is True
        assert fifth.discovery_performed is False
        assert fifth.discovery_reason is None
        assert len(references.calls) == 2

        explicit = asyncio.run(observer.poll_once(force_discovery=True))
        assert explicit.observation_committed is False
        assert explicit.discovery_performed is True
        assert explicit.discovery_reason == "EXPLICIT"
        assert len(references.calls) == 3
    finally:
        bridge.disconnect()
        headquarters.close()


def test_provider_failure_does_not_erase_host_truth_or_hammer_retry_loop(tmp_path):
    clock = _Clock()
    failing = _ReferenceClient(fail=True)
    headquarters, song, bridge, references, observer = _build(
        tmp_path,
        reference_client=failing,
        clock=clock,
    )
    try:
        first = asyncio.run(observer.poll_once())
        assert first.observation_committed is True
        assert first.discovery_performed is False
        assert first.discovery_error_class == "RuntimeError"
        assert first.observation.status == "COMPLETE"
        assert len(references.calls) == 1

        song.current_song_time = 10.0
        second = asyncio.run(observer.poll_once())
        assert second.observation_committed is False
        assert second.discovery_deferred is True
        assert second.discovery_reason == "RETRY"
        assert len(references.calls) == 1

        clock.now = 31.0
        third = asyncio.run(observer.poll_once())
        assert third.discovery_deferred is False
        assert third.discovery_performed is False
        assert third.discovery_error_class == "RuntimeError"
        assert len(references.calls) == 2
    finally:
        bridge.disconnect()
        headquarters.close()


def test_continuous_watch_stops_cleanly_without_background_task_leak(tmp_path):
    headquarters, _, bridge, _, observer = _build(tmp_path)

    async def scenario():
        stop = asyncio.Event()
        cycles = []
        async for cycle in observer.watch(stop_event=stop):
            cycles.append(cycle)
            stop.set()
        return cycles

    try:
        cycles = asyncio.run(scenario())
        assert len(cycles) == 1
        assert cycles[0].sequence == 1
    finally:
        bridge.disconnect()
        headquarters.close()
