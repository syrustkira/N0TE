from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from .learning import DECISION_KINDS, ConsequenceObservation, LearningDecision, LearningEpisode, LearningMemory
from .lineage import LineageCorruptionError, ValidationError

_CONFIDENCE_LEVELS = {"LOW": 0.4, "MEDIUM": 0.7, "HIGH": 1.0}


class LearningExperimentError(RuntimeError):
    """A consumer Learning experiment operation cannot be applied safely."""


class StaleLearningExperimentError(LearningExperimentError):
    """The Song, Session, episode, or observations moved after authority was rendered."""


@dataclass(frozen=True)
class LearningStartBinding:
    song_id: str
    session_id: str


@dataclass(frozen=True)
class LearningDecisionBinding:
    episode_id: str
    expected_consequence_ids: tuple[str, ...]


class LearningExperimentService:
    """Bounded artist authority over canonical change → consequence → decision memory.

    The service owns no persistence. New episodes require the exact active Song and
    exact latest open work Session the artist saw. Observations are always
    USER_DECLARED with server-owned source references. Terminal decisions bind the
    exact ordered consequence set rendered to the artist so a stale page cannot
    decide past unseen evidence. Nothing here asserts that a change caused an
    observed consequence.
    """

    def __init__(self, learning: LearningMemory):
        if not isinstance(learning, LearningMemory):
            raise TypeError("LearningExperimentService requires canonical LearningMemory")
        self.learning = learning
        self.sessions = learning.sessions
        self.store = learning.store

    @staticmethod
    def _text(value: str, field: str, *, maximum: int) -> str:
        text = " ".join(str(value).split())
        if not text:
            raise ValidationError(f"{field} must not be empty")
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return text

    @staticmethod
    def _optional_text(value: str | None, field: str, *, maximum: int) -> tuple[str, ...]:
        if value is None:
            return ()
        text = " ".join(str(value).split())
        if not text:
            return ()
        if len(text) > maximum:
            raise ValidationError(f"{field} is too long")
        return (text,)

    @staticmethod
    def _confidence(value: str) -> float:
        key = str(value).strip().upper()
        if key not in _CONFIDENCE_LEVELS:
            raise ValidationError("unsupported confidence level")
        return _CONFIDENCE_LEVELS[key]

    def _active_song_id(self) -> str | None:
        song = self.store.active_song()
        return None if song is None else song.id

    def start_binding(self) -> LearningStartBinding | None:
        song = self.store.active_song()
        if song is None:
            return None
        session = self.sessions.latest_for_song(song.id)
        if session is None or session.state != "OPEN":
            return None
        return LearningStartBinding(song.id, session.id)

    def start_episode(
        self,
        binding: LearningStartBinding,
        *,
        domain: str,
        subject: str,
        change_description: str,
    ) -> LearningEpisode:
        if not isinstance(binding, LearningStartBinding):
            raise TypeError("binding must be LearningStartBinding")
        domain = self._text(domain, "domain", maximum=120)
        subject = self._text(subject, "subject", maximum=160)
        change_description = self._text(change_description, "change", maximum=1200)
        episode_id = f"learn_{uuid.uuid4().hex}"

        try:
            with self.store._tx():
                active = self.store._conn.execute(
                    "SELECT value FROM metadata WHERE key='active_song_id'"
                ).fetchone()
                if active is None or str(active["value"]) != binding.song_id:
                    raise StaleLearningExperimentError(
                        "The active Song changed after this Learning action was prepared."
                    )
                latest = self.store._conn.execute(
                    "SELECT id,artist_id,song_id,version_id,state FROM sessions "
                    "WHERE song_id=? ORDER BY seq DESC LIMIT 1",
                    (binding.song_id,),
                ).fetchone()
                if (
                    latest is None
                    or str(latest["id"]) != binding.session_id
                    or str(latest["state"]) != "OPEN"
                ):
                    raise StaleLearningExperimentError(
                        "The open work Session changed after this Learning action was prepared."
                    )
                self.store._conn.execute(
                    "INSERT INTO learning_episodes("
                    "id,artist_id,song_id,version_id,session_id,domain,subject_ref,change_description"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        episode_id,
                        self.store.primary_artist_id,
                        binding.song_id,
                        None if latest["version_id"] is None else str(latest["version_id"]),
                        binding.session_id,
                        domain,
                        subject,
                        change_description,
                    ),
                )
        except StaleLearningExperimentError:
            raise
        except sqlite3.IntegrityError as exc:
            raise LearningExperimentError(f"Learning experiment was rejected safely: {exc}") from exc

        episode = self.learning.get_episode(episode_id)
        if episode is None:
            raise LineageCorruptionError("Learning episode disappeared after commit")
        return episode

    def append_observation(
        self,
        episode_id: str,
        *,
        observation: str,
        confidence: str,
        conditions: str | None = None,
        confounders: str | None = None,
    ) -> ConsequenceObservation:
        episode_id = self._text(episode_id, "episode_id", maximum=200)
        observation = self._text(observation, "observation", maximum=1200)
        confidence_value = self._confidence(confidence)
        conditions_tuple = self._optional_text(conditions, "conditions", maximum=600)
        confounders_tuple = self._optional_text(confounders, "confounders", maximum=600)
        consequence_id = f"lobs_{uuid.uuid4().hex}"
        source_ref = f"consumer-learning-observation:{uuid.uuid4().hex}"

        try:
            with self.store._tx():
                episode = self.store._conn.execute(
                    "SELECT id,song_id FROM learning_episodes WHERE id=?",
                    (episode_id,),
                ).fetchone()
                if episode is None:
                    raise StaleLearningExperimentError("That Learning experiment no longer exists.")
                active = self.store._conn.execute(
                    "SELECT value FROM metadata WHERE key='active_song_id'"
                ).fetchone()
                if active is None or str(active["value"]) != str(episode["song_id"]):
                    raise StaleLearningExperimentError(
                        "The active Song changed. Reload before recording this observation."
                    )
                if self.store._conn.execute(
                    "SELECT 1 FROM learning_decisions WHERE episode_id=?",
                    (episode_id,),
                ).fetchone() is not None:
                    raise StaleLearningExperimentError(
                        "That Learning experiment already has a final decision."
                    )
                self.store._conn.execute(
                    "INSERT INTO learning_consequences("
                    "id,episode_id,observation,source_kind,source_ref,confidence,"
                    "conditions_json,confounders_json) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        consequence_id,
                        episode_id,
                        observation,
                        "USER_DECLARED",
                        source_ref,
                        confidence_value,
                        json.dumps(conditions_tuple, separators=(",", ":")),
                        json.dumps(confounders_tuple, separators=(",", ":")),
                    ),
                )
        except StaleLearningExperimentError:
            raise
        except sqlite3.IntegrityError as exc:
            raise LearningExperimentError(f"Learning observation was rejected safely: {exc}") from exc

        episode = self.learning.get_episode(episode_id)
        if episode is None:
            raise LineageCorruptionError("Learning episode disappeared after observation commit")
        result = next((item for item in episode.consequences if item.id == consequence_id), None)
        if result is None:
            raise LineageCorruptionError("Learning observation disappeared after commit")
        return result

    def decision_binding(self, episode_id: str) -> LearningDecisionBinding:
        episode = self.learning.get_episode(episode_id)
        if episode is None:
            raise LearningExperimentError("Learning experiment not found")
        if episode.decision is not None:
            raise LearningExperimentError("Learning experiment already has a decision")
        if not episode.consequences:
            raise LearningExperimentError("Record what you observed before deciding")
        return LearningDecisionBinding(
            episode.id,
            tuple(item.id for item in episode.consequences),
        )

    def decide(
        self,
        binding: LearningDecisionBinding,
        *,
        decision: str,
        rationale: str,
        confidence: str,
    ) -> LearningDecision:
        if not isinstance(binding, LearningDecisionBinding):
            raise TypeError("binding must be LearningDecisionBinding")
        decision = str(decision).strip().upper()
        if decision not in DECISION_KINDS:
            raise ValidationError(f"unsupported Learning decision: {decision}")
        rationale = self._text(rationale, "rationale", maximum=1200)
        confidence_value = self._confidence(confidence)
        decision_id = f"ldec_{uuid.uuid4().hex}"

        try:
            with self.store._tx():
                episode = self.store._conn.execute(
                    "SELECT id,song_id FROM learning_episodes WHERE id=?",
                    (binding.episode_id,),
                ).fetchone()
                if episode is None:
                    raise StaleLearningExperimentError("That Learning experiment no longer exists.")
                active = self.store._conn.execute(
                    "SELECT value FROM metadata WHERE key='active_song_id'"
                ).fetchone()
                if active is None or str(active["value"]) != str(episode["song_id"]):
                    raise StaleLearningExperimentError(
                        "The active Song changed. Reload before deciding."
                    )
                if self.store._conn.execute(
                    "SELECT 1 FROM learning_decisions WHERE episode_id=?",
                    (binding.episode_id,),
                ).fetchone() is not None:
                    raise StaleLearningExperimentError(
                        "That Learning experiment already has a final decision."
                    )
                current_ids = tuple(
                    str(row["id"])
                    for row in self.store._conn.execute(
                        "SELECT id FROM learning_consequences WHERE episode_id=? ORDER BY seq",
                        (binding.episode_id,),
                    )
                )
                if not current_ids:
                    raise StaleLearningExperimentError(
                        "That Learning experiment has no observations to decide from."
                    )
                if current_ids != binding.expected_consequence_ids:
                    raise StaleLearningExperimentError(
                        "New Learning evidence was recorded after this decision was prepared."
                    )
                self.store._conn.execute(
                    "INSERT INTO learning_decisions(id,episode_id,decision,rationale,confidence) "
                    "VALUES(?,?,?,?,?)",
                    (
                        decision_id,
                        binding.episode_id,
                        decision,
                        rationale,
                        confidence_value,
                    ),
                )
        except StaleLearningExperimentError:
            raise
        except sqlite3.IntegrityError as exc:
            raise LearningExperimentError(f"Learning decision was rejected safely: {exc}") from exc

        episode = self.learning.get_episode(binding.episode_id)
        if episode is None or episode.decision is None:
            raise LineageCorruptionError("Learning decision disappeared after commit")
        if episode.decision.id != decision_id:
            raise LineageCorruptionError("Learning decision does not match committed authority")
        return episode.decision

    def episodes_for_active_song(self) -> tuple[LearningEpisode, ...]:
        song_id = self._active_song_id()
        if song_id is None:
            return ()
        return self.learning.episodes_for_song(song_id)
