from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from .audio_engineering import (
    CorruptEngineeringMedia,
    EngineeringEvidenceBinding,
    UnsupportedEngineeringMedia,
    _decode_sample,
    _inspect_pcm_layout,
    _sha256_file,
)

PERFORMANCE_TIMING_ANALYZER_VERSION = "PERFORMANCE_TIMING_EVIDENCE_V1"
TIMING_MEASURED = "MEASURED"
TIMING_TOO_SHORT = "TOO_SHORT"
TIMING_SILENT = "SILENT"
TIMING_INSUFFICIENT_EVENTS = "INSUFFICIENT_EVENTS"
TIMING_BOUNDED_OUT = "BOUNDED_OUT"
TIMING_STATES = (
    TIMING_MEASURED,
    TIMING_TOO_SHORT,
    TIMING_SILENT,
    TIMING_INSUFFICIENT_EVENTS,
    TIMING_BOUNDED_OUT,
)

WINDOW_SECONDS = 0.010
MIN_ANALYSIS_DURATION_SECONDS = 0.250
MIN_EVENT_SEPARATION_SECONDS = 0.050
MIN_MEASURED_CANDIDATES = 4
MAX_TIMING_SAMPLE_VALUES = 30_000_000
_ABSOLUTE_RMS_CHANGE_FLOOR = 0.002
_RELATIVE_PEAK_FRACTION = 0.20
_MAD_MULTIPLIER = 6.0


@dataclass(frozen=True)
class PerformanceTimingEvidence:
    binding: EngineeringEvidenceBinding
    analyzer_version: str
    state: str
    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    frame_count: int
    duration_seconds: float
    window_seconds: float
    candidate_count: int
    candidate_density_per_second: float | None
    median_spacing_ms: float | None
    spacing_mad_ms: float | None

    def __post_init__(self) -> None:
        if self.state not in TIMING_STATES:
            raise ValueError(f"unsupported performance timing state: {self.state}")
        if self.candidate_count < 0:
            raise ValueError("candidate_count must not be negative")
        if self.state == TIMING_MEASURED:
            if self.candidate_count < MIN_MEASURED_CANDIDATES:
                raise ValueError("measured timing evidence requires enough candidates")
            if self.median_spacing_ms is None or self.spacing_mad_ms is None:
                raise ValueError("measured timing evidence requires spacing statistics")
        elif self.median_spacing_ms is not None or self.spacing_mad_ms is not None:
            raise ValueError("non-measured timing states must not invent spacing statistics")

    @property
    def evidence_only(self) -> bool:
        return True

    @property
    def has_musical_grid(self) -> bool:
        return False


def _verify_exact_material(path: Path, binding: EngineeringEvidenceBinding, *, phase: str) -> int:
    if path.is_symlink() or not path.is_file():
        raise CorruptEngineeringMedia("performance timing material is not a safe regular file")
    size_bytes = path.stat().st_size
    if size_bytes != binding.source_size_bytes:
        raise CorruptEngineeringMedia(f"performance timing material size changed {phase}")
    if _sha256_file(path) != binding.sha256:
        raise CorruptEngineeringMedia(f"performance timing material fingerprint changed {phase}")
    return size_bytes


def _empty_evidence(
    *,
    binding: EngineeringEvidenceBinding,
    state: str,
    sample_rate_hz: int,
    channels: int,
    bits_per_sample: int,
    frame_count: int,
    duration_seconds: float,
    window_seconds: float,
    candidate_count: int = 0,
    candidate_density_per_second: float | None = None,
) -> PerformanceTimingEvidence:
    return PerformanceTimingEvidence(
        binding=binding,
        analyzer_version=PERFORMANCE_TIMING_ANALYZER_VERSION,
        state=state,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        bits_per_sample=bits_per_sample,
        frame_count=frame_count,
        duration_seconds=duration_seconds,
        window_seconds=window_seconds,
        candidate_count=candidate_count,
        candidate_density_per_second=candidate_density_per_second,
        median_spacing_ms=None,
        spacing_mad_ms=None,
    )


def _energy_envelope(path: Path, *, layout) -> tuple[list[float], float, float]:
    bytes_per_sample = layout.bits_per_sample // 8
    window_frames = max(1, int(round(layout.sample_rate_hz * WINDOW_SECONDS)))
    actual_window_seconds = window_frames / float(layout.sample_rate_hz)
    envelope: list[float] = []
    window_sum_sq = 0.0
    window_sample_count = 0
    frames_in_window = 0
    frames_seen = 0
    sample_values_seen = 0
    sample_peak = 0.0

    with path.open("rb") as source:
        for offset, chunk_size in layout.data_chunks:
            source.seek(offset)
            remaining = chunk_size
            while remaining:
                request = min(1024 * 1024, remaining)
                request -= request % layout.block_align
                if request <= 0:
                    request = layout.block_align
                block = source.read(request)
                if len(block) != request:
                    raise CorruptEngineeringMedia(
                        "WAV audio data ended before its declared size during performance timing analysis"
                    )
                remaining -= len(block)
                for frame_start in range(0, len(block), layout.block_align):
                    for channel in range(layout.channels):
                        start = frame_start + channel * bytes_per_sample
                        value = _decode_sample(
                            block[start : start + bytes_per_sample],
                            layout.bits_per_sample,
                        )
                        sample_peak = max(sample_peak, abs(value))
                        window_sum_sq += value * value
                        window_sample_count += 1
                        sample_values_seen += 1
                    frames_seen += 1
                    frames_in_window += 1
                    if frames_in_window == window_frames:
                        envelope.append(math.sqrt(window_sum_sq / window_sample_count))
                        window_sum_sq = 0.0
                        window_sample_count = 0
                        frames_in_window = 0

    if frames_in_window:
        envelope.append(math.sqrt(window_sum_sq / window_sample_count))
    expected_values = layout.frame_count * layout.channels
    if frames_seen != layout.frame_count or sample_values_seen != expected_values:
        raise CorruptEngineeringMedia("PCM frame count changed during performance timing analysis")
    return envelope, sample_peak, actual_window_seconds


def _candidate_windows(envelope: list[float], *, window_seconds: float) -> tuple[int, ...]:
    if len(envelope) < 2:
        return ()
    deltas = [max(0.0, envelope[index] - envelope[index - 1]) for index in range(1, len(envelope))]
    max_delta = max(deltas, default=0.0)
    if max_delta <= 0.0:
        return ()

    center = median(deltas)
    spread = median(abs(value - center) for value in deltas)
    threshold = max(
        _ABSOLUTE_RMS_CHANGE_FLOOR,
        center + (_MAD_MULTIPLIER * spread),
        max_delta * _RELATIVE_PEAK_FRACTION,
    )
    local_peaks: list[tuple[int, float]] = []
    for offset, value in enumerate(deltas):
        if value < threshold:
            continue
        prior = deltas[offset - 1] if offset > 0 else -1.0
        following = deltas[offset + 1] if offset + 1 < len(deltas) else -1.0
        if value < prior or value < following:
            continue
        # delta[offset] is the positive change entering envelope window offset + 1.
        local_peaks.append((offset + 1, value))

    minimum_windows = max(1, int(math.ceil(MIN_EVENT_SEPARATION_SECONDS / window_seconds)))
    selected: list[tuple[int, float]] = []
    for window_index, strength in local_peaks:
        if not selected or window_index - selected[-1][0] >= minimum_windows:
            selected.append((window_index, strength))
            continue
        if strength > selected[-1][1]:
            selected[-1] = (window_index, strength)
    return tuple(window_index for window_index, _ in selected)


def analyze_performance_timing(
    path: str | Path,
    *,
    binding: EngineeringEvidenceBinding,
) -> PerformanceTimingEvidence:
    """Describe exact PCM energy-change timing without grading musical performance.

    This analyzer has no tempo, meter, beat phase, note transcription, performer
    identity, or artistic reference. Its candidates are positive short-time energy
    changes only. Spacing statistics therefore cannot truthfully mean early/late,
    ahead/behind, tight/sloppy, swung/straight, correct/incorrect, or in need of
    quantization. The function is read-only and re-verifies exact material identity
    before returning evidence.
    """

    media_path = Path(path)
    size_bytes = _verify_exact_material(media_path, binding, phase="after lineage verification")
    layout = _inspect_pcm_layout(media_path, size_bytes=size_bytes)
    duration_seconds = layout.frame_count / float(layout.sample_rate_hz)
    window_frames = max(1, int(round(layout.sample_rate_hz * WINDOW_SECONDS)))
    actual_window_seconds = window_frames / float(layout.sample_rate_hz)

    if duration_seconds < MIN_ANALYSIS_DURATION_SECONDS:
        _verify_exact_material(media_path, binding, phase="before evidence return")
        return _empty_evidence(
            binding=binding,
            state=TIMING_TOO_SHORT,
            sample_rate_hz=layout.sample_rate_hz,
            channels=layout.channels,
            bits_per_sample=layout.bits_per_sample,
            frame_count=layout.frame_count,
            duration_seconds=duration_seconds,
            window_seconds=actual_window_seconds,
        )

    sample_values = layout.frame_count * layout.channels
    if sample_values > MAX_TIMING_SAMPLE_VALUES:
        _verify_exact_material(media_path, binding, phase="before evidence return")
        return _empty_evidence(
            binding=binding,
            state=TIMING_BOUNDED_OUT,
            sample_rate_hz=layout.sample_rate_hz,
            channels=layout.channels,
            bits_per_sample=layout.bits_per_sample,
            frame_count=layout.frame_count,
            duration_seconds=duration_seconds,
            window_seconds=actual_window_seconds,
        )

    envelope, sample_peak, actual_window_seconds = _energy_envelope(media_path, layout=layout)
    _verify_exact_material(media_path, binding, phase="during performance timing analysis")
    if sample_peak <= 0.0:
        return _empty_evidence(
            binding=binding,
            state=TIMING_SILENT,
            sample_rate_hz=layout.sample_rate_hz,
            channels=layout.channels,
            bits_per_sample=layout.bits_per_sample,
            frame_count=layout.frame_count,
            duration_seconds=duration_seconds,
            window_seconds=actual_window_seconds,
        )

    candidates = _candidate_windows(envelope, window_seconds=actual_window_seconds)
    candidate_count = len(candidates)
    density = candidate_count / duration_seconds
    if candidate_count < MIN_MEASURED_CANDIDATES:
        _verify_exact_material(media_path, binding, phase="before evidence return")
        return _empty_evidence(
            binding=binding,
            state=TIMING_INSUFFICIENT_EVENTS,
            sample_rate_hz=layout.sample_rate_hz,
            channels=layout.channels,
            bits_per_sample=layout.bits_per_sample,
            frame_count=layout.frame_count,
            duration_seconds=duration_seconds,
            window_seconds=actual_window_seconds,
            candidate_count=candidate_count,
            candidate_density_per_second=density,
        )

    times = [window_index * actual_window_seconds for window_index in candidates]
    spacings = [times[index] - times[index - 1] for index in range(1, len(times))]
    spacing_center = median(spacings)
    spacing_mad = median(abs(value - spacing_center) for value in spacings)
    _verify_exact_material(media_path, binding, phase="before evidence return")
    return PerformanceTimingEvidence(
        binding=binding,
        analyzer_version=PERFORMANCE_TIMING_ANALYZER_VERSION,
        state=TIMING_MEASURED,
        sample_rate_hz=layout.sample_rate_hz,
        channels=layout.channels,
        bits_per_sample=layout.bits_per_sample,
        frame_count=layout.frame_count,
        duration_seconds=duration_seconds,
        window_seconds=actual_window_seconds,
        candidate_count=candidate_count,
        candidate_density_per_second=density,
        median_spacing_ms=spacing_center * 1000.0,
        spacing_mad_ms=spacing_mad * 1000.0,
    )
