from __future__ import annotations

import asyncio
import json

import pytest

from integrations.ableton.N0TEBridge import N0TEBridge, WORKSPACE_DATA_KEY
from n0te.ableton_host_bridge import (
    ABLETON_LEGACY_SNAPSHOT_SCHEMA,
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
    def __init__(self, *, workspace_id=None, tempo=128.0, file_path=""):
        self.tracks = (_Track("Drums"), _Track("Lead"))
        self.return_tracks = (_Track("Reverb"),)
        self.master_track = _Track("Master")
        self.view = _View(self.tracks[1])
        self.tempo = tempo
        self.is_playing = True
        self.current_song_time = 33.5
        self.file_path = file_path
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
        "adapter": {"id": "N0TEBridge", "version": "2"},
        "bridge_session_id": "session-123",
        "workspace_id": None,
        "set_path_fingerprint": None,
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
    assert snapshot.has_durable_set_location is False
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

    malformed = _payload(set_path_fingerprint="not-a-digest")
    with pytest.raises(AbletonHostBridgeError, match="SHA-256"):
        AbletonObservationSnapshot.from_payload(malformed)


def test_v1_snapshot_remains_accepted_without_durable_path_field():
    payload = _payload(schema=ABLETON_LEGACY_SNAPSHOT_SCHEMA)
    payload["adapter"] = {"id": "N0TEBridge", "version": "1"}
    payload.pop("set_path_fingerprint")
    snapshot = AbletonObservationSnapshot.from_payload(payload)
    assert snapshot.location_ref == "ableton-session:session-123"
    assert snapshot.set_path_fingerprint is None

    payload["set_path_fingerprint"] = "0" * 64
    with pytest.raises(AbletonHostBridgeError, match="unsupported fields"):
        AbletonObservationSnapshot.from_payload(payload)


def test_remote_script_capture_hashes_saved_set_path_and_never_exposes_or_writes_it():
    raw_path = "/tmp/TellMeN0TE/Album/Track One.als"
    song = _Song(
        workspace_id="wsp_0123456789abcdef",
        file_path=raw_path,
    )
    bridge = N0TEBridge(_CInstance(song), start_server=False)
    try:
        snapshot = bridge._capture_snapshot()
    finally:
        bridge.disconnect()

    assert snapshot["schema"] == ABLETON_SNAPSHOT_SCHEMA
    assert snapshot["adapter"]["version"] == "2"
    assert snapshot["workspace_id"] == "wsp_0123456789abcdef"
    assert len(snapshot["set_path_fingerprint"]) == 64
    assert snapshot["tempo_bpm"] == 128.0
    assert snapshot["runtime"]["version"] == "12.4.5"
    assert snapshot["selected_track"] == {
        "kind": "TRACK",
        "index": 1,
        "name": "Lead",
    }
    assert raw_path not in json.dumps(snapshot, sort_keys=True)
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
        assert snapshot.set_path_fingerprint is None
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


def test_saved_set_path_reuses_workspace_across_new_bridge_sessions_without_token(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Ableton Bridge Artist")
    bridge_a = None
    bridge_b = None
    raw_path_a = "/tmp/TellMeN0TE/Album/Saved Set.als"
    raw_path_b = "/tmp/TellMeN0TE/Album/./Saved Set.als"
    try:
        headquarters.store.create_song("Bridge Song")
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )

        live_song_a = _Song(file_path=raw_path_a)
        bridge_a = N0TEBridge(_CInstance(live_song_a), port=0, start_server=True)
        client_a = AbletonRemoteScriptClient(
            endpoint=f"http://127.0.0.1:{bridge_a.port}",
        )
        first = asyncio.run(
            client_a.observe_and_discover(workflow, provider_id="session-local")
        )
        first_workspace = first.observation.binding.workspace_id
        first_location = first.handshake.workspace.current_observation.location_ref
        assert first.handshake.status == "CREATED"
        assert first_location.startswith("ableton-set:sha256:")
        assert raw_path_a not in first_location
        bridge_a.disconnect()
        bridge_a = None

        live_song_b = _Song(file_path=raw_path_b)
        bridge_b = N0TEBridge(_CInstance(live_song_b), port=0, start_server=True)
        client_b = AbletonRemoteScriptClient(
            endpoint=f"http://127.0.0.1:{bridge_b.port}",
        )
        second = asyncio.run(
            client_b.observe_and_discover(workflow, provider_id="session-local")
        )

        assert second.handshake.status == "REUSED"
        assert second.observation.binding.workspace_id == first_workspace
        assert second.handshake.workspace.current_observation.location_ref == first_location
        assert live_song_a.set_data_calls == []
        assert live_song_b.set_data_calls == []
    finally:
        if bridge_a is not None:
            bridge_a.disconnect()
        if bridge_b is not None:
            bridge_b.disconnect()
        headquarters.close()


def test_existing_set_workspace_token_reconciles_canonical_workspace_after_move(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Ableton Bridge Artist")
    bridge = None
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
            location_ref="ableton-set:sha256:" + ("1" * 64),
            display_name="Ableton Live Set",
        )

        live_song = _Song(
            workspace_id=existing.id,
            file_path="/tmp/TellMeN0TE/Moved/Saved Set.als",
        )
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
            )
        )

        assert result.handshake.status == "RECONCILED"
        assert result.observation.binding.workspace_id == existing.id
        current_location = headquarters.workspaces.state(
            existing.id
        ).current_observation.location_ref
        assert current_location.startswith("ableton-set:sha256:")
        assert current_location != "ableton-set:sha256:" + ("1" * 64)
        assert len(headquarters.workspaces.history(existing.id)) == 2
        assert live_song.set_data_calls == []
    finally:
        if bridge is not None:
            bridge.disconnect()
        headquarters.close()
