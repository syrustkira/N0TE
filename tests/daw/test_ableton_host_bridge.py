from __future__ import annotations

import asyncio

import pytest

from integrations.ableton.N0TEBridge import N0TEBridge, WORKSPACE_DATA_KEY
from n0te.ableton_host_bridge import (
    ABLETON_SNAPSHOT_SCHEMA,
    AbletonHostBridgeError,
    AbletonObservationSnapshot,
    AbletonRemoteScriptClient,
)
from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.hosts import HostRuntimeIdentity
from n0te.memory import HeadquartersMemory


class _Track:
    def __init__(self, name):
        self.name = name


class _View:
    def __init__(self, selected_track):
        self.selected_track = selected_track


class _Song:
    def __init__(self, *, workspace_id=None, tempo=128.0):
        self.tracks = (_Track("Drums"), _Track("Lead"))
        self.return_tracks = (_Track("Reverb"),)
        self.master_track = _Track("Master")
        self.view = _View(self.tracks[1])
        self.tempo = tempo
        self.is_playing = True
        self.current_song_time = 33.5
        self._workspace_id = workspace_id
        self.get_data_calls = []
        self.set_data_calls = []

    def get_data(self, key, default=None):
        self.get_data_calls.append((key, default))
        if key == WORKSPACE_DATA_KEY:
            return self._workspace_id
        return default

    def set_data(self, *args):
        self.set_data_calls.append(args)
        raise AssertionError("read-only N0TE bridge must never call set_data")


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
        self.logs = []

    def song(self):
        return self._song

    def application(self):
        return _Application()

    def log_message(self, message):
        self.logs.append(str(message))


class _ReferenceClient:
    def __init__(self):
        self.calls = []

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        primary = {
            "title": "Live Reference",
            "source_locator": "catalog:live-reference",
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


def _payload(**overrides):
    payload = {
        "schema": ABLETON_SNAPSHOT_SCHEMA,
        "adapter": {"id": "N0TEBridge", "version": "1"},
        "bridge_session_id": "session-123",
        "workspace_id": None,
        "runtime": {
            "host_family": "ABLETON_LIVE",
            "version": "12.4.5",
            "edition": "Unknown",
            "os_name": "Darwin",
            "machine": "arm64",
        },
        "observed_at_epoch_seconds": 100,
        "tempo_bpm": 128.0,
        "transport": {"is_playing": True, "current_song_time": 33.5},
        "selected_track": {"kind": "TRACK", "index": 1, "name": "Lead"},
    }
    payload.update(overrides)
    return payload


def test_snapshot_parser_is_strict_and_builds_only_observed_evidence():
    snapshot = AbletonObservationSnapshot.from_payload(_payload())
    assert snapshot.runtime.family == "ABLETON_LIVE"
    assert snapshot.runtime.version == "12.4.5"
    assert snapshot.location_ref == "ableton-session:session-123"
    assert snapshot.selected_track.ref == "track:1"
    assert {item.capability for item in snapshot.capabilities()} == {
        "tempo.read",
        "transport.read",
        "focus.track.read",
    }
    assert snapshot.focus_dimensions()[0].refs == ("track:1",)
    facts = {
        (event.object_kind, event.object_ref, event.field): event.value
        for event in snapshot.shadow().events
    }
    assert facts[("TEMPO", "tempo:main", "bpm")] == 128.0
    assert facts[("TRANSPORT", "transport:main", "is_playing")] is True
    assert facts[("TRACK", "track:1", "name")] == "Lead"

    smuggled = _payload(calibration={"tempo_bpm": 150.0})
    with pytest.raises(AbletonHostBridgeError, match="unsupported fields"):
        AbletonObservationSnapshot.from_payload(smuggled)


def test_remote_script_capture_reads_workspace_token_but_never_writes_live_set():
    song = _Song(workspace_id="wsp_0123456789abcdef")
    bridge = N0TEBridge(_CInstance(song), start_server=False)
    try:
        snapshot = bridge._capture_snapshot()
    finally:
        bridge.disconnect()

    assert snapshot["schema"] == ABLETON_SNAPSHOT_SCHEMA
    assert snapshot["workspace_id"] == "wsp_0123456789abcdef"
    assert snapshot["tempo_bpm"] == 128.0
    assert snapshot["runtime"]["version"] == "12.4.5"
    assert snapshot["selected_track"] == {
        "kind": "TRACK",
        "index": 1,
        "name": "Lead",
    }
    assert song.get_data_calls == [(WORKSPACE_DATA_KEY, None)]
    assert song.set_data_calls == []


def test_remote_script_http_is_loopback_get_only_and_external_client_reads_it():
    song = _Song()
    bridge = N0TEBridge(_CInstance(song), port=0, start_server=True)
    try:
        client = AbletonRemoteScriptClient(
            endpoint=f"http://127.0.0.1:{bridge.port}",
        )
        snapshot = client.fetch_snapshot()
        assert snapshot.tempo_bpm == 128.0
        assert snapshot.selected_track.name == "Lead"
        assert snapshot.workspace_id is None
    finally:
        bridge.disconnect()

    with pytest.raises(AbletonHostBridgeError, match="loopback-only"):
        AbletonRemoteScriptClient(endpoint="http://example.com:9799")


def test_live_snapshot_roundtrips_into_canonical_reference_workflow(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Ableton Bridge Artist")
    bridge = None
    try:
        song_identity = headquarters.store.create_song("Bridge Song")
        live_song = _Song()
        bridge = N0TEBridge(_CInstance(live_song), port=0, start_server=True)
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        live_client = AbletonRemoteScriptClient(
            endpoint=f"http://127.0.0.1:{bridge.port}",
        )

        result = asyncio.run(
            live_client.observe_and_discover(
                workflow,
                provider_id="session-local",
                comparison_dimensions=("tempo",),
                required_features=("tempo_bpm",),
            )
        )

        assert result.handshake.status == "CREATED"
        assert result.observation.status == "COMPLETE"
        assert result.observation.binding.song_id == song_identity.id
        assert result.references["primary"]["title"] == "Live Reference"
        assert result.references["read_only"] is True
        assert result.references["action_authority_granted"] is False
        assert len(reference_client.calls) == 1
        _, binding, shadow, _ = reference_client.calls[0]
        assert binding == result.observation.binding
        assert shadow.status == "CURRENT"
        assert any(
            fact.object_kind == "TEMPO" and fact.field == "bpm" and fact.value == 128.0
            for fact in shadow.facts
        )
        assert live_song.set_data_calls == []
    finally:
        if bridge is not None:
            bridge.disconnect()
        headquarters.close()


def test_existing_set_workspace_token_reuses_canonical_workspace_after_reopen(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Ableton Bridge Artist")
    bridge_a = None
    bridge_b = None
    try:
        headquarters.store.create_song("Bridge Song")
        runtime = HostRuntimeIdentity.from_runtime_labels(
            host_family="ABLETON_LIVE",
            version="12.4.5",
            edition="Unknown",
            os_name="Linux",
            machine="x86_64",
        )
        existing = headquarters.workspaces.create(
            headquarters.store.active_song().id,
            runtime=runtime,
            location_ref="ableton-session:older-session",
            display_name="Ableton Live Set",
        )

        live_song = _Song(workspace_id=existing.id)
        bridge_b = N0TEBridge(_CInstance(live_song), port=0, start_server=True)
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        live_client = AbletonRemoteScriptClient(
            endpoint=f"http://127.0.0.1:{bridge_b.port}",
        )
        result = asyncio.run(
            live_client.observe_and_discover(
                workflow,
                provider_id="session-local",
            )
        )

        assert result.handshake.status == "RECONCILED"
        assert result.observation.binding.workspace_id == existing.id
        assert headquarters.workspaces.state(existing.id).current_observation.location_ref.startswith(
            "ableton-session:"
        )
        assert len(headquarters.workspaces.history(existing.id)) == 2
        assert live_song.set_data_calls == []
    finally:
        if bridge_a is not None:
            bridge_a.disconnect()
        if bridge_b is not None:
            bridge_b.disconnect()
        headquarters.close()
