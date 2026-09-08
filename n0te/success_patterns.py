from __future__ import annotations

from dataclasses import dataclass

from .lineage import LineageStore, NotFoundError
from .success import CAUSAL_STATUS, SuccessMemory, SuccessPattern

_SOURCE_LABELS = {
    "USER_DECLARED": "artist-reported",
    "OBSERVED": "observed in real work",
    "MEASURED": "measured",
    "PROVIDER_VERIFIED": "provider-verified",
    "REMEMBERED": "remembered",
    "INFERRED": "inferred",
}


@dataclass(frozen=True)
class PatternObservationView:
    observation: str
    count: int
    source_labels: tuple[str, ...]
    confidence_mean: float | None


@dataclass(frozen=True)
class PatternTermView:
    term: str
    episode_count: int


@dataclass(frozen=True)
class SuccessPatternView:
    domain: str
    subject: str
    change: str
    causal_status: str
    humility_state: str
    warning: str
    completed_count: int
    pending_count: int
    keep_count: int
    revert_count: int
    revise_count: int
    inconclusive_count: int
    observations: tuple[PatternObservationView, ...]
    conditions: tuple[PatternTermView, ...]
    alternative_explanations: tuple[PatternTermView, ...]
    observation_confidence_mean: float | None
    decision_confidence_mean: float | None


class SongSuccessPatterns:
    """Safe artist-facing projection of canonical association-only SuccessMemory.

    This class owns no persistence and does not rank or recommend changes. Internal
    pattern/episode/source-reference identities are intentionally absent. The
    canonical SuccessPattern warning is preserved because uncertainty and
    counterevidence are product truth, not implementation clutter.
    """

    def __init__(self, store: LineageStore, success: SuccessMemory):
        if not isinstance(store, LineageStore):
            raise TypeError("SongSuccessPatterns requires LineageStore")
        if not isinstance(success, SuccessMemory) or success.learning.store is not store:
            raise TypeError("SongSuccessPatterns requires SuccessMemory for the same LineageStore")
        self.store = store
        self.success = success

    @staticmethod
    def _source_labels(source_kinds: tuple[str, ...]) -> tuple[str, ...]:
        unknown = tuple(kind for kind in source_kinds if kind not in _SOURCE_LABELS)
        if unknown:
            raise RuntimeError(
                "Success evidence source semantics changed; consumer projection stopped safely"
            )
        return tuple(_SOURCE_LABELS[kind] for kind in source_kinds)

    @classmethod
    def _view(cls, pattern: SuccessPattern) -> SuccessPatternView:
        if pattern.causal_status != CAUSAL_STATUS or pattern.causal_status != "ASSOCIATION_ONLY":
            raise RuntimeError(
                "Success pattern causal semantics changed; consumer projection stopped safely"
            )
        return SuccessPatternView(
            domain=pattern.domain,
            subject=pattern.subject_ref,
            change=pattern.change_description,
            causal_status=pattern.causal_status,
            humility_state=pattern.humility_state,
            warning=pattern.warning,
            completed_count=pattern.sample_size,
            pending_count=len(pattern.pending_episode_ids),
            keep_count=pattern.keep_count,
            revert_count=pattern.revert_count,
            revise_count=pattern.revise_count,
            inconclusive_count=pattern.inconclusive_count,
            observations=tuple(
                PatternObservationView(
                    observation=item.observation,
                    count=item.count,
                    source_labels=cls._source_labels(item.source_kinds),
                    confidence_mean=item.confidence.mean,
                )
                for item in pattern.consequences
            ),
            conditions=tuple(
                PatternTermView(item.term, item.count) for item in pattern.conditions
            ),
            alternative_explanations=tuple(
                PatternTermView(item.term, item.count)
                for item in pattern.alternative_explanations
            ),
            observation_confidence_mean=pattern.consequence_confidence.mean,
            decision_confidence_mean=pattern.decision_confidence.mean,
        )

    def for_song(self, song_id: str) -> tuple[SuccessPatternView, ...]:
        song = self.store.get_song(song_id)
        if song is None:
            raise NotFoundError(f"Song not found in profile {self.store.profile_id}: {song_id}")
        return tuple(
            self._view(pattern) for pattern in self.success.patterns_for_song(song.id)
        )

    def for_active_song(self) -> tuple[SuccessPatternView, ...]:
        song = self.store.active_song()
        return () if song is None else self.for_song(song.id)
