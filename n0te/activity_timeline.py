from __future__ import annotations

from dataclasses import dataclass

from .activity import ActivityEvent, ActivityLog
from .lineage import LineageStore, NotFoundError


@dataclass(frozen=True)
class SongActivityItem:
    """One artist-readable, read-only projection of canonical Activity history."""

    sequence: int
    summary: str
    detail: str | None = None


class SongActivityTimeline:
    """Translate canonical Song Activity into safe artist-facing chronology.

    Activity sequence is the only temporal fact available in schema v1. This
    projection therefore never manufactures timestamps, elapsed time or recency.
    Unknown future event types remain visible as generic recorded activity rather
    than leaking internal event names, object IDs or payload JSON.
    """

    def __init__(self, store: LineageStore, activity: ActivityLog):
        if not isinstance(store, LineageStore):
            raise TypeError("SongActivityTimeline requires LineageStore")
        if not isinstance(activity, ActivityLog) or activity.store is not store:
            raise TypeError("SongActivityTimeline requires ActivityLog for the same LineageStore")
        self.store = store
        self.activity = activity

    def for_song(self, song_id: str, *, newest_first: bool = True) -> tuple[SongActivityItem, ...]:
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        events = self.activity.for_song(song.id)
        items = tuple(self._item(event) for event in events)
        return tuple(reversed(items)) if newest_first else items

    def _version_name(self, event: ActivityEvent) -> str | None:
        if event.version_id is None:
            return None
        version = self.store.get_version(event.version_id)
        if version is None or version.song_id != event.song_id:
            return None
        return f"Version {version.ordinal}: {version.label}"

    def _asset_name(self, event: ActivityEvent) -> str | None:
        if event.object_type != "ASSET":
            return None
        asset = self.store.get_asset(event.object_id)
        if asset is None or asset.song_id != event.song_id:
            return None
        return asset.name

    @staticmethod
    def _payload_text(event: ActivityEvent, key: str) -> str | None:
        if not isinstance(event.payload, dict):
            return None
        value = event.payload.get(key)
        return value if isinstance(value, str) and value.strip() else None

    def _item(self, event: ActivityEvent) -> SongActivityItem:
        version = self._version_name(event)
        kind = event.event_type

        if kind == "SONG_CREATED":
            return SongActivityItem(event.sequence, "Song started")
        if kind == "SONG_SELECTED":
            return SongActivityItem(event.sequence, "Song made active")
        if kind == "ASSET_ATTACHED":
            name = self._asset_name(event)
            return SongActivityItem(event.sequence, "Song material added", name)
        if kind == "VERSION_CREATED":
            return SongActivityItem(event.sequence, "Version preserved", version)
        if kind == "CURRENT_VERSION_CHANGED":
            return SongActivityItem(event.sequence, "Current Version changed", version)
        if kind == "VERSION_APPROVED":
            return SongActivityItem(event.sequence, "Version approved", version)
        if kind == "SESSION_STARTED":
            return SongActivityItem(event.sequence, "Work Session started", version)
        if kind == "SESSION_SCRATCH_ADDED":
            scratch_kind = self._payload_text(event, "kind")
            labels = {
                "OBSERVATION": "Observation captured",
                "DECISION": "Decision captured",
                "REJECTED_IDEA": "Rejected idea captured",
                "UNRESOLVED": "Unresolved question captured",
                "MARK": "MARK captured",
            }
            return SongActivityItem(event.sequence, labels.get(scratch_kind, "Session note captured"), version)
        if kind == "SESSION_CLOSED":
            return SongActivityItem(event.sequence, "Work Session finished", version)
        if kind == "SESSION_ITEM_PROMOTED":
            return SongActivityItem(event.sequence, "Session learning promoted", version)
        if kind == "EVIDENCE_CLAIM_RECORDED":
            return SongActivityItem(event.sequence, "Song evidence recorded", version)
        if kind == "EVIDENCE_SUPERSESSION_LINKED":
            return SongActivityItem(event.sequence, "Song evidence updated", version)
        if kind == "LEARNING_EPISODE_STARTED":
            return SongActivityItem(event.sequence, "Learning episode started", version)
        if kind == "LEARNING_CONSEQUENCE_RECORDED":
            return SongActivityItem(event.sequence, "Learning consequence recorded", version)
        if kind == "LEARNING_DECISION_RECORDED":
            decision = self._payload_text(event, "decision")
            label = {
                "KEEP": "Keep",
                "REVERT": "Revert",
                "REVISE": "Revise",
                "INCONCLUSIVE": "Inconclusive",
            }.get(decision)
            return SongActivityItem(event.sequence, "Learning decision recorded", label)
        if kind == "SKILL_ASSESSED":
            return SongActivityItem(event.sequence, "Skill assessment recorded", version)
        if kind == "FOCUS_SESSION_STARTED":
            mode = self._payload_text(event, "mode")
            return SongActivityItem(event.sequence, "Focus started", None if mode is None else mode.title())
        if kind == "FOCUS_SESSION_ENDED":
            mode = self._payload_text(event, "mode")
            return SongActivityItem(event.sequence, "Focus ended", None if mode is None else mode.title())

        return SongActivityItem(event.sequence, "Activity recorded")
