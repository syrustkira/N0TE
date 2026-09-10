from __future__ import annotations

from pathlib import Path

import pytest

from n0te import HeadquartersMemory
from n0te.daw_inspection import DAWInspectionError, inspect_current_daw
from n0te.hosts import HostRuntimeIdentity
from n0te.shadow import ShadowEventInput


def _workspace(tmp_path: Path):
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "Inspection Artist")
    song = headquarters.store.create_song("Inspection Song")
    runtime = HostRuntimeIdentity.from_runtime_labels(
        host_family="ABLETON_LIVE",
        version="12.1",
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    )
    workspace = headquarters.workspaces.create(
        song.id,
        runtime=runtime,
        location_ref="inspection:test",
        display_name="Inspection Session",
    )
    return headquarters, workspace


def test_daw_inspection_unifies_current_shadow_and_active_tools(tmp_path: Path) -> None:
    headquarters, workspace = _workspace(tmp_path)
    try:
        state = headquarters.workspaces.state(workspace.id)
        headquarters.shadow.record_batch(
            workspace.id,
            workspace_observation_id=state.current_observation.id,
            host_runtime_fingerprint=state.current_observation.host_runtime_fingerprint,
            coverage="FULL",
            actor="EXTERNAL",
            evidence_ref="inspection:batch",
            verified=True,
            events=(
                ShadowEventInput("TEMPO", "tempo:main", "bpm", "SET", 128.0, "host:tempo"),
                ShadowEventInput("TRANSPORT", "transport:main", "playing", "SET", True, "host:transport"),
                ShadowEventInput("ROUTING", "routing:track:1", "output_ref", "SET", "master", "host:routing"),
                ShadowEventInput("TRACK", "track:1", "name", "SET", "Lead", "host:track"),
                ShadowEventInput("TRACK", "track:1", "device_count", "SET", 1, "host:chain"),
                ShadowEventInput("DEVICE_PLUGIN", "device:1", "track_ref", "SET", "track:1", "host:device"),
                ShadowEventInput("DEVICE_PLUGIN", "device:1", "index", "SET", 0, "host:device"),
                ShadowEventInput("DEVICE_PLUGIN", "device:1", "name", "SET", "Serum", "host:device"),
                ShadowEventInput("DEVICE_PLUGIN", "device:1", "class_name", "SET", "PluginDevice", "host:device"),
                ShadowEventInput("DEVICE_PLUGIN", "device:1", "enabled", "SET", True, "host:device"),
                ShadowEventInput("DEVICE_PLUGIN", "device:1", "offline", "SET", False, "host:device"),
            ),
        )

        inspection = inspect_current_daw(
            headquarters.workspaces,
            headquarters.shadow,
            workspace.id,
        )
    finally:
        headquarters.close()

    assert inspection.workspace_id == workspace.id
    assert inspection.host_family == "ABLETON_LIVE"
    assert inspection.read_only is True
    assert inspection.action_authority_granted is False
    assert [item.object_kind for item in inspection.objects] == [
        "ROUTING",
        "TEMPO",
        "TRACK",
        "TRANSPORT",
    ]
    tempo = next(item for item in inspection.objects if item.object_kind == "TEMPO")
    assert [(field.name, field.value, field.evidence_ref) for field in tempo.fields] == [
        ("bpm", 128.0, "host:tempo")
    ]
    assert inspection.active_tools.scopes[0].parent_name == "Lead"
    assert [tool.name for tool in inspection.active_tools.scopes[0].tools] == ["Serum"]


def test_daw_inspection_fails_closed_without_current_verified_shadow(tmp_path: Path) -> None:
    headquarters, workspace = _workspace(tmp_path)
    try:
        with pytest.raises(DAWInspectionError, match="CURRENT verified Host Shadow"):
            inspect_current_daw(
                headquarters.workspaces,
                headquarters.shadow,
                workspace.id,
            )
    finally:
        headquarters.close()
