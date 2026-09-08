from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .friction import FrictionMemory, FrictionObservation, FrictionPattern
from .lineage import LineageCorruptionError, ValidationError

_CONFIDENCE_LEVELS = {"LOW": 0.4, "MEDIUM": 0.7, "HIGH": 1.0}
_SOURCE_LABELS = {
    "USER_DECLARED": "artist-reported",
    "OBSERVED": "observed in real work",
    "MEASURED": "measured",
    "PROVIDER_VERIFIED": "provider-verified",
    "REMEMBERED": "remembered",
    "INFERRED": "inferred",
}


class FrictionJourneyError(RuntimeError):
    """A consumer Friction Memory operation cannot be applied safely."""


class StaleFrictionJourneyError(FrictionJourneyError):
    """The active Song or bound Learning episode moved after authority was rendered."""


@dataclass(frozen=True)
class FrictionCaptureBinding:
    song_id: str
    episode_id: str


@dataclass(frozen=True)
class FrictionObservationView:
    key: str
    description: str
    source_label: str
    confidence: float
    prevention_hint: str | None


@dataclass(frozen=True)
class FrictionEpisodeView:
    episode_id: str
    domain: str
    subject: str
    change: str
    observations: tuple[FrictionObservationView, ...]


@dataclass(frozen=True)
class FrictionPatternView:
    key: str
    occurrence_count: int
    session_count: int
    occurrences: tuple[FrictionObservationView, ...]
    prevention_hints: tuple[str, ...]


class SongFrictionJourney:
    """Consumer-safe reachability for canonical FrictionMemory.

    This service owns no new persistence. Browser-authored evidence is always
    USER_DECLARED with a server-owned source reference. Capture authority binds the
    exact active Song and exact Learning episode the artist saw. Recurrence remains
    the canonical FrictionMemory rule: the same explicit key across at least two
    distinct real work Sessions.
    """

    def __init__(self, friction: FrictionMemory):
        if not isinstance(friction, FrictionMemory):
            raise TypeError("SongFrictionJourney requires canonical FrictionMemory")
        self.friction = friction
        self.learning = friction.learning
        self.store = friction.store

    @staticmethod
    def _text(value: str, field: str, *, maximum: int) -> str:
        text = " ".join(str(value).split())
        if not text:
            raise ValidationError(f"{field} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return text

    @staticmethod
    def _optional_text(value: str | None, field: str, *, maximum: int) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        if not text:
            return None
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return text

    @staticmethod
    def _confidence(value: str) -> float:
        key = str(value).strip().upper()
        if key not in _CONFIDENCE_LEVELS:
            raise ValidationError("unsupported confidence level")
        return _CONFIDENCE_LEVELS[key]

    @staticmethod
    def _source_label(source_kind: str) -> str:
        try:
            return _SOURCE_LABELS[source_kind]
        except KeyError as exc:
            raise FrictionJourneyError(
                "Friction evidence source semantics changed; consumer projection stopped safely"
            ) from exc

    @classmethod
    def _observation_view(cls, observation: FrictionObservation) -> FrictionObservationView:
        return FrictionObservationView(
            key=observation.friction_key,
            description=observation.description,
            source_label=cls._source_label(observation.source_kind),
            confidence=observation.confidence,
            prevention_hint=observation.prevention_hint,
        )

    @classmethod
    def _pattern_view(cls, pattern: FrictionPattern) -> FrictionPatternView:
        if pattern.session_count < 2:
            raise FrictionJourneyError(
                "Friction recurrence semantics changed; consumer projection stopped safely"
            )
        return FrictionPatternView(
            key=pattern.friction_key,
            occurrence_count=pattern.occurrence_count,
            session_count=pattern.session_count,
            occurrences=tuple(cls._observation_view(item) for item in pattern.occurrences),
            prevention_hints=pattern.prevention_hints,
        )

    def _active_song_id(self) -> str | None:
        song = self.store.active_song()
        return None if song is None else song.id

    def episodes_for_active_song(self) -> tuple[FrictionEpisodeView, ...]:
        song_id = self._active_song_id()
        if song_id is None:
            return ()
        observations = self.friction.observations(song_id=song_id)
        grouped: dict[str, list[FrictionObservation]] = {}
        for observation in observations:
            grouped.setdefault(observation.episode_id, []).append(observation)
        views: list[FrictionEpisodeView] = []
        for episode in self.learning.episodes_for_song(song_id):
            views.append(
                FrictionEpisodeView(
                    episode_id=episode.id,
                    domain=episode.domain,
                    subject=episode.subject_ref,
                    change=episode.change_description,
                    observations=tuple(
                        self._observation_view(item)
                        for item in grouped.get(episode.id, ())
                    ),
                )
            )
        return tuple(views)

    def recurring_for_active_song(self) -> tuple[FrictionPatternView, ...]:
        song_id = self._active_song_id()
        if song_id is None:
            return ()
        return tuple(
            self._pattern_view(pattern)
            for pattern in self.friction.recurring_patterns(
                min_sessions=2,
                song_id=song_id,
            )
        )

    def capture_binding(self, episode_id: str) -> FrictionCaptureBinding:
        episode_id = self._text(episode_id, "episode_id", maximum=200)
        episode = self.learning.get_episode(episode_id)
        song_id = self._active_song_id()
        if episode is None or song_id is None or episode.song_id != song_id:
            raise StaleFrictionJourneyError(
                "That Learning episode is no longer part of the active Song."
            )
        return FrictionCaptureBinding(song_id=song_id, episode_id=episode.id)

    def record(
        self,
        binding: FrictionCaptureBinding,
        *,
        friction_key: str,
        description: str,
        confidence: str,
        prevention_hint: str | None = None,
    ) -> FrictionObservation:
        if not isinstance(binding, FrictionCaptureBinding):
            raise TypeError("binding must be FrictionCaptureBinding")
        friction_key = self._text(friction_key, "blocker name", maximum=120)
        description = self._text(description, "what got in the way", maximum=1200)
        confidence_value = self._confidence(confidence)
        prevention_hint = self._optional_text(
            prevention_hint,
            "prevention idea",
            maximum=600,
        )
        observation_id = f"fric_{uuid.uuid4().hex}"
        source_ref = f"consumer-friction:{uuid.uuid4().hex}"

        try:
            with self.store._tx():
                active = self.store._conn.execute(
                    "SELECT value FROM metadata WHERE key='active_song_id'"
                ).fetchone()
                if active is None or str(active["value"]) != binding.song_id:
                    raise StaleFrictionJourneyError(
                        "The active Song changed after this Friction action was prepared."
                    )
                episode = self.store._conn.execute(
                    "SELECT id,song_id FROM learning_episodes WHERE id=?",
                    (binding.episode_id,),
                ).fetchone()
                if episode is None or str(episode["song_id"]) != binding.song_id:
                    raise StaleFrictionJourneyError(
                        "That Learning episode no longer belongs to the active Song."
                    )
                duplicate = self.store._conn.execute(
                    "SELECT 1 FROM friction_observations "
                    "WHERE episode_id=? AND friction_key=?",
                    (binding.episode_id, friction_key),
                ).fetchone()
                if duplicate is not None:
                    raise FrictionJourneyError(
                        "That blocker is already recorded for this Learning episode. Use another blocker name only if it is genuinely a different source of friction."
                    )
                self.store._conn.execute(
                    "INSERT INTO friction_observations("
                    "id,episode_id,friction_key,description,source_kind,source_ref,"
                    "confidence,prevention_hint) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        binding.episode_id,
                        friction_key,
                        description,
                        "USER_DECLARED",
                        source_ref,
                        confidence_value,
                        prevention_hint,
                    ),
                )
        except (StaleFrictionJourneyError, FrictionJourneyError):
            raise
        except sqlite3.IntegrityError as exc:
            raise FrictionJourneyError(
                "Friction evidence was rejected safely. Reload the Song before trying again."
            ) from exc

        row = self.store._conn.execute(
            "SELECT seq,id,episode_id,friction_key,description,source_kind,"
            "source_ref,confidence,prevention_hint "
            "FROM friction_observations WHERE id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise LineageCorruptionError("Friction observation disappeared after commit")
        return self.friction._observation(row)
