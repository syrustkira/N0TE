from __future__ import annotations

from dataclasses import dataclass

from .lineage import LineageCorruptionError, LineageStore, Song, ValidationError, Version


class VersionApprovalError(RuntimeError):
    """An exact Song Version approval can no longer be applied safely."""


class StaleVersionApprovalError(VersionApprovalError):
    """The Song/current/approved state moved after approval authority was rendered."""


@dataclass(frozen=True)
class VersionApprovalBinding:
    song_id: str
    target_version_id: str
    expected_current_version_id: str | None
    expected_approved_version_id: str | None


@dataclass(frozen=True)
class VersionApprovalResult:
    song: Song
    approved_version: Version


class VersionApprovalService:
    """Apply one exact approval against the Song state the artist actually saw.

    This service owns no approval storage and no browser authority. It performs
    stale-state validation and the canonical approved_version_id update in one
    immediate SQLite transaction. Existing lineage triggers enforce same-Song
    validity and Activity records the resulting VERSION_APPROVED chronology event.
    """

    def __init__(self, store: LineageStore):
        if not isinstance(store, LineageStore):
            raise TypeError("VersionApprovalService requires the canonical LineageStore")
        self.store = store

    def binding_for(self, song_id: str, target_version_id: str) -> VersionApprovalBinding:
        song = self.store._require_song(song_id)
        target = self.store.get_version(target_version_id)
        if target is None or target.song_id != song.id:
            raise ValidationError("approval target belongs to a different Song")
        return VersionApprovalBinding(
            song_id=song.id,
            target_version_id=target.id,
            expected_current_version_id=song.current_version_id,
            expected_approved_version_id=song.approved_version_id,
        )

    def approve(self, binding: VersionApprovalBinding) -> VersionApprovalResult:
        if not isinstance(binding, VersionApprovalBinding):
            raise TypeError("binding must be VersionApprovalBinding")

        with self.store._tx():
            active_row = self.store._conn.execute(
                "SELECT value FROM metadata WHERE key = 'active_song_id'"
            ).fetchone()
            if active_row is None:
                raise LineageCorruptionError("active Song metadata disappeared")
            if str(active_row["value"]) != binding.song_id:
                raise StaleVersionApprovalError("active Song changed after approval was prepared")

            song_row = self.store._conn.execute(
                "SELECT id, artist_id, title, current_version_id, approved_version_id "
                "FROM songs WHERE id = ?",
                (binding.song_id,),
            ).fetchone()
            if song_row is None:
                raise StaleVersionApprovalError("active Song disappeared after approval was prepared")
            if song_row["current_version_id"] != binding.expected_current_version_id:
                raise StaleVersionApprovalError("current Version changed after approval was prepared")
            if song_row["approved_version_id"] != binding.expected_approved_version_id:
                raise StaleVersionApprovalError("approved Version changed after approval was prepared")

            target_row = self.store._conn.execute(
                "SELECT id, song_id, ordinal, label, parent_version_id "
                "FROM versions WHERE id = ?",
                (binding.target_version_id,),
            ).fetchone()
            if target_row is None or target_row["song_id"] != binding.song_id:
                raise VersionApprovalError("approval target is not a Version of the active Song")
            if song_row["approved_version_id"] == binding.target_version_id:
                raise StaleVersionApprovalError("that exact Version is already approved")

            changed = self.store._conn.execute(
                "UPDATE songs SET approved_version_id = ? "
                "WHERE id = ? AND current_version_id IS ? AND approved_version_id IS ?",
                (
                    binding.target_version_id,
                    binding.song_id,
                    binding.expected_current_version_id,
                    binding.expected_approved_version_id,
                ),
            )
            if changed.rowcount != 1:
                raise StaleVersionApprovalError("Song Version state changed before approval committed")

        approved_song = self.store.get_song(binding.song_id)
        target = self.store.get_version(binding.target_version_id)
        if approved_song is None or target is None:
            raise LineageCorruptionError("approved Version lineage disappeared after commit")
        if approved_song.current_version_id != binding.expected_current_version_id:
            raise VersionApprovalError("approval unexpectedly changed the current Version")
        if approved_song.approved_version_id != target.id:
            raise VersionApprovalError("canonical approval pointer did not match the target Version")
        return VersionApprovalResult(song=approved_song, approved_version=target)
