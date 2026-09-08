from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from .learning import LearningEpisode, LearningMemory

HUMILITY_STATES = {
    "NO_COMPLETED_EVIDENCE",
    "SINGLE_OBSERVATION",
    "SUCCESS_ONLY",
    "MIXED",
    "NO_KEEP_EVIDENCE",
    "INCONCLUSIVE_ONLY",
}
CAUSAL_STATUS = "ASSOCIATION_ONLY"


def _text(value: str, field: str) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _domain(value: str) -> str:
    return _text(value, "domain").upper()


def _pattern_id(domain: str, subject_ref: str, change_description: str) -> str:
    payload = json.dumps(
        {
            "domain": domain,
            "subject_ref": subject_ref,
            "change_description": change_description,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "success_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class ConfidenceSummary:
    count: int
    minimum: float | None
    maximum: float | None
    mean: float | None

    @classmethod
    def from_values(cls, values: Iterable[float]) -> "ConfidenceSummary":
        numbers = tuple(float(value) for value in values)
        if not numbers:
            return cls(0, None, None, None)
        return cls(
            count=len(numbers),
            minimum=min(numbers),
            maximum=max(numbers),
            mean=sum(numbers) / len(numbers),
        )


@dataclass(frozen=True)
class TermEvidence:
    term: str
    count: int
    episode_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConsequenceSummary:
    observation: str
    count: int
    episode_ids: tuple[str, ...]
    song_ids: tuple[str, ...]
    source_kinds: tuple[str, ...]
    source_refs: tuple[str, ...]
    confidence: ConfidenceSummary


@dataclass(frozen=True)
class SuccessPattern:
    id: str
    domain: str
    subject_ref: str
    change_description: str
    causal_status: str
    humility_state: str
    warning: str
    song_ids: tuple[str, ...]
    sample_size: int
    completed_episode_ids: tuple[str, ...]
    pending_episode_ids: tuple[str, ...]
    supporting_episode_ids: tuple[str, ...]
    counterexample_episode_ids: tuple[str, ...]
    inconclusive_episode_ids: tuple[str, ...]
    keep_count: int
    revert_count: int
    revise_count: int
    inconclusive_count: int
    consequences: tuple[ConsequenceSummary, ...]
    conditions: tuple[TermEvidence, ...]
    alternative_explanations: tuple[TermEvidence, ...]
    consequence_confidence: ConfidenceSummary
    decision_confidence: ConfidenceSummary

    @property
    def has_counterexamples(self) -> bool:
        return bool(self.counterexample_episode_ids)


class SuccessMemory:
    """Pure-read synthesis over durable LearningMemory.

    A pattern summarizes observed consequences and artist decisions. It never
    asserts that the represented change caused an outcome and never creates,
    edits, supersedes, promotes or ranks durable memory.
    """

    def __init__(self, learning: LearningMemory):
        self.learning = learning

    @staticmethod
    def _group_key(episode: LearningEpisode) -> tuple[str, str, str]:
        return (
            _domain(episode.domain),
            _text(episode.subject_ref, "subject_ref"),
            _text(episode.change_description, "change_description"),
        )

    @staticmethod
    def _humility_state(
        sample_size: int,
        keep_count: int,
        counter_count: int,
        inconclusive_count: int,
    ) -> str:
        if sample_size == 0:
            return "NO_COMPLETED_EVIDENCE"
        if keep_count == 0:
            if inconclusive_count == sample_size:
                return "INCONCLUSIVE_ONLY"
            return "NO_KEEP_EVIDENCE"
        if keep_count == 1 and counter_count == 0:
            return "SINGLE_OBSERVATION"
        if counter_count:
            return "MIXED"
        return "SUCCESS_ONLY"

    @staticmethod
    def _warning(state: str) -> str:
        return {
            "NO_COMPLETED_EVIDENCE": (
                "No completed Learning decision exists for this exact pattern; "
                "there is nothing yet to generalize."
            ),
            "SINGLE_OBSERVATION": (
                "One retained example is thin evidence. Treat it as a prior example, "
                "not proof of causation or a reusable rule."
            ),
            "SUCCESS_ONLY": (
                "Retained examples have no recorded counterexample in this exact "
                "pattern. Absence of counterexamples is not proof of causation, "
                "general success, or future performance."
            ),
            "MIXED": (
                "Comparable episodes disagree. Preserve the conditions, confounders "
                "and counterexamples instead of collapsing them into a success rule."
            ),
            "NO_KEEP_EVIDENCE": (
                "No comparable completed episode was retained as-is. This is "
                "counterevidence to a success claim, not proof that the change can "
                "never work."
            ),
            "INCONCLUSIVE_ONLY": (
                "Comparable completed episodes are inconclusive. No success or "
                "failure rule is supported."
            ),
        }[state]

    @staticmethod
    def _term_evidence(
        episodes: tuple[LearningEpisode, ...], attr: str
    ) -> tuple[TermEvidence, ...]:
        buckets: dict[str, set[str]] = {}
        for episode in episodes:
            episode_terms: set[str] = set()
            for consequence in episode.consequences:
                for raw in getattr(consequence, attr):
                    episode_terms.add(
                        _text(raw, attr[:-1] if attr.endswith("s") else attr)
                    )
            for term in episode_terms:
                buckets.setdefault(term, set()).add(episode.id)
        return tuple(
            TermEvidence(term, len(buckets[term]), tuple(sorted(buckets[term])))
            for term in sorted(buckets)
        )

    @staticmethod
    def _consequence_summaries(
        episodes: tuple[LearningEpisode, ...],
    ) -> tuple[ConsequenceSummary, ...]:
        rows: dict[str, list[tuple[LearningEpisode, object]]] = {}
        for episode in episodes:
            for consequence in episode.consequences:
                observation = _text(consequence.observation, "observation")
                rows.setdefault(observation, []).append((episode, consequence))
        summaries: list[ConsequenceSummary] = []
        for observation in sorted(rows):
            items = rows[observation]
            summaries.append(
                ConsequenceSummary(
                    observation=observation,
                    count=len(items),
                    episode_ids=tuple(sorted({episode.id for episode, _ in items})),
                    song_ids=tuple(sorted({episode.song_id for episode, _ in items})),
                    source_kinds=tuple(
                        sorted({str(consequence.source_kind) for _, consequence in items})
                    ),
                    source_refs=tuple(
                        sorted({str(consequence.source_ref) for _, consequence in items})
                    ),
                    confidence=ConfidenceSummary.from_values(
                        consequence.confidence for _, consequence in items
                    ),
                )
            )
        return tuple(summaries)

    def _build_pattern(
        self,
        key: tuple[str, str, str],
        episodes: tuple[LearningEpisode, ...],
    ) -> SuccessPattern:
        ordered = tuple(sorted(episodes, key=lambda item: (item.sequence, item.id)))
        completed = tuple(episode for episode in ordered if episode.decision is not None)
        pending = tuple(episode for episode in ordered if episode.decision is None)

        supporting = tuple(
            episode for episode in completed if episode.decision.decision == "KEEP"
        )
        counter = tuple(
            episode
            for episode in completed
            if episode.decision.decision in {"REVERT", "REVISE"}
        )
        inconclusive = tuple(
            episode
            for episode in completed
            if episode.decision.decision == "INCONCLUSIVE"
        )

        keep_count = len(supporting)
        revert_count = sum(
            1 for episode in completed if episode.decision.decision == "REVERT"
        )
        revise_count = sum(
            1 for episode in completed if episode.decision.decision == "REVISE"
        )
        inconclusive_count = len(inconclusive)
        state = self._humility_state(
            len(completed), keep_count, len(counter), inconclusive_count
        )

        domain, subject_ref, change_description = key
        return SuccessPattern(
            id=_pattern_id(domain, subject_ref, change_description),
            domain=domain,
            subject_ref=subject_ref,
            change_description=change_description,
            causal_status=CAUSAL_STATUS,
            humility_state=state,
            warning=self._warning(state),
            song_ids=tuple(sorted({episode.song_id for episode in ordered})),
            sample_size=len(completed),
            completed_episode_ids=tuple(episode.id for episode in completed),
            pending_episode_ids=tuple(episode.id for episode in pending),
            supporting_episode_ids=tuple(episode.id for episode in supporting),
            counterexample_episode_ids=tuple(episode.id for episode in counter),
            inconclusive_episode_ids=tuple(episode.id for episode in inconclusive),
            keep_count=keep_count,
            revert_count=revert_count,
            revise_count=revise_count,
            inconclusive_count=inconclusive_count,
            consequences=self._consequence_summaries(completed),
            conditions=self._term_evidence(completed, "conditions"),
            alternative_explanations=self._term_evidence(completed, "confounders"),
            consequence_confidence=ConfidenceSummary.from_values(
                consequence.confidence
                for episode in completed
                for consequence in episode.consequences
            ),
            decision_confidence=ConfidenceSummary.from_values(
                episode.decision.confidence for episode in completed
            ),
        )

    def _patterns(
        self, episodes: Iterable[LearningEpisode]
    ) -> tuple[SuccessPattern, ...]:
        groups: dict[tuple[str, str, str], list[LearningEpisode]] = {}
        for episode in episodes:
            groups.setdefault(self._group_key(episode), []).append(episode)
        return tuple(
            self._build_pattern(key, tuple(groups[key])) for key in sorted(groups)
        )

    def patterns_for_song(self, song_id: str) -> tuple[SuccessPattern, ...]:
        return self._patterns(self.learning.episodes_for_song(song_id))

    def patterns_for_artist(self) -> tuple[SuccessPattern, ...]:
        rows = self.learning.store._conn.execute(
            "SELECT DISTINCT song_id FROM learning_episodes ORDER BY song_id"
        ).fetchall()
        episodes: list[LearningEpisode] = []
        for row in rows:
            episodes.extend(self.learning.episodes_for_song(str(row["song_id"])))
        return self._patterns(episodes)
