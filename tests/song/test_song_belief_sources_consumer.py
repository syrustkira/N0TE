from __future__ import annotations

import pytest

from n0te.belief_sources import present_belief_source
from n0te.creative_diagnosis import (
    CreativeDiagnosis,
    DiagnosisFact,
    DiagnosisHypothesis,
    InterventionPath,
)
from n0te.creative_diagnosis_shell import (
    _diagnosis_markup,
    _fact_source_kind,
)
from n0te.evidence import SOURCE_KINDS
from n0te.lineage import ValidationError


EXPECTED_LABELS = {
    "USER_DECLARED": "YOU TOLD N0TE",
    "OBSERVED": "OBSERVED NOW",
    "MEASURED": "MEASURED",
    "PROVIDER_VERIFIED": "PROVIDER VERIFIED",
    "REMEMBERED": "REMEMBERED",
    "INFERRED": "INFERRED",
}


def _path(dimension: str, title: str) -> InterventionPath:
    return InterventionPath(
        semantic_key=f"test:{dimension.lower()}",
        dimension=dimension,
        title=title,
        rationale="A reversible test, not an artistic verdict.",
        steps=("Make one bounded change.", "Compare it against the original."),
        preserves=(),
    )


def _result(*facts: DiagnosisFact) -> CreativeDiagnosis:
    return CreativeDiagnosis(
        song_id="song-test",
        session_id=None,
        current_version_id=None,
        measured_asset_id=None,
        measured_asset_sha256=None,
        measured_source_size_bytes=None,
        analyzer_version=None,
        problem="Test the chorus impact.",
        problem_source="USER_DECLARED",
        effective_locks=(),
        evidence_status="NO_CURRENT_VERSION",
        facts=tuple(facts),
        hypotheses=(
            DiagnosisHypothesis(
                label="Path A hypothesis",
                statement="Contrast may be driving the perceived problem.",
                test_dimension="ARRANGEMENT",
            ),
            DiagnosisHypothesis(
                label="Path B hypothesis",
                statement="Energy delivery may be driving the perceived problem.",
                test_dimension="DYNAMICS",
            ),
        ),
        interventions=(
            _path("ARRANGEMENT", "Test arrangement contrast"),
            _path("DYNAMICS", "Test energy delivery"),
        ),
        limitations=("Nothing here upgrades artist preference into fact.",),
    )


def test_belief_source_presentations_cover_exact_canonical_evidence_vocabulary() -> None:
    assert set(EXPECTED_LABELS) == set(SOURCE_KINDS)
    for source_kind, expected_label in EXPECTED_LABELS.items():
        presentation = present_belief_source(source_kind.lower())
        assert presentation.source_kind == source_kind
        assert presentation.label == expected_label
        assert presentation.explanation.strip()


def test_unknown_belief_source_fails_closed() -> None:
    with pytest.raises(ValidationError, match="unsupported evidence source"):
        present_belief_source("provider_guessed")


def test_diagnosis_adapter_keeps_format_observed_but_numeric_analysis_measured() -> None:
    render_format = DiagnosisFact(
        truth_kind="OBSERVED",
        label="Current render format",
        value="8000 Hz · 2 channels · 16-bit integer PCM",
        scope="Exact verified current-Version WAV",
    )
    sample_peak = DiagnosisFact(
        truth_kind="OBSERVED",
        label="Sample peak",
        value="-6.02 dBFS",
        scope="Exact verified current-Version WAV; whole render",
    )
    rms = DiagnosisFact(
        truth_kind="OBSERVED",
        label="RMS",
        value="-12.10 dBFS",
        scope="Exact verified current-Version WAV; whole render; RMS is not LUFS",
    )

    assert _fact_source_kind(render_format) == "OBSERVED"
    assert _fact_source_kind(sample_peak) == "MEASURED"
    assert _fact_source_kind(rms) == "MEASURED"


def test_diagnosis_explains_only_source_kinds_supported_by_actual_result() -> None:
    result = _result(
        DiagnosisFact(
            truth_kind="USER_DECLARED",
            label="Problem to test",
            value="My chorus feels weak.",
            scope="Artist statement for this diagnosis",
        ),
        DiagnosisFact(
            truth_kind="OBSERVED",
            label="Current render format",
            value="8000 Hz · 2 channels · 16-bit integer PCM",
            scope="Exact verified current-Version WAV",
        ),
        DiagnosisFact(
            truth_kind="OBSERVED",
            label="Sample peak",
            value="-6.02 dBFS",
            scope="Exact verified current-Version WAV; whole render",
        ),
    )

    markup = _diagnosis_markup(result)

    assert "Why does N0TE think that?" in markup
    assert "YOU TOLD N0TE" in markup
    assert "OBSERVED NOW" in markup
    assert "MEASURED" in markup
    assert "INFERRED" in markup
    assert "PROVIDER VERIFIED" not in markup
    assert "REMEMBERED" not in markup
    assert "do not upgrade its authority or certainty" in markup
    assert "not independently verified merely because N0TE can show it here" in markup
    assert "A measurement can describe the material without deciding whether the artist should prefer it" in markup
    assert markup.count("Hypothesis, not observation.") == 2


def test_artist_only_diagnosis_does_not_manufacture_observation_measurement_or_external_truth() -> None:
    result = _result(
        DiagnosisFact(
            truth_kind="USER_DECLARED",
            label="Problem to test",
            value="The chorus feels too flat.",
            scope="Artist statement for this diagnosis",
        )
    )

    markup = _diagnosis_markup(result)

    assert "YOU TOLD N0TE" in markup
    assert "INFERRED" in markup
    assert "OBSERVED NOW" not in markup
    assert "MEASURED" not in markup
    assert "PROVIDER VERIFIED" not in markup
    assert "REMEMBERED" not in markup
