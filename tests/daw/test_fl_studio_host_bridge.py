from __future__ import annotations

import asyncio
import json
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

from n0te.fl_studio_host_bridge import (
    FL_STUDIO_SNAPSHOT_SCHEMA,
    FLStudioHostBridgeError,
    FLStudioObservationSnapshot,
    FLStudioSnapshotFileClient,
)
from n0te.host_session_handshake import HostSessionReferenceWorkflow
from n0te.memory import HeadquartersMemory


class _ReferenceClient:
    def __init__(self):
        self.calls = []

    async def discover_session(self, provider_id, binding, shadow, **kwargs):
        self.calls.append((provider_id, binding, shadow, kwargs))
        primary = {
            "title": "FL Reference",
            "source_locator": "catalog:fl-reference",
        }
        return {
            "provider_id": provider_id,
            "read_only": True,
            "action_authority_granted": False,
            "session_calibration": {
                "workspace_id": binding.workspace_id,
                "song_id": binding.song_id,
                "workspace_observation_id": binding.workspace_observation_id,
                "features": {"TEMPO_BPM": 132.5},
                "evidence": [],
            },
            "ranked": [primary],
            "primary": primary,
        }


def _payload(**overrides):
    payload = {
        "schema": FL_STUDIO_SNAPSHOT_SCHEMA,
        "adapter": {"id": "N0TEBridge", "version": "1"},
        "bridge_session_id": "fl-session-123",
        "runtime": {
            "host_family": "FL_STUDIO",
            "version": "2026.1.4.1234",
            "edition": "FL Studio 2026.1 Producer Edition",
            "os_name": "Windows",
            "machine": "AMD64",
        },
        "observed_at_epoch_seconds": 100,
        "project": {"title": "TellMeN0TE Project", "changed_flag": 1},
        "tempo_bpm": 132.5,
        "transport": {"is_playing": True, "song_position": 0.25, "loop_mode": 1},
        "selected_mixer_track": {"index": 4, "name": "Drum Bus"},
        "selected_channel": {"index": 2, "name": "Serum"},
        "active_window": {"form_id": 7, "caption": "Serum", "plugin_name": "Serum"},
    }
    payload.update(overrides)
    return payload


def test_snapshot_parser_is_strict_and_builds_only_observed_fl_evidence():
    snapshot = FLStudioObservationSnapshot.from_payload(_payload())
    assert snapshot.runtime.family == "FL_STUDIO"
    assert snapshot.location_ref == "fl-studio-session:fl-session-123"
    assert snapshot.project_title == "TellMeN0TE Project"
    assert snapshot.selected_mixer_track.name == "Drum Bus"
    assert snapshot.selected_channel.name == "Serum"

    capabilities = {item.capability for item in snapshot.capabilities()}
    assert capabilities == {
        "tempo.read",
        "transport.read",
        "project.metadata.read",
        "focus.track.read",
        "focus.active-editor.read",
    }
    dimensions = {item.dimension: item for item in snapshot.focus_dimensions()}
    assert dimensions["TRACK"].refs == ("mixer-track:4",)
    assert dimensions["ACTIVE_EDITOR"].refs == ("fl-window:7",)
    assert dimensions["DEVICE_PLUGIN"].refs == ("fl-plugin:7:Serum",)

    facts = {
        (event.object_kind, event.object_ref, event.field): event.value
        for event in snapshot.shadow().events
    }
    assert facts[("TEMPO", "tempo:main", "bpm")] == 132.5
    assert facts[("TRANSPORT", "transport:main", "is_playing")] is True
    assert facts[("TRACK", "mixer-track:4", "name")] == "Drum Bus"

    with pytest.raises(FLStudioHostBridgeError, match="unsupported fields"):
        FLStudioObservationSnapshot.from_payload(
            _payload(calibration={"tempo_bpm": 150.0})
        )
    with pytest.raises(FLStudioHostBridgeError, match="FL_STUDIO"):
        FLStudioObservationSnapshot.from_payload(
            _payload(runtime={
                "host_family": "ABLETON_LIVE",
                "version": "12",
                "edition": "Suite",
                "os_name": "Windows",
                "machine": "AMD64",
            })
        )


def test_file_client_rejects_stale_future_and_symlink_snapshots(tmp_path: Path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")

    with pytest.raises(FLStudioHostBridgeError, match="stale"):
        FLStudioSnapshotFileClient(
            path,
            max_age_seconds=5,
            now_epoch_seconds=lambda: 106.0,
        ).fetch_snapshot()

    future = _payload(observed_at_epoch_seconds=120)
    path.write_text(json.dumps(future), encoding="utf-8")
    with pytest.raises(FLStudioHostBridgeError, match="future"):
        FLStudioSnapshotFileClient(
            path,
            max_age_seconds=5,
            now_epoch_seconds=lambda: 100.0,
        ).fetch_snapshot()

    target = tmp_path / "real.json"
    target.write_text(json.dumps(_payload()), encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(FLStudioHostBridgeError, match="symlink"):
        FLStudioSnapshotFileClient(
            link,
            now_epoch_seconds=lambda: 100.0,
        ).fetch_snapshot()


def _fake_module(name: str, **attrs) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _load_fl_script(monkeypatch, snapshot_path: Path):
    versions = {
        0: 2026,
        1: 1,
        2: 4,
        3: 1234,
        4: "FL Studio 2026.1 Producer Edition",
        5: "FL Studio 2026.1.4 Producer Edition",
    }
    monkeypatch.setitem(
        sys.modules,
        "mixer",
        _fake_module(
            "mixer",
            trackNumber=lambda: 4,
            getTrackName=lambda index: "Drum Bus",
            getCurrentTempo=lambda: 132.5,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "channels",
        _fake_module(
            "channels",
            selectedChannel=lambda canBeNone=0, offset=0, indexGlobal=0: 2,
            getChannelName=lambda index, useGlobalIndex=False: "Serum",
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "transport",
        _fake_module(
            "transport",
            isPlaying=lambda: 1,
            getSongPos=lambda: 0.25,
            getLoopMode=lambda: 1,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "ui",
        _fake_module(
            "ui",
            getVersion=lambda mode=4: versions[mode],
            getFocusedFormID=lambda: 7,
            getFocusedFormCaption=lambda: "Serum",
            getFocusedPluginName=lambda: "Serum",
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "general",
        _fake_module(
            "general",
            getProjectTitle=lambda: "TellMeN0TE Project",
            getChangedFlag=lambda: 1,
        ),
    )

    script = (
        Path(__file__).resolve().parents[2]
        / "integrations"
        / "fl_studio"
        / "N0TEBridge"
        / "device_N0TEBridge.py"
    )
    namespace = runpy.run_path(str(script))
    namespace["OnInit"].__globals__["_SNAPSHOT_PATH"] = str(snapshot_path)
    return namespace, script


def test_real_fl_script_writes_atomic_read_only_snapshot_and_external_client_reads_it(
    tmp_path: Path,
    monkeypatch,
):
    snapshot_path = tmp_path / "n0te_snapshot.json"
    namespace, script = _load_fl_script(monkeypatch, snapshot_path)
    namespace["OnInit"]()

    raw = snapshot_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["schema"] == FL_STUDIO_SNAPSHOT_SCHEMA
    assert payload["tempo_bpm"] == 132.5
    assert payload["selected_mixer_track"] == {"index": 4, "name": "Drum Bus"}
    assert payload["selected_channel"] == {"index": 2, "name": "Serum"}
    assert payload["project"]["title"] == "TellMeN0TE Project"
    assert "path" not in raw.casefold()

    client = FLStudioSnapshotFileClient(
        snapshot_path,
        now_epoch_seconds=lambda: float(payload["observed_at_epoch_seconds"]),
    )
    snapshot = client.fetch_snapshot()
    assert snapshot.runtime.family == "FL_STUDIO"
    assert snapshot.tempo_bpm == 132.5

    source = script.read_text(encoding="utf-8")
    for forbidden in (
        "setCurrentTempo",
        "transport.start",
        "transport.stop",
        "setTrackName",
        "setChannelName",
        "setHintMsg",
        "socket",
        "urllib",
    ):
        assert forbidden not in source

    namespace["OnDeInit"]()
    assert not snapshot_path.exists()


def test_fl_snapshot_roundtrips_into_canonical_reference_workflow(tmp_path: Path):
    snapshot_path = tmp_path / "n0te_snapshot.json"
    snapshot_path.write_text(json.dumps(_payload()), encoding="utf-8")
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "FL Bridge Artist")
    try:
        song = headquarters.store.create_song("FL Bridge Song")
        reference_client = _ReferenceClient()
        workflow = HostSessionReferenceWorkflow(
            headquarters.host_observation,
            reference_client,
        )
        client = FLStudioSnapshotFileClient(
            snapshot_path,
            now_epoch_seconds=lambda: 100.0,
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
        assert result.handshake.workspace.workspace.host_family == "FL_STUDIO"
        assert result.handshake.workspace.current_observation.location_ref.startswith(
            "fl-studio-session:"
        )
        assert result.references["primary"]["title"] == "FL Reference"
        assert result.references["read_only"] is True
        assert result.references["action_authority_granted"] is False
        assert len(reference_client.calls) == 1
        _, binding, shadow, _ = reference_client.calls[0]
        assert binding == result.observation.binding
        assert shadow.status == "CURRENT"
        assert any(
            fact.object_kind == "TEMPO" and fact.field == "bpm" and fact.value == 132.5
            for fact in shadow.facts
        )
    finally:
        headquarters.close()
