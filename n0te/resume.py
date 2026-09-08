from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceClaim
from .lineage import LineageCorruptionError, NotFoundError, Version
from .memory import HeadquartersMemory


@dataclass(frozen=True)
class ResumeVersion:
    id: str
    ordinal: int
    label: str


@dataclass(frozen=True)
class ResumeEvidence:
    claim_id: str
    value: Any
    source_kind: str
    source_ref: str | None
    confidence: float


@dataclass(frozen=True)
class ResumeConflict:
    key: str
    scope_kind: str
    scope_id: str
    evidence: tuple[ResumeEvidence, ...]


@dataclass(frozen=True)
class ResumeChange:
    sequence: int
    event_type: str
    summary: str
    object_type: str
    object_id: str
    version_id: str | None


@dataclass(frozen=True)
class SongResumeBrief:
    artist_name: str
    song_id: str
    song_title: str
    is_active_song: bool
    current_version: ResumeVersion | None
    approved_version: ResumeVersion | None
    recent_changes: tuple[ResumeChange, ...]
    unresolved_conflicts: tuple[ResumeConflict, ...]
    next_action_status: str
    next_action: Any | None
    next_action_evidence: tuple[ResumeEvidence, ...]


_EVENT_SUMMARIES = {
    "SONG_CREATED": "Song created",
    "SONG_SELECTED": "Song selected",
    "ASSET_ATTACHED": "Asset attached",
    "VERSION_CREATED": "Version created",
    "CURRENT_VERSION_CHANGED": "Current version changed",
    "VERSION_APPROVED": "Version approved",
    "EVIDENCE_CLAIM_RECORDED": "Evidence recorded",
    "EVIDENCE_SUPERSESSION_LINKED": "Evidence reconciled",
}


class SongResumeService:
    """Pure read model over canonical Headquarters memory.

    The service owns no persistence, records no Activity and never resolves a
    conflict by ranking sources. It reports only already represented truth.
    """

    def __init__(self, memory: HeadquartersMemory):
        if not isinstance(memory, HeadquartersMemory):
            raise TypeError("SongResumeService requires HeadquartersMemory")
        self.memory = memory
        self.store = memory.store
        self._conn = memory.store._conn

    @staticmethod
    def _resume_version(version: Version | None) -> ResumeVersion | None:
        if version is None:
            return None
        return ResumeVersion(id=version.id, ordinal=version.ordinal, label=version.label)

    @staticmethod
    def _resume_evidence(claim: EvidenceClaim) -> ResumeEvidence:
        return ResumeEvidence(
            claim_id=claim.id,
            value=claim.value,
            source_kind=claim.source_kind,
            source_ref=claim.source_ref,
            confidence=claim.confidence,
        )

    def _version(self, version_id: str | None, song_id: str) -> Version | None:
        if version_id is None:
            return None
        version = self.store.get_version(version_id)
        if version is None or version.song_id != song_id:
            raise LineageCorruptionError("Song points at a missing or cross-Song version")
        return version

    def _recent_changes(self, song_id: str, limit: int) -> tuple[ResumeChange, ...]:
        if int(limit) <= 0:
            raise ValueError("recent_limit must be > 0")
        rows = self._conn.execute(
            "SELECT seq,event_type,object_type,object_id,version_id "
            "FROM activity_events WHERE song_id=? ORDER BY seq DESC LIMIT ?",
            (song_id, int(limit)),
        ).fetchall()
        changes = []
        for row in reversed(rows):
            event_type = str(row["event_type"])
            changes.append(
                ResumeChange(
                    sequence=int(row["seq"]),
                    event_type=event_type,
                    summary=_EVENT_SUMMARIES.get(
                        event_type, event_type.replace("_", " ").strip().title()
                    ),
                    object_type=str(row["object_type"]),
                    object_id=str(row["object_id"]),
                    version_id=None if row["version_id"] is None else str(row["version_id"]),
                )
            )
        return tuple(changes)

    def _applicable_keys(self, song_id: str, artist_id: str, version_id: str | None) -> tuple[str, ...]:
        scopes = [("SONG", song_id), ("ARTIST", artist_id), ("PROFILE", self.store.profile_id)]
        if version_id is not None:
            scopes.insert(0, ("VERSION", version_id))
        predicates = " OR ".join("(c.scope_kind=? AND c.scope_id=?)" for _ in scopes)
        params = [value for pair in scopes for value in pair]
        rows = self._conn.execute(
            "SELECT DISTINCT c.key FROM evidence_claims c "
            "WHERE NOT EXISTS (SELECT 1 FROM evidence_supersessions s WHERE s.old_claim_id=c.id) "
            f"AND ({predicates}) ORDER BY c.key",
            params,
        ).fetchall()
        return tuple(str(row["key"]) for row in rows)

    def _conflicts(self, song_id: str, artist_id: str, version_id: str | None) -> tuple[ResumeConflict, ...]:
        conflicts = []
        for key in self._applicable_keys(song_id, artist_id, version_id):
            resolution = self.memory.evidence.resolve_for_song(
                song_id=song_id, key=key, version_id=version_id
            )
            if resolution.status != "CONFLICT":
                continue
            if resolution.scope_kind is None or resolution.scope_id is None:
                raise LineageCorruptionError("conflict is missing its applicable evidence scope")
            conflicts.append(
                ResumeConflict(
                    key=key,
                    scope_kind=resolution.scope_kind,
                    scope_id=resolution.scope_id,
                    evidence=tuple(self._resume_evidence(claim) for claim in resolution.claims),
                )
            )
        return tuple(conflicts)

    def brief(self, song_id: str | None = None, *, recent_limit: int = 20) -> SongResumeBrief:
        if song_id is None:
            song = self.store.active_song()
            if song is None:
                raise NotFoundError("there is no active Song to resume")
        else:
            song = self.store.get_song(song_id)
            if song is None:
                raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")

        current = self._version(song.current_version_id, song.id)
        approved = self._version(song.approved_version_id, song.id)
        current_id = None if current is None else current.id
        conflicts = self._conflicts(song.id, song.artist_id, current_id)
        next_action_resolution = self.memory.evidence.resolve_for_song(
            song_id=song.id, key="next.action", version_id=current_id
        )
        next_action = (
            next_action_resolution.value
            if next_action_resolution.status == "RESOLVED"
            else None
        )
        next_evidence = tuple(
            self._resume_evidence(claim) for claim in next_action_resolution.claims
        )
        active = self.store.active_song()
        return SongResumeBrief(
            artist_name=self.store.artist().display_name,
            song_id=song.id,
            song_title=song.title,
            is_active_song=active is not None and active.id == song.id,
            current_version=self._resume_version(current),
            approved_version=self._resume_version(approved),
            recent_changes=self._recent_changes(song.id, recent_limit),
            unresolved_conflicts=conflicts,
            next_action_status=next_action_resolution.status,
            next_action=next_action,
            next_action_evidence=next_evidence,
        )
