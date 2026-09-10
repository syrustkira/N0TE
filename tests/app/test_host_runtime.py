from __future__ import annotations

from pathlib import Path

import pytest

from n0te import HeadquartersMemory
from n0te.host_runtime import (
    ActiveToolProjectionError,
    CoordinatorProcessSupervisor,
    HostRuntimeError,
    project_active_tools,
    reference_backend,
    reference_route_kind,
    resolve_profile_id,
    resolve_provider_id,
)
from n0te.hosts import HostRuntimeIdentity
from n0te.shadow import ShadowEventInput


def _profile(root: Path, suffix: str) -> str:
    profile_id = "prf_" + suffix * 32
    path = root / "profiles" / profile_id
    path.mkdir(parents=True)
    (path / "lineage.sqlite3").write_bytes(b"placeholder")
    return profile_id


def _shadowed_workspace(
    tmp_path: Path,
    events: tuple[ShadowEventInput, ...],
    *,
    family: str = "ABLETON_LIVE",
):
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "Runtime Artist")
    song = headquarters.store.create_song("Runtime Song")
    runtime = HostRuntimeIdentity.from_runtime_labels(
        host_family=family,
        version="1.0",
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    )
    workspace = headquarters.workspaces.create(
        song.id,
        runtime=runtime,
        location_ref=f"runtime-test:{family.lower()}",
        display_name=f"{family} Runtime Test",
    )
    state = headquarters.workspaces.state(workspace.id)
    headquarters.shadow.record_batch(
        workspace.id,
        workspace_observation_id=state.current_observation.id,
        host_runtime_fingerprint=state.current_observation.host_runtime_fingerprint,
        coverage="FULL",
        actor="EXTERNAL",
        evidence_ref="runtime-test:shadow",
        verified=True,
        events=events,
    )
    return headquarters, workspace


def test_profile_resolution_is_host_neutral_and_fail_closed(
    tmp_path: Path,
) -> None:
    profile_id = _profile(tmp_path, "a")
    assert (
        resolve_profile_id(
            tmp_path,
            explicit=None,
            environment={},
        )
        == profile_id
    )
    _profile(tmp_path, "b")
    with pytest.raises(
        HostRuntimeError,
        match="multiple N0TE profiles",
    ):
        resolve_profile_id(
            tmp_path,
            explicit=None,
            environment={},
        )


def test_reference_provider_and_route_resolution_are_shared() -> None:
    assert (
        reference_backend({"OPENAI_API_KEY": "test-key"})
        == "openai_web"
    )
    provider, child = resolve_provider_id(
        explicit=None,
        environment={"OPENAI_API_KEY": "test-key"},
    )
    assert provider == "openai-web-reference-search"
    assert child["N0TE_REFERENCE_SEARCH_BACKEND"] == "openai_web"

    assert (
        reference_route_kind(
            {
                "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
                "N0TE_REFERENCE_SEARCH_ENDPOINT":
                    "http://127.0.0.1:9001/search",
            }
        )
        == "LOCALHOST"
    )
    assert (
        reference_route_kind(
            {
                "N0TE_REFERENCE_SEARCH_BACKEND": "http_json",
                "N0TE_REFERENCE_SEARCH_ENDPOINT":
                    "https://references.example.test/search",
            }
        )
        == "INTERNET"
    )


def test_coordinator_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(
        HostRuntimeError,
        match="loopback http /mcp",
    ):
        CoordinatorProcessSupervisor(
            "https://example.test/mcp",
            environment={},
        )


def test_fl_runtime_surface_has_no_ableton_launcher_dependency() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "n0te/fl_studio.py",
        "n0te/fl_studio_session_launcher.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "ableton_session_launcher" not in source


def test_active_tool_projection_normalizes_complete_track_chain_and_generator(
    tmp_path: Path,
) -> None:
    events = (
        ShadowEventInput(
            "TRACK",
            "track:1",
            "name",
            "SET",
            "Lead",
            "evidence:track",
        ),
        ShadowEventInput(
            "TRACK",
            "track:1",
            "device_count",
            "SET",
            2,
            "evidence:chain",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:0",
            "track_ref",
            "SET",
            "track:1",
            "evidence:a",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:0",
            "index",
            "SET",
            0,
            "evidence:a",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:0",
            "name",
            "SET",
            "Serum",
            "evidence:a",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:0",
            "class_name",
            "SET",
            "PluginDevice",
            "evidence:a",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:1",
            "track_ref",
            "SET",
            "track:1",
            "evidence:b",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:1",
            "index",
            "SET",
            1,
            "evidence:b",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:1",
            "name",
            "SET",
            "Pro-Q 4",
            "evidence:b",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:1",
            "enabled",
            "SET",
            True,
            "evidence:b",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:track:1:1",
            "offline",
            "SET",
            False,
            "evidence:b",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:channel:2:generator",
            "channel_ref",
            "SET",
            "channel:2",
            "evidence:generator",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:channel:2:generator",
            "role",
            "SET",
            "GENERATOR",
            "evidence:generator",
        ),
        ShadowEventInput(
            "DEVICE_PLUGIN",
            "device:channel:2:generator",
            "name",
            "SET",
            "Sytrus",
            "evidence:generator",
        ),
    )
    headquarters, workspace = _shadowed_workspace(tmp_path, events)
    try:
        projection = project_active_tools(
            headquarters.workspaces,
            headquarters.shadow,
            workspace.id,
        )
    finally:
        headquarters.close()

    assert projection.workspace_id == workspace.id
    assert projection.host_family == "ABLETON_LIVE"
    assert len(projection.scopes) == 2

    channel = next(scope for scope in projection.scopes if scope.parent_kind == "CHANNEL")
    assert channel.parent_ref == "channel:2"
    assert channel.coverage == "OBSERVED"
    assert channel.expected_count is None
    assert [tool.name for tool in channel.tools] == ["Sytrus"]
    assert channel.tools[0].role == "GENERATOR"

    track = next(scope for scope in projection.scopes if scope.parent_kind == "TRACK")
    assert track.parent_name == "Lead"
    assert track.coverage == "COMPLETE"
    assert track.expected_count == 2
    assert [tool.name for tool in track.tools] == ["Serum", "Pro-Q 4"]
    assert track.tools[0].class_name == "PluginDevice"
    assert track.tools[1].enabled is True
    assert track.tools[1].offline is False


def test_active_tool_projection_preserves_complete_empty_chain(
    tmp_path: Path,
) -> None:
    headquarters, workspace = _shadowed_workspace(
        tmp_path,
        (
            ShadowEventInput(
                "TRACK",
                "track:empty",
                "name",
                "SET",
                "Empty",
                "evidence:track",
            ),
            ShadowEventInput(
                "TRACK",
                "track:empty",
                "device_count",
                "SET",
                0,
                "evidence:chain",
            ),
        ),
    )
    try:
        projection = project_active_tools(
            headquarters.workspaces,
            headquarters.shadow,
            workspace.id,
        )
    finally:
        headquarters.close()

    assert len(projection.scopes) == 1
    scope = projection.scopes[0]
    assert scope.coverage == "COMPLETE"
    assert scope.expected_count == 0
    assert scope.tools == ()


def test_active_tool_projection_fails_closed_on_count_mismatch(
    tmp_path: Path,
) -> None:
    headquarters, workspace = _shadowed_workspace(
        tmp_path,
        (
            ShadowEventInput(
                "TRACK",
                "track:1",
                "device_count",
                "SET",
                2,
                "evidence:chain",
            ),
            ShadowEventInput(
                "DEVICE_PLUGIN",
                "device:1",
                "track_ref",
                "SET",
                "track:1",
                "evidence:device",
            ),
            ShadowEventInput(
                "DEVICE_PLUGIN",
                "device:1",
                "index",
                "SET",
                0,
                "evidence:device",
            ),
            ShadowEventInput(
                "DEVICE_PLUGIN",
                "device:1",
                "name",
                "SET",
                "Only One",
                "evidence:device",
            ),
        ),
    )
    try:
        with pytest.raises(
            ActiveToolProjectionError,
            match="device_count does not match",
        ):
            project_active_tools(
                headquarters.workspaces,
                headquarters.shadow,
                workspace.id,
            )
    finally:
        headquarters.close()


def test_active_tool_projection_rejects_ambiguous_device_parent(
    tmp_path: Path,
) -> None:
    headquarters, workspace = _shadowed_workspace(
        tmp_path,
        (
            ShadowEventInput(
                "TRACK",
                "track:1",
                "name",
                "SET",
                "Lead",
                "evidence:track",
            ),
            ShadowEventInput(
                "DEVICE_PLUGIN",
                "device:ambiguous",
                "track_ref",
                "SET",
                "track:1",
                "evidence:device",
            ),
            ShadowEventInput(
                "DEVICE_PLUGIN",
                "device:ambiguous",
                "channel_ref",
                "SET",
                "channel:2",
                "evidence:device",
            ),
            ShadowEventInput(
                "DEVICE_PLUGIN",
                "device:ambiguous",
                "name",
                "SET",
                "Impossible",
                "evidence:device",
            ),
        ),
    )
    try:
        with pytest.raises(
            ActiveToolProjectionError,
            match="exactly one parent relation",
        ):
            project_active_tools(
                headquarters.workspaces,
                headquarters.shadow,
                workspace.id,
            )
    finally:
        headquarters.close()


def test_active_tool_projection_requires_current_shadow(tmp_path: Path) -> None:
    headquarters = HeadquartersMemory.create(tmp_path / "hq", "Runtime Artist")
    song = headquarters.store.create_song("Runtime Song")
    runtime = HostRuntimeIdentity.from_runtime_labels(
        host_family="ABLETON_LIVE",
        version="12.1",
        edition="Suite",
        os_name="Darwin",
        machine="arm64",
    )
    workspace = headquarters.workspaces.create(
        song.id,
        runtime=runtime,
        location_ref="runtime-test:empty",
    )
    try:
        with pytest.raises(
            ActiveToolProjectionError,
            match="CURRENT verified Host Shadow",
        ):
            project_active_tools(
                headquarters.workspaces,
                headquarters.shadow,
                workspace.id,
            )
    finally:
        headquarters.close()
