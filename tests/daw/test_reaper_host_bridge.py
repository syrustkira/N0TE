from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.memory import HeadquartersMemory
from n0te.reaper_host_bridge import (
    REAPER_SNAPSHOT_SCHEMA,
    ReaperHostBridgeError,
    ReaperObservationSnapshot,
    ReaperSnapshotFileClient,
)


def _payload(*, selected_tracks=None, observed_at=100):
    return {
        "schema": REAPER_SNAPSHOT_SCHEMA,
        "adapter": {"id": "n0te-reaper-reascript", "version": "1"},
        "bridge_session_id": "reaper-12345678-abcd-ef01-2345-6789abcdef01",
        "observed_at_epoch_seconds": observed_at,
        "runtime": {
            "host_family": "REAPER",
            "version": "7.79/x64",
            "edition": "REAPER",
            "os_name": "Linux",
            "machine": "x86_64",
        },
        "project": {
            "name": "TellMeN0TE Project.rpp",
            "saved": True,
            "state_change_count": 42,
        },
        "tempo_bpm": 128.0,
        "transport": {
            "play_state": 1,
            "play_position_seconds": 12.5,
            "repeat_enabled": True,
        },
        "track_count": 12,
        "selected_tracks": selected_tracks
        if selected_tracks is not None
        else [{"index": 4, "guid": "{TRACK-ONE}", "name": "Drum Bus"}],
        "selection_truncated": False,
    }


def test_snapshot_maps_reaper_truth_to_focus_capabilities_and_shadow():
    snapshot = ReaperObservationSnapshot.from_payload(_payload())

    assert snapshot.runtime.family == "REAPER"
    assert snapshot.runtime.version == "7.79/x64"
    assert snapshot.location_ref.startswith("reaper-session:")
    assert snapshot.project_name == "TellMeN0TE Project.rpp"
    assert snapshot.project_saved is True
    assert snapshot.project_state_change_count == 42
    assert snapshot.is_playing is True
    assert snapshot.is_paused is False
    assert snapshot.is_recording is False

    capabilities = {fact.capability for fact in snapshot.capabilities()}
    assert capabilities == {
        "tempo.read",
        "transport.read",
        "project.metadata.read",
        "project.state-change.read",
        "track.count.read",
        "focus.track.read",
    }
    focus = snapshot.focus_dimensions()
    assert len(focus) == 1
    assert focus[0].dimension == "TRACK"
    assert focus[0].state == "OBSERVED_EXACT"
    assert focus[0].refs == ("reaper-track:{TRACK-ONE}",)

    facts = {
        (event.object_kind, event.object_ref, event.field): event.value
        for event in snapshot.shadow().events
    }
    assert facts[("TEMPO", "tempo:main", "bpm")] == 128.0
    assert facts[("TRANSPORT", "transport:main", "is_playing")] is True
    assert facts[("TRANSPORT", "transport:main", "play_position_seconds")] == 12.5
    assert facts[("TRANSPORT", "transport:main", "repeat_enabled")] is True
    assert facts[("TRACK", "reaper-track:{TRACK-ONE}", "name")] == "Drum Bus"


def test_multiple_selected_tracks_are_explicitly_ambiguous_focus():
    payload = _payload(
        selected_tracks=[
            {"index": 1, "guid": "{TRACK-A}", "name": "A"},
            {"index": 2, "guid": "{TRACK-B}", "name": "B"},
        ]
    )
    focus = ReaperObservationSnapshot.from_payload(payload).focus_dimensions()[0]
    assert focus.state == "OBSERVED_AMBIGUOUS"
    assert focus.refs == (
        "reaper-track:{TRACK-A}",
        "reaper-track:{TRACK-B}",
    )


def test_parser_rejects_raw_project_paths_and_untrusted_shapes():
    payload = _payload()
    payload["project"]["path"] = "/secret/music/song.rpp"
    with pytest.raises(ReaperHostBridgeError, match="unsupported fields"):
        ReaperObservationSnapshot.from_payload(payload)

    payload = _payload()
    payload["project_path"] = "/secret/music/song.rpp"
    with pytest.raises(ReaperHostBridgeError, match="unsupported fields"):
        ReaperObservationSnapshot.from_payload(payload)

    payload = _payload(
        selected_tracks=[
            {"index": 1, "guid": "{DUP}", "name": "One"},
            {"index": 2, "guid": "{DUP}", "name": "Two"},
        ]
    )
    with pytest.raises(ReaperHostBridgeError, match="GUIDs must be unique"):
        ReaperObservationSnapshot.from_payload(payload)

    payload = _payload(selected_tracks=[{"index": 99, "guid": "{BAD}", "name": "Bad"}])
    with pytest.raises(ReaperHostBridgeError, match="inside current track_count"):
        ReaperObservationSnapshot.from_payload(payload)


def test_snapshot_file_client_enforces_freshness_and_regular_file(tmp_path: Path):
    path = tmp_path / "n0te_snapshot.json"
    path.write_text(json.dumps(_payload(observed_at=100)), encoding="utf-8")
    client = ReaperSnapshotFileClient(
        path,
        now_epoch_seconds=lambda: 103.0,
        max_age_seconds=5.0,
    )
    assert client.fetch_snapshot().tempo_bpm == 128.0

    stale = ReaperSnapshotFileClient(
        path,
        now_epoch_seconds=lambda: 200.0,
        max_age_seconds=5.0,
    )
    with pytest.raises(ReaperHostBridgeError, match="stale"):
        stale.fetch_snapshot()


class _ReferenceClient:
    def __init__(self):
        self.calls = []

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        primary = {
            "title": "REAPER Reference",
            "source_locator": "catalog:reaper-reference",
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


def test_reaper_snapshot_reaches_canonical_reference_workflow(tmp_path: Path):
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "REAPER Bridge Artist")
    snapshot_path = tmp_path / "n0te_snapshot.json"
    snapshot_path.write_text(json.dumps(_payload(observed_at=100)), encoding="utf-8")
    client = ReaperSnapshotFileClient(
        snapshot_path,
        now_epoch_seconds=lambda: 100.0,
    )
    try:
        song = headquarters.store.create_song("REAPER Bridge Song")
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        result = asyncio.run(
            client.observe_and_discover(
                workflow,
                provider_id="session-local",
                comparison_dimensions=("tempo",),
                required_features=("tempo_bpm",),
            )
        )

        assert result.handshake.status == "CREATED"
        assert result.observation.status == "COMPLETE"
        assert result.observation.binding.song_id == song.id
        assert result.handshake.workspace.workspace.host_family == "REAPER"
        assert result.references["primary"]["title"] == "REAPER Reference"
        assert result.references["read_only"] is True
        assert result.references["action_authority_granted"] is False
        assert len(reference_client.calls) == 1
        _, binding, shadow, kwargs = reference_client.calls[0]
        assert binding == result.observation.binding
        assert shadow.status == "CURRENT"
        assert kwargs["comparison_dimensions"] == ("tempo",)
        assert any(
            fact.object_kind == "TEMPO"
            and fact.field == "bpm"
            and fact.value == 128.0
            for fact in shadow.facts
        )
    finally:
        headquarters.close()


def test_reaper_script_contains_no_project_mutation_or_raw_path_field():
    root = Path(__file__).resolve().parents[2]
    script = (root / "integrations" / "reaper" / "N0TEBridge.lua").read_text(
        encoding="utf-8"
    )
    assert "reaper.defer(loop)" in script
    assert "EnumProjects(-1" in script
    assert "GetProjectName" in script
    assert "GetProjectStateChangeCount" in script
    assert "Master_GetTempo" in script
    assert "CountSelectedTracks" in script
    assert '"path"' not in script
    for forbidden in (
        "SetMediaTrackInfo_Value",
        "SetTrackSelected",
        "SetCurrentBPM",
        "Main_OnCommand",
        "CSurf_OnPlay",
        "CSurf_OnStop",
    ):
        assert forbidden not in script
