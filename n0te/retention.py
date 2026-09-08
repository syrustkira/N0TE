from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from .activity import ActivityLog
from .activity_timeline import SongActivityItem, SongActivityTimeline
from .context import ContextIsolationService
from .evidence import EvidenceClaim, EvidenceMemory
from .friction import FrictionMemory
from .learning import LearningMemory
from .lineage import LineageStore, NotFoundError, ValidationError
from .session import SessionItem, SessionMemory
from .skills import SkillMemory
from .success import SuccessMemory
from .success_patterns import SongSuccessPatterns, SuccessPatternView

RETENTION_SECTIONS = {
    "DURABLE_FACTS",
    "IMPORTED_CONTEXT",
    "SESSIONS",
    "LEARNING",
    "SUCCESS",
    "FRICTION",
    "SKILLS",
    "ACTIVITY",
}

_EVIDENCE_SOURCE_LABELS = {
    "USER_DECLARED": "artist-reported",
    "OBSERVED": "observed in real work",
    "MEASURED": "measured",
    "PROVIDER_VERIFIED": "provider-verified",
    "REMEMBERED": "remembered",
    "INFERRED": "inferred",
}

_SKILL_SOURCE_LABELS = {
    "ARTIST_DECLARED": "artist-declared",
    "ARTIST_CORRECTION": "artist-corrected",
    "N0TE_ASSESSED": "N0TE-assessed",
    "OBSERVED": "observed in real work",
}


@dataclass(frozen=True)
class RetainedFact:
    scope: str
    key: str
    value: Any
    source_label: str
    confidence: float
    twin_domain: str


@dataclass(frozen=True)
class RetainedImport:
    source_kind: str
    payload: Any


@dataclass(frozen=True)
class RetainedSessionItem:
    kind: str
    body: str


@dataclass(frozen=True)
class RetainedSession:
    sequence: int
    objective: str
    state: str
    debrief_summary: str | None
    next_action: str | None
    items: tuple[RetainedSessionItem, ...]


@dataclass(frozen=True)
class RetainedObservation:
    observation: str
    source_label: str
    confidence: float
    conditions: tuple[str, ...]
    confounders: tuple[str, ...]


@dataclass(frozen=True)
class RetainedLearning:
    sequence: int
    domain: str
    subject: str
    change: str
    observations: tuple[RetainedObservation, ...]
    decision: str | None
    rationale: str | None
    decision_confidence: float | None


@dataclass(frozen=True)
class RetainedFriction:
    key: str
    description: str
    source_label: str
    confidence: float
    prevention_hint: str | None
    recurring_session_count: int


@dataclass(frozen=True)
class RetainedSkill:
    skill_id: str
    level: str
    source_label: str | None
    confidence: float | None
    assistance_level: float | None
    note: str | None


@dataclass(frozen=True)
class SongRetentionBrief:
    song_title: str
    current_version: str | None
    approved_version: str | None
    durable_facts: tuple[RetainedFact, ...]
    imported_context: tuple[RetainedImport, ...]
    sessions: tuple[RetainedSession, ...]
    learning: tuple[RetainedLearning, ...]
    success_patterns: tuple[SuccessPatternView, ...]
    friction: tuple[RetainedFriction, ...]
    skills: tuple[RetainedSkill, ...]
    activity: tuple[SongActivityItem, ...]

    @property
    def latest_session(self) -> RetainedSession | None:
        return None if not self.sessions else self.sessions[-1]

    @property
    def next_action(self) -> str | None:
        latest = self.latest_session
        return None if latest is None else latest.next_action


class SongRetentionService:
    """Consult existing canonical memory without creating a second memory store.

    Retention is a read/composition boundary. It keeps immutable history, active
    supersedable Evidence claims, Session continuity, explicit Learning decisions,
    association-only Success summaries, explicit Friction evidence, Skill state,
    imported evidence-only context and Activity chronology distinct rather than
    flattening them into one supposedly authoritative belief database.

    The returned context packet intentionally omits internal object IDs and source
    references. It is suitable for consumer presentation or future coproducer
    retrieval, but it grants no action authority and performs no automatic memory
    promotion, inference, mutation or recommendation.
    """

    def __init__(
        self,
        store: LineageStore,
        evidence: EvidenceMemory,
        context: ContextIsolationService,
        sessions: SessionMemory,
        learning: LearningMemory,
        success: SuccessMemory,
        friction: FrictionMemory,
        skills: SkillMemory,
        activity: ActivityLog,
    ) -> None:
        if not isinstance(store, LineageStore):
            raise TypeError("SongRetentionService requires LineageStore")
        components = (
            evidence,
            context,
            sessions,
            learning,
            friction,
            skills,
            activity,
        )
        for component in components:
            if getattr(component, "store", None) is not store:
                raise TypeError("retention components must share one canonical LineageStore")
        if not isinstance(success, SuccessMemory) or success.learning is not learning:
            raise TypeError("SongRetentionService requires SuccessMemory for canonical LearningMemory")
        self.store = store
        self.evidence = evidence
        self.context = context
        self.sessions = sessions
        self.learning = learning
        self.success = success
        self.friction = friction
        self.skills = skills
        self.activity = activity

    @staticmethod
    def _source_label(source_kind: str) -> str:
        try:
            return _EVIDENCE_SOURCE_LABELS[source_kind]
        except KeyError as exc:
            raise RuntimeError(
                "evidence source semantics changed; retention stopped safely"
            ) from exc

    @staticmethod
    def _fact(claim: EvidenceClaim) -> RetainedFact:
        try:
            source_label = _EVIDENCE_SOURCE_LABELS[claim.source_kind]
        except KeyError as exc:
            raise RuntimeError(
                "evidence source semantics changed; retention stopped safely"
            ) from exc
        return RetainedFact(
            scope=claim.scope_kind,
            key=claim.key,
            value=claim.value,
            source_label=source_label,
            confidence=claim.confidence,
            twin_domain=claim.twin_domain,
        )

    def _durable_facts(self, song_id: str) -> tuple[RetainedFact, ...]:
        song = self.store.get_song(song_id)
        assert song is not None
        claims: list[EvidenceClaim] = []
        claims.extend(self.evidence.active_claims_for_scope("ARTIST", song.artist_id))
        claims.extend(self.evidence.active_claims_for_scope("SONG", song.id))
        if song.current_version_id is not None:
            claims.extend(
                self.evidence.active_claims_for_scope("VERSION", song.current_version_id)
            )
        return tuple(self._fact(claim) for claim in claims)

    def _sessions(self, song_id: str) -> tuple[RetainedSession, ...]:
        rows = self.store._conn.execute(
            "SELECT id FROM sessions WHERE song_id=? ORDER BY seq",
            (song_id,),
        ).fetchall()
        retained: list[RetainedSession] = []
        for row in rows:
            session = self.sessions.get_session(str(row["id"]))
            if session is None:
                raise RuntimeError("canonical Session disappeared during retention read")
            items = self.sessions.items_for_session(session.id)
            retained.append(
                RetainedSession(
                    sequence=session.sequence,
                    objective=session.objective,
                    state=session.state,
                    debrief_summary=session.debrief_summary,
                    next_action=session.next_action,
                    items=tuple(
                        RetainedSessionItem(kind=item.kind, body=item.body)
                        for item in items
                    ),
                )
            )
        return tuple(retained)

    def _learning(self, song_id: str) -> tuple[RetainedLearning, ...]:
        retained: list[RetainedLearning] = []
        for episode in self.learning.episodes_for_song(song_id):
            decision = episode.decision
            retained.append(
                RetainedLearning(
                    sequence=episode.sequence,
                    domain=episode.domain,
                    subject=episode.subject_ref,
                    change=episode.change_description,
                    observations=tuple(
                        RetainedObservation(
                            observation=item.observation,
                            source_label=self._source_label(item.source_kind),
                            confidence=item.confidence,
                            conditions=item.conditions,
                            confounders=item.confounders,
                        )
                        for item in episode.consequences
                    ),
                    decision=None if decision is None else decision.decision,
                    rationale=None if decision is None else decision.rationale,
                    decision_confidence=(
                        None if decision is None else decision.confidence
                    ),
                )
            )
        return tuple(retained)

    def _friction(self, song_id: str) -> tuple[RetainedFriction, ...]:
        recurring = {
            pattern.friction_key: pattern.session_count
            for pattern in self.friction.recurring_patterns(
                min_sessions=2,
                song_id=song_id,
            )
        }
        return tuple(
            RetainedFriction(
                key=item.friction_key,
                description=item.description,
                source_label=self._source_label(item.source_kind),
                confidence=item.confidence,
                prevention_hint=item.prevention_hint,
                recurring_session_count=recurring.get(item.friction_key, 0),
            )
            for item in self.friction.observations(song_id=song_id)
        )

    def _skills(self) -> tuple[RetainedSkill, ...]:
        rows = self.store._conn.execute(
            "SELECT DISTINCT skill_id FROM skill_assessments ORDER BY skill_id"
        ).fetchall()
        retained: list[RetainedSkill] = []
        for row in rows:
            state = self.skills.state(str(row["skill_id"]))
            latest = state.latest_assessment
            if latest is None:
                retained.append(
                    RetainedSkill(state.skill_id, state.level, None, None, None, None)
                )
                continue
            try:
                source_label = _SKILL_SOURCE_LABELS[latest.source_kind]
            except KeyError as exc:
                raise RuntimeError(
                    "Skill source semantics changed; retention stopped safely"
                ) from exc
            retained.append(
                RetainedSkill(
                    skill_id=state.skill_id,
                    level=state.level,
                    source_label=source_label,
                    confidence=latest.confidence,
                    assistance_level=latest.assistance_level,
                    note=latest.note,
                )
            )
        return tuple(retained)

    @staticmethod
    def _version_label(store: LineageStore, version_id: str | None) -> str | None:
        if version_id is None:
            return None
        version = store.get_version(version_id)
        return None if version is None else f"Version {version.ordinal}: {version.label}"

    def brief_for_song(self, song_id: str) -> SongRetentionBrief:
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(
                f"Song not found in profile {self.store.profile_id}: {song_id}"
            )
        imports = self.context.imports_for(song_id=song.id)
        success_patterns = SongSuccessPatterns(self.store, self.success).for_song(song.id)
        activity = SongActivityTimeline(self.store, self.activity).for_song(
            song.id,
            newest_first=False,
        )
        return SongRetentionBrief(
            song_title=song.title,
            current_version=self._version_label(self.store, song.current_version_id),
            approved_version=self._version_label(self.store, song.approved_version_id),
            durable_facts=self._durable_facts(song.id),
            imported_context=tuple(
                RetainedImport(source_kind=item.source_kind, payload=item.payload)
                for item in imports
            ),
            sessions=self._sessions(song.id),
            learning=self._learning(song.id),
            success_patterns=success_patterns,
            friction=self._friction(song.id),
            skills=self._skills(),
            activity=activity,
        )

    def for_active_song(self) -> SongRetentionBrief | None:
        song = self.store.active_song()
        return None if song is None else self.brief_for_song(song.id)

    @staticmethod
    def _normalize_sections(sections: Iterable[str] | None) -> tuple[str, ...]:
        if sections is None:
            return tuple(sorted(RETENTION_SECTIONS))
        normalized = tuple(dict.fromkeys(str(item).strip().upper() for item in sections))
        invalid = tuple(item for item in normalized if item not in RETENTION_SECTIONS)
        if invalid:
            raise ValidationError(f"unsupported retention sections: {', '.join(invalid)}")
        return normalized

    def context_packet_for_song(
        self,
        song_id: str,
        *,
        sections: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = self._normalize_sections(sections)
        brief = self.brief_for_song(song_id)
        packet: dict[str, Any] = {
            "schema": "n0te.song-retention.v1",
            "song": {
                "title": brief.song_title,
                "current_version": brief.current_version,
                "approved_version": brief.approved_version,
            },
            "retention_policy": {
                "history": "append-only where canonical ledgers define it",
                "durable_beliefs": "active supersedable evidence only",
                "causality": "association only unless independently proven",
                "authority": "read-only; grants no action authority",
                "automatic_promotion": False,
            },
        }
        if "DURABLE_FACTS" in selected:
            packet["durable_facts"] = [
                {
                    "scope": item.scope,
                    "key": item.key,
                    "value": item.value,
                    "source": item.source_label,
                    "confidence": item.confidence,
                    "twin_domain": item.twin_domain,
                }
                for item in brief.durable_facts
            ]
        if "IMPORTED_CONTEXT" in selected:
            packet["imported_context"] = [
                {"source_kind": item.source_kind, "payload": item.payload}
                for item in brief.imported_context
            ]
        if "SESSIONS" in selected:
            packet["sessions"] = [
                {
                    "sequence": item.sequence,
                    "objective": item.objective,
                    "state": item.state,
                    "debrief": item.debrief_summary,
                    "next_action": item.next_action,
                    "items": [
                        {"kind": note.kind, "body": note.body} for note in item.items
                    ],
                }
                for item in brief.sessions
            ]
        if "LEARNING" in selected:
            packet["learning"] = [
                {
                    "sequence": item.sequence,
                    "domain": item.domain,
                    "subject": item.subject,
                    "change": item.change,
                    "observations": [
                        {
                            "observation": observation.observation,
                            "source": observation.source_label,
                            "confidence": observation.confidence,
                            "conditions": list(observation.conditions),
                            "confounders": list(observation.confounders),
                        }
                        for observation in item.observations
                    ],
                    "decision": item.decision,
                    "rationale": item.rationale,
                    "decision_confidence": item.decision_confidence,
                }
                for item in brief.learning
            ]
        if "SUCCESS" in selected:
            packet["success_patterns"] = [
                {
                    "domain": item.domain,
                    "subject": item.subject,
                    "change": item.change,
                    "causal_status": item.causal_status,
                    "humility_state": item.humility_state,
                    "warning": item.warning,
                    "completed_count": item.completed_count,
                    "pending_count": item.pending_count,
                    "keep_count": item.keep_count,
                    "revert_count": item.revert_count,
                    "revise_count": item.revise_count,
                    "inconclusive_count": item.inconclusive_count,
                }
                for item in brief.success_patterns
            ]
        if "FRICTION" in selected:
            packet["friction"] = [
                {
                    "key": item.key,
                    "description": item.description,
                    "source": item.source_label,
                    "confidence": item.confidence,
                    "prevention_hint": item.prevention_hint,
                    "recurring_session_count": item.recurring_session_count,
                }
                for item in brief.friction
            ]
        if "SKILLS" in selected:
            packet["skills"] = [
                {
                    "skill": item.skill_id,
                    "level": item.level,
                    "source": item.source_label,
                    "confidence": item.confidence,
                    "assistance_level": item.assistance_level,
                    "note": item.note,
                }
                for item in brief.skills
            ]
        if "ACTIVITY" in selected:
            packet["activity"] = [
                {
                    "sequence": item.sequence,
                    "summary": item.summary,
                    "detail": item.detail,
                }
                for item in brief.activity
            ]
        json.dumps(packet, sort_keys=True, allow_nan=False)
        return packet
