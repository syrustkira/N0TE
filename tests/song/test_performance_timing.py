from __future__ import annotations

import hashlib
import math
import struct
import wave
from pathlib import Path

import pytest

from n0te.audio_engineering import CorruptEngineeringMedia, EngineeringEvidenceBinding
from n0te.performance_timing import (
    PERFORMANCE_TIMING_ANALYZER_VERSION,
    TIMING_INSUFFICIENT_EVENTS,
    TIMING_MEASURED,
    TIMING_SILENT,
    TIMING_TOO_SHORT,
    analyze_performance_timing,
)


def binding(path: Path) -> EngineeringEvidenceBinding:
    payload = path.read_bytes()
    return EngineeringEvidenceBinding(
        song_id="song-performance",
        version_id="version-performance",
        asset_id="asset-performance",
        sha256=hashlib.sha256(payload).hexdigest(),
        source_size_bytes=len(payload),
    )


def write_pcm16(path: Path, samples: list[int], *, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def pulse_samples(
    *,
    starts: tuple[float, ...],
    duration_seconds: float = 2.0,
    rate: int = 8000,
    pulse_seconds: float = 0.020,
    amplitude: int = 20000,
) -> list[int]:
    samples = [0] * int(round(duration_seconds * rate))
    width = int(round(pulse_seconds * rate))
    for start_seconds in starts:
        start = int(round(start_seconds * rate))
        for index in range(start, min(len(samples), start + width)):
            samples[index] = amplitude
    return samples


def test_regular_pulses_produce_deterministic_descriptive_spacing(tmp_path: Path) -> None:
    path = tmp_path / "regular.wav"
    write_pcm16(
        path,
        pulse_samples(starts=(0.20, 0.50, 0.80, 1.10, 1.40, 1.70)),
    )
    before = path.read_bytes()
    exact_binding = binding(path)

    evidence = analyze_performance_timing(path, binding=exact_binding)

    assert evidence.binding == exact_binding
    assert evidence.analyzer_version == PERFORMANCE_TIMING_ANALYZER_VERSION
    assert evidence.evidence_only
    assert evidence.has_musical_grid is False
    assert evidence.state == TIMING_MEASURED
    assert evidence.candidate_count == 6
    assert evidence.candidate_density_per_second == pytest.approx(3.0, abs=0.01)
    assert evidence.median_spacing_ms == pytest.approx(300.0, abs=0.1)
    assert evidence.spacing_mad_ms == pytest.approx(0.0, abs=0.1)
    assert path.read_bytes() == before


def test_irregular_spacing_changes_spread_without_becoming_a_quality_grade(tmp_path: Path) -> None:
    regular_path = tmp_path / "regular.wav"
    irregular_path = tmp_path / "irregular.wav"
    write_pcm16(
        regular_path,
        pulse_samples(starts=(0.20, 0.50, 0.80, 1.10, 1.40, 1.70)),
    )
    write_pcm16(
        irregular_path,
        pulse_samples(starts=(0.20, 0.45, 0.82, 1.10, 1.55, 1.80)),
    )

    regular = analyze_performance_timing(regular_path, binding=binding(regular_path))
    irregular = analyze_performance_timing(irregular_path, binding=binding(irregular_path))

    assert regular.state == TIMING_MEASURED
    assert irregular.state == TIMING_MEASURED
    assert regular.spacing_mad_ms is not None
    assert irregular.spacing_mad_ms is not None
    assert irregular.spacing_mad_ms > regular.spacing_mad_ms
    assert irregular.median_spacing_ms is not None
    assert math.isfinite(irregular.median_spacing_ms)


def test_digital_silence_has_no_invented_timing_statistics(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    write_pcm16(path, [0] * 8000)

    evidence = analyze_performance_timing(path, binding=binding(path))

    assert evidence.state == TIMING_SILENT
    assert evidence.candidate_count == 0
    assert evidence.candidate_density_per_second is None
    assert evidence.median_spacing_ms is None
    assert evidence.spacing_mad_ms is None


def test_steady_tone_is_insufficient_events_not_perfect_timing(tmp_path: Path) -> None:
    path = tmp_path / "steady.wav"
    samples = [int(round(8000 * math.sin(2.0 * math.pi * 440.0 * frame / 8000))) for frame in range(8000)]
    write_pcm16(path, samples)

    evidence = analyze_performance_timing(path, binding=binding(path))

    assert evidence.state == TIMING_INSUFFICIENT_EVENTS
    assert evidence.candidate_count < 4
    assert evidence.median_spacing_ms is None
    assert evidence.spacing_mad_ms is None


def test_two_pulses_report_count_but_withhold_spacing_statistics(tmp_path: Path) -> None:
    path = tmp_path / "two-pulses.wav"
    write_pcm16(path, pulse_samples(starts=(0.30, 1.20)))

    evidence = analyze_performance_timing(path, binding=binding(path))

    assert evidence.state == TIMING_INSUFFICIENT_EVENTS
    assert evidence.candidate_count == 2
    assert evidence.candidate_density_per_second == pytest.approx(1.0, abs=0.01)
    assert evidence.median_spacing_ms is None
    assert evidence.spacing_mad_ms is None


def test_short_material_is_named_without_fabricated_candidates(tmp_path: Path) -> None:
    path = tmp_path / "short.wav"
    write_pcm16(path, pulse_samples(starts=(0.02,), duration_seconds=0.10))

    evidence = analyze_performance_timing(path, binding=binding(path))

    assert evidence.state == TIMING_TOO_SHORT
    assert evidence.candidate_count == 0
    assert evidence.candidate_density_per_second is None
    assert evidence.median_spacing_ms is None
    assert evidence.spacing_mad_ms is None


def test_same_size_byte_tampering_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "tampered.wav"
    write_pcm16(path, pulse_samples(starts=(0.20, 0.50, 0.80, 1.10, 1.40, 1.70)))
    exact_binding = binding(path)
    changed = bytearray(path.read_bytes())
    changed[-2:] = struct.pack("<h", 1234)
    assert len(changed) == exact_binding.source_size_bytes
    path.write_bytes(changed)

    with pytest.raises(CorruptEngineeringMedia, match="fingerprint changed"):
        analyze_performance_timing(path, binding=exact_binding)
