from __future__ import annotations

import pytest

from n0te.host_session_handshake import (
    HostSessionHandshake,
    HostSessionHandshakeError,
)
from n0te.hosts import HostRuntimeIdentity
from n0te.memory import HeadquartersMemory


def _runtime(
    *,
    family: str = "ABLETON_LIVE",
    version: str = "12.1",
) -> HostRuntimeIdentity:
    return HostRuntimeIdentity.from_runtime_labels(
        host_family=family,
        version=version,
        edition="Standard",
        os_name="Darwin",
        machine="arm64",
    )


def test_first_unknown_project_attaches_to_explicitly_active_song(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        song = headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)

        result = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/active-song.als",
            display_name="Active Song.als",
            state_fingerprint="state:1",
        )

        assert result.status == "CREATED"
        assert result.binding.song_id == song.id
        assert result.workspace.workspace.song_id == song.id
        assert result.workspace.workspace.host_family == "ABLETON_LIVE"
        assert result.workspace.current_observation.location_ref == (
            "file:///projects/active-song.als"
        )
        assert result.binding.workspace_observation_id == (
            result.workspace.current_observation.id
        )
    finally:
        headquarters.close()


def test_exact_project_reuses_workspace_without_appending_history(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/active-song.als",
            display_name="Active Song.als",
            state_fingerprint="state:1",
        )
        second = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/active-song.als",
            display_name="Active Song.als",
            state_fingerprint="state:1",
        )

        assert second.status == "REUSED"
        assert second.binding.workspace_id == first.binding.workspace_id
        assert second.binding.workspace_observation_id == (
            first.binding.workspace_observation_id
        )
        assert len(headquarters.workspaces.history(first.binding.workspace_id)) == 1
    finally:
        headquarters.close()


def test_known_workspace_reconciles_project_move_without_new_identity(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/original.als",
            display_name="Song.als",
            state_fingerprint="state:1",
        )
        moved = handshake.attach(
            runtime=_runtime(),
            known_workspace_id=first.binding.workspace_id,
            location_ref="file:///archive/song.als",
        )

        assert moved.status == "RECONCILED"
        assert moved.binding.workspace_id == first.binding.workspace_id
        assert moved.binding.workspace_observation_id != (
            first.binding.workspace_observation_id
        )
        assert moved.workspace.current_observation.location_ref == (
            "file:///archive/song.als"
        )
        assert moved.workspace.current_observation.display_name == "Song.als"
        assert moved.workspace.current_observation.state_fingerprint == "state:1"
        assert len(headquarters.workspaces.history(first.binding.workspace_id)) == 2
    finally:
        headquarters.close()


def test_runtime_change_reconciles_same_workspace_and_preserves_optional_metadata(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(version="12.1"),
            location_ref="file:///projects/song.als",
            display_name="Song.als",
            state_fingerprint="state:1",
        )
        updated = handshake.attach(
            runtime=_runtime(version="12.2"),
            known_workspace_id=first.binding.workspace_id,
            location_ref="file:///projects/song.als",
        )

        assert updated.status == "RECONCILED"
        assert updated.binding.workspace_id == first.binding.workspace_id
        assert updated.binding.runtime.version == "12.2"
        assert updated.workspace.current_observation.display_name == "Song.als"
        assert updated.workspace.current_observation.state_fingerprint == "state:1"
    finally:
        headquarters.close()


def test_location_owned_by_another_song_fails_instead_of_rebinding(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        song_a = headquarters.store.create_song("Song A")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/shared.als",
        )
        assert first.binding.song_id == song_a.id

        song_b = headquarters.store.create_song("Song B")
        assert headquarters.store.active_song().id == song_b.id
        history_before = headquarters.workspaces.history(first.binding.workspace_id)

        with pytest.raises(HostSessionHandshakeError, match="different Song"):
            handshake.attach(
                runtime=_runtime(),
                location_ref="file:///projects/shared.als",
            )

        assert headquarters.workspaces.history(first.binding.workspace_id) == history_before
        assert headquarters.workspaces.state(first.binding.workspace_id).workspace.song_id == (
            song_a.id
        )
    finally:
        headquarters.close()


def test_known_workspace_cannot_move_onto_another_workspace_location(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/one.als",
        )
        second = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/two.als",
        )

        with pytest.raises(HostSessionHandshakeError, match="another workspace"):
            handshake.attach(
                runtime=_runtime(),
                known_workspace_id=first.binding.workspace_id,
                location_ref="file:///projects/two.als",
            )

        assert headquarters.workspaces.state(first.binding.workspace_id).current_observation.location_ref == (
            "file:///projects/one.als"
        )
        assert headquarters.workspaces.state(second.binding.workspace_id).current_observation.location_ref == (
            "file:///projects/two.als"
        )
    finally:
        headquarters.close()


def test_host_family_change_fails_closed(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        headquarters.store.create_song("Active Song")
        handshake = HostSessionHandshake(headquarters.host_observation)
        first = handshake.attach(
            runtime=_runtime(),
            location_ref="file:///projects/song.als",
        )
        history_before = headquarters.workspaces.history(first.binding.workspace_id)

        with pytest.raises(HostSessionHandshakeError, match="host family"):
            handshake.attach(
                runtime=_runtime(family="FL_STUDIO"),
                known_workspace_id=first.binding.workspace_id,
                location_ref="file:///projects/song.flp",
            )

        assert headquarters.workspaces.history(first.binding.workspace_id) == history_before
    finally:
        headquarters.close()


def test_attachment_requires_active_song(tmp_path):
    headquarters = HeadquartersMemory.create(tmp_path, "Handshake Artist")
    try:
        handshake = HostSessionHandshake(headquarters.host_observation)
        with pytest.raises(HostSessionHandshakeError, match="active Song"):
            handshake.attach(
                runtime=_runtime(),
                location_ref="file:///projects/unbound.als",
            )
        assert headquarters.workspaces.current_candidates_at_location(
            "file:///projects/unbound.als"
        ) == ()
    finally:
        headquarters.close()
