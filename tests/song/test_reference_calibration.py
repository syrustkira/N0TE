from __future__ import annotations

from pathlib import Path

import pytest

from n0te.lineage import LineageStore
from n0te.reference_calibration import (
    ReferenceCalibrationError,
    ReferenceCalibrationProfile,
    ReferenceCandidate,
    discover_ranked_references,
    persist_ranked_reference,
    rank_reference_candidates,
    select_primary_reference,
)
from n0te.song_references import SongReferenceStore


def _profile(**overrides) -> ReferenceCalibrationProfile:
    values = {
        "tempo_bpm": 129.0,
        "energy": 0.78,
        "low_end": 0.82,
        "brightness": 0.62,
        "density": 0.74,
        "dynamics": 0.46,
        "stereo_width": 0.68,
        "semantic_tags": ("dark", "electronic"),
    }
    values.update(overrides)
    return ReferenceCalibrationProfile.create(**values)


def _candidate(
    title: str,
    *,
    profile: ReferenceCalibrationProfile,
    confidence: float = 1.0,
) -> ReferenceCandidate:
    return ReferenceCandidate(
        title=title,
        source_type="CATALOG_RECORDING",
        source_locator=f"catalog:{title.casefold().replace(' ', '-')}",
        profile=profile,
        comparison_dimensions=("low-end", "density", "arrangement"),
        source_kind="PROVIDER_VERIFIED",
        source_ref=f"provider:{title.casefold().replace(' ', '-')}",
        confidence=confidence,
    )


def test_calibration_ranks_close_reference_without_genre_memory_search():
    target = _profile()
    close = _candidate(
        "Close",
        profile=_profile(
            tempo_bpm=128.0,
            energy=0.80,
            low_end=0.79,
            density=0.76,
            semantic_tags=("dark", "electronic", "bass-house"),
        ),
    )
    wrong_lane = _candidate(
        "Wrong Lane",
        profile=_profile(
            tempo_bpm=92.0,
            energy=0.55,
            low_end=0.95,
            density=0.45,
            semantic_tags=("dark", "trap"),
        ),
    )

    ranked = rank_reference_candidates(
        target,
        (wrong_lane, close),
        required_features=("tempo-bpm", "low-end", "density"),
        desired_tags=("dark", "electronic"),
    )

    assert [item.candidate.title for item in ranked] == ["Close", "Wrong Lane"]
    assert ranked[0].similarity > ranked[1].similarity
    assert ranked[0].matched_features == ("TEMPO_BPM", "LOW_END", "DENSITY")
    assert ranked[0].matched_tags == ("DARK", "ELECTRONIC")


def test_required_feature_gate_drops_candidate_without_needed_evidence():
    target = _profile()
    tempo_only = _candidate(
        "Tempo Only",
        profile=ReferenceCalibrationProfile.create(tempo_bpm=129.0),
    )
    complete = _candidate("Complete", profile=_profile())

    ranked = rank_reference_candidates(
        target,
        (tempo_only, complete),
        required_features=("TEMPO_BPM", "LOW_END"),
    )

    assert tuple(item.candidate.title for item in ranked) == ("Complete",)


def test_missing_optional_evidence_reduces_coverage_instead_of_faking_match():
    target = _profile()
    sparse = _candidate(
        "Sparse Evidence",
        profile=ReferenceCalibrationProfile.create(tempo_bpm=129.0),
    )
    full = _candidate("Full Evidence", profile=_profile())

    ranked = rank_reference_candidates(target, (sparse, full))

    assert ranked[0].candidate.title == "Full Evidence"
    assert ranked[0].similarity > ranked[1].similarity


def test_feature_weights_can_focus_calibration_on_current_problem():
    target = _profile()
    better_low_end = _candidate(
        "Low End",
        profile=_profile(tempo_bpm=123.0, low_end=0.82, density=0.70),
    )
    better_tempo = _candidate(
        "Tempo",
        profile=_profile(tempo_bpm=129.0, low_end=0.55, density=0.70),
    )

    ranked = rank_reference_candidates(
        target,
        (better_tempo, better_low_end),
        required_features=("TEMPO_BPM", "LOW_END"),
        feature_weights={"LOW_END": 8.0, "TEMPO_BPM": 1.0},
    )

    assert ranked[0].candidate.title == "Low End"


class _Provider:
    def __init__(self, candidates):
        self.candidates = tuple(candidates)
        self.calls = []

    def discover_references(self, *, target, comparison_dimensions, limit):
        self.calls.append((target, comparison_dimensions, limit))
        return self.candidates[:limit]


def test_provider_discovery_is_separate_from_deterministic_local_calibration():
    target = _profile()
    provider = _Provider(
        (
            _candidate("Far", profile=_profile(tempo_bpm=93.0, low_end=0.4)),
            _candidate("Near", profile=_profile(tempo_bpm=130.0, low_end=0.80)),
        )
    )

    ranked = discover_ranked_references(
        provider,
        target,
        comparison_dimensions=("low-end", "arrangement"),
        required_features=("tempo_bpm", "low_end"),
        discovery_limit=10,
        result_limit=2,
    )

    assert provider.calls == [(target, ("LOW_END", "ARRANGEMENT"), 10)]
    assert [item.candidate.title for item in ranked] == ["Near", "Far"]


def test_primary_reference_is_one_explicit_choice_not_a_vibe_safari():
    target = _profile()
    ranked = rank_reference_candidates(
        target,
        (
            _candidate("Second", profile=_profile(tempo_bpm=125.0)),
            _candidate("Primary", profile=_profile(tempo_bpm=129.0)),
        ),
    )

    selected = select_primary_reference(ranked)
    assert selected.candidate.title == "Primary"

    with pytest.raises(ReferenceCalibrationError, match="no reference candidate"):
        select_primary_reference(())


def test_calibrated_primary_can_be_promoted_into_canonical_song_history(tmp_path: Path):
    lineage = LineageStore.create(tmp_path, "Reference Calibration Artist")
    try:
        song = lineage.create_song("Current Song")
        target = _profile()
        ranked = rank_reference_candidates(
            target,
            (_candidate("Primary", profile=_profile(tempo_bpm=129.5)),),
            required_features=("TEMPO_BPM", "LOW_END"),
        )
        chosen = select_primary_reference(ranked)

        persisted = persist_ranked_reference(
            SongReferenceStore(lineage),
            song_id=song.id,
            ranked=chosen,
            comparison_dimensions=("tempo-bpm", "low-end"),
        )

        assert persisted.title == "Primary"
        assert persisted.comparison_dimensions == ("TEMPO_BPM", "LOW_END")
        assert persisted.source_kind == "PROVIDER_VERIFIED"
        assert persisted.source_ref == "provider:primary"
        assert persisted.loudness_match_policy == "MATCH_BEFORE_COMPARISON"
    finally:
        lineage.close()


def test_invalid_profiles_and_unverified_provider_provenance_fail_closed():
    with pytest.raises(ReferenceCalibrationError, match="between 20 and 400"):
        ReferenceCalibrationProfile.create(tempo_bpm=4)
    with pytest.raises(ReferenceCalibrationError, match="normalized"):
        ReferenceCalibrationProfile.create(energy=1.1)
    with pytest.raises(ReferenceCalibrationError, match="requires source_ref"):
        ReferenceCandidate(
            title="Unproven",
            source_type="CATALOG_RECORDING",
            source_locator="catalog:unproven",
            profile=_profile(),
            comparison_dimensions=("LOW_END",),
            source_kind="PROVIDER_VERIFIED",
            source_ref=None,
        )


def test_rank_is_deterministic_for_equal_evidence():
    target = _profile()
    a = _candidate("Alpha", profile=_profile(), confidence=0.9)
    b = _candidate("Beta", profile=_profile(), confidence=0.9)

    first = rank_reference_candidates(target, (b, a))
    second = rank_reference_candidates(target, (a, b))

    assert [item.candidate.title for item in first] == ["Alpha", "Beta"]
    assert first == second
