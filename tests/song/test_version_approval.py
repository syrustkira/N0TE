from pathlib import Path

import pytest

from n0te.memory import HeadquartersMemory
from n0te.version_approval import (
    StaleVersionApprovalError,
    VersionApprovalBinding,
    VersionApprovalError,
    VersionApprovalService,
)


def seed_versions(root: Path):
    headquarters = HeadquartersMemory.create(root, "Approval Artist")
    song = headquarters.store.create_song("Approval Song")
    versions = []
    parent = None
    for ordinal, label in enumerate(("Sketch", "Rough", "Mix"), start=1):
        asset = headquarters.store.attach_asset(
            song.id,
            name=f"take-{ordinal}.wav",
            sha256=f"{ordinal:x}" * 64,
            source_uri=f"file:///take-{ordinal}.wav",
        )
        version = headquarters.store.create_version(
            song.id,
            label=label,
            parent_version_id=parent,
            asset_ids=(asset.id,),
        )
        versions.append(version)
        parent = version.id
    return headquarters, song, tuple(versions)


def test_exact_approval_changes_only_approved_pointer_and_records_activity(tmp_path: Path) -> None:
    headquarters, song, versions = seed_versions(tmp_path / "data")
    try:
        service = VersionApprovalService(headquarters.store)
        current_before = headquarters.store.get_song(song.id).current_version_id
        assets_before = tuple(
            headquarters.store.version_asset_ids(version.id) for version in versions
        )
        activity_before = headquarters.activity.for_song(song.id)

        binding = service.binding_for(song.id, versions[0].id)
        result = service.approve(binding)

        assert result.song.approved_version_id == versions[0].id
        assert result.song.current_version_id == current_before == versions[-1].id
        assert tuple(
            headquarters.store.version_asset_ids(version.id) for version in versions
        ) == assets_before
        assert headquarters.store.versions_for_song(song.id) == versions

        activity_after = headquarters.activity.for_song(song.id)
        added = activity_after[len(activity_before):]
        assert [event.event_type for event in added] == ["VERSION_APPROVED"]
        assert added[0].song_id == song.id
        assert added[0].version_id == versions[0].id
    finally:
        headquarters.close()


def test_approval_binding_rejects_stale_current_approved_and_song(tmp_path: Path) -> None:
    headquarters, song, versions = seed_versions(tmp_path / "data")
    try:
        service = VersionApprovalService(headquarters.store)

        stale_current = service.binding_for(song.id, versions[0].id)
        headquarters.store.set_current_version(song.id, versions[1].id)
        with pytest.raises(StaleVersionApprovalError):
            service.approve(stale_current)
        assert headquarters.store.get_song(song.id).approved_version_id is None

        headquarters.store.set_current_version(song.id, versions[-1].id)
        stale_approved = service.binding_for(song.id, versions[0].id)
        headquarters.store.approve_version(song.id, versions[1].id)
        with pytest.raises(StaleVersionApprovalError):
            service.approve(stale_approved)
        assert headquarters.store.get_song(song.id).approved_version_id == versions[1].id

        stale_song = service.binding_for(song.id, versions[0].id)
        other = headquarters.store.create_song("Other Song")
        with pytest.raises(StaleVersionApprovalError):
            service.approve(stale_song)
        assert headquarters.store.get_song(song.id).approved_version_id == versions[1].id
        assert headquarters.store.get_song(other.id).approved_version_id is None
    finally:
        headquarters.close()


def test_approval_rejects_cross_song_target_inside_atomic_boundary(tmp_path: Path) -> None:
    headquarters, song, versions = seed_versions(tmp_path / "data")
    try:
        service = VersionApprovalService(headquarters.store)
        original = headquarters.store.get_song(song.id)
        assert original is not None
        other = headquarters.store.create_song("Other Song")
        other_asset = headquarters.store.attach_asset(
            other.id,
            name="other.wav",
            sha256="d" * 64,
        )
        other_version = headquarters.store.create_version(
            other.id,
            label="Other Version",
            asset_ids=(other_asset.id,),
        )
        headquarters.store.select_song(song.id)

        forged = VersionApprovalBinding(
            song_id=song.id,
            target_version_id=other_version.id,
            expected_current_version_id=original.current_version_id,
            expected_approved_version_id=original.approved_version_id,
        )
        with pytest.raises(VersionApprovalError):
            service.approve(forged)

        unchanged = headquarters.store.get_song(song.id)
        assert unchanged is not None
        assert unchanged.approved_version_id is None
        assert headquarters.store.get_song(other.id).approved_version_id is None
        assert headquarters.store.get_version(versions[0].id) is not None
    finally:
        headquarters.close()


def test_approval_is_restart_durable_and_can_intentionally_differ_from_current(tmp_path: Path) -> None:
    root = tmp_path / "data"
    headquarters, song, versions = seed_versions(root)
    profile_id = headquarters.store.profile_id
    try:
        service = VersionApprovalService(headquarters.store)
        binding = service.binding_for(song.id, versions[0].id)
        service.approve(binding)
        assert headquarters.store.get_song(song.id).current_version_id == versions[-1].id
    finally:
        headquarters.close()

    reopened = HeadquartersMemory.open(root, profile_id)
    try:
        durable = reopened.store.get_song(song.id)
        assert durable is not None
        assert durable.approved_version_id == versions[0].id
        assert durable.current_version_id == versions[-1].id
    finally:
        reopened.close()
