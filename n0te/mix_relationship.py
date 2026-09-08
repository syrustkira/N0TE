from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from .audio_engineering import (
    LOUDNESS_MEASURED,
    CorruptEngineeringMedia,
    EngineeringEvidenceBinding,
    EngineeringSnapshot,
    _decode_sample,
    _inspect_pcm_layout,
    _sha256_file,
)

RELATIONSHIP_ANALYZER_VERSION = "MIX_RELATIONSHIP_V1"
SPECTRAL_MEASURED = "MEASURED"
SPECTRAL_TOO_SHORT = "TOO_SHORT"
SPECTRAL_SILENT = "SILENT"
SPECTRAL_UNSUPPORTED_CHANNEL_LAYOUT = "UNSUPPORTED_CHANNEL_LAYOUT"
SPECTRAL_BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
SPECTRAL_BACKEND_ERROR = "BACKEND_ERROR"

SPECTRAL_WINDOW_FRAMES = 2048
MIN_SPECTRAL_FRAMES = 256
MAX_SPECTRAL_WINDOWS = 24
SHARED_BAND_MIN_SHARE = 0.10

SPECTRAL_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Sub", 20.0, 60.0),
    ("Bass", 60.0, 250.0),
    ("Low-mid", 250.0, 500.0),
    ("Mid", 500.0, 2000.0),
    ("Presence", 2000.0, 6000.0),
    ("Air", 6000.0, 20000.0),
)


class MixRelationshipError(RuntimeError):
    """Exact material cannot produce truthful bounded mix-relationship evidence."""


@dataclass(frozen=True)
class MixRelationshipEvidence:
    left_binding: EngineeringEvidenceBinding
    right_binding: EngineeringEvidenceBinding
    analyzer_version: str
    rms_delta_db: float | None
    integrated_loudness_delta_lu: float | None
    crest_factor_delta_db: float | None
    left_stereo_correlation: float | None
    right_stereo_correlation: float | None
    spectral_overlap_ratio: float | None
    shared_spectral_bands: tuple[str, ...]
    left_spectral_state: str
    right_spectral_state: str
    left_sampled_windows: int
    right_sampled_windows: int
    action_authority_granted: bool = field(default=False, init=False)
    mutation_authorized: bool = field(default=False, init=False)
    artistic_decision_recorded: bool = field(default=False, init=False)

    @property
    def evidence_only(self) -> bool:
        return True


@dataclass(frozen=True)
class _SpectralProfile:
    state: str
    shares: tuple[float, ...] | None
    sampled_windows: int


def _verify_exact_material(path: Path, binding: EngineeringEvidenceBinding) -> None:
    if path.is_symlink() or not path.is_file():
        raise CorruptEngineeringMedia("relationship material is not a safe regular file")
    if path.stat().st_size != binding.source_size_bytes:
        raise CorruptEngineeringMedia(
            "relationship material size changed after lineage verification"
        )
    if _sha256_file(path) != binding.sha256:
        raise CorruptEngineeringMedia(
            "relationship material fingerprint changed after lineage verification"
        )


def _window_positions(frame_count: int, window_frames: int) -> tuple[int, ...]:
    if frame_count <= window_frames:
        return (0,)
    max_start = frame_count - window_frames
    possible = max(1, frame_count // window_frames)
    count = min(MAX_SPECTRAL_WINDOWS, possible)
    if count <= 1:
        return (max_start // 2,)
    return tuple(
        int(round(index * max_start / float(count - 1)))
        for index in range(count)
    )


def _read_logical_frames(path: Path, layout, *, start_frame: int, frame_count: int) -> bytes:
    target_start = start_frame * layout.block_align
    target_end = target_start + frame_count * layout.block_align
    logical_start = 0
    pieces: list[bytes] = []
    with path.open("rb") as source:
        for file_offset, chunk_size in layout.data_chunks:
            logical_end = logical_start + chunk_size
            overlap_start = max(target_start, logical_start)
            overlap_end = min(target_end, logical_end)
            if overlap_start < overlap_end:
                offset_in_chunk = overlap_start - logical_start
                take = overlap_end - overlap_start
                source.seek(file_offset + offset_in_chunk)
                block = source.read(take)
                if len(block) != take:
                    raise CorruptEngineeringMedia(
                        "relationship PCM data ended before its declared size"
                    )
                pieces.append(block)
            logical_start = logical_end
            if logical_start >= target_end:
                break
    payload = b"".join(pieces)
    expected = frame_count * layout.block_align
    if len(payload) != expected:
        raise CorruptEngineeringMedia(
            "relationship PCM frame window could not be reconstructed exactly"
        )
    return payload


def _sampled_spectral_profile(
    path: str | Path,
    *,
    binding: EngineeringEvidenceBinding,
) -> _SpectralProfile:
    media_path = Path(path)
    _verify_exact_material(media_path, binding)
    layout = _inspect_pcm_layout(
        media_path,
        size_bytes=binding.source_size_bytes,
    )
    if layout.channels not in {1, 2}:
        return _SpectralProfile(
            state=SPECTRAL_UNSUPPORTED_CHANNEL_LAYOUT,
            shares=None,
            sampled_windows=0,
        )
    if layout.frame_count < MIN_SPECTRAL_FRAMES:
        return _SpectralProfile(
            state=SPECTRAL_TOO_SHORT,
            shares=None,
            sampled_windows=0,
        )
    try:
        import numpy as np
    except ImportError:
        return _SpectralProfile(
            state=SPECTRAL_BACKEND_UNAVAILABLE,
            shares=None,
            sampled_windows=0,
        )

    window_frames = min(SPECTRAL_WINDOW_FRAMES, layout.frame_count)
    energies = np.zeros(len(SPECTRAL_BANDS), dtype=np.float64)
    sampled = 0
    bytes_per_sample = layout.bits_per_sample // 8

    try:
        for start_frame in _window_positions(layout.frame_count, window_frames):
            payload = _read_logical_frames(
                media_path,
                layout,
                start_frame=start_frame,
                frame_count=window_frames,
            )
            channels = np.empty(
                (layout.channels, window_frames),
                dtype=np.float64,
            )
            for frame_index in range(window_frames):
                frame_start = frame_index * layout.block_align
                for channel in range(layout.channels):
                    sample_start = frame_start + channel * bytes_per_sample
                    channels[channel, frame_index] = _decode_sample(
                        payload[sample_start : sample_start + bytes_per_sample],
                        layout.bits_per_sample,
                    )

            channels -= np.mean(channels, axis=1, keepdims=True)
            if not np.any(channels):
                sampled += 1
                continue
            tapered = channels * np.hanning(window_frames)
            spectrum = np.fft.rfft(tapered, axis=1)
            power = np.mean(np.abs(spectrum) ** 2, axis=0)
            frequencies = np.fft.rfftfreq(
                window_frames,
                d=1.0 / float(layout.sample_rate_hz),
            )
            for index, (_, low_hz, high_hz) in enumerate(SPECTRAL_BANDS):
                mask = (frequencies >= low_hz) & (frequencies < high_hz)
                if np.any(mask):
                    energies[index] += float(np.sum(power[mask]))
            sampled += 1
    except (ValueError, TypeError, FloatingPointError, OverflowError, MemoryError):
        return _SpectralProfile(
            state=SPECTRAL_BACKEND_ERROR,
            shares=None,
            sampled_windows=sampled,
        )

    _verify_exact_material(media_path, binding)

    total_energy = float(np.sum(energies))
    if not math.isfinite(total_energy):
        return _SpectralProfile(
            state=SPECTRAL_BACKEND_ERROR,
            shares=None,
            sampled_windows=sampled,
        )
    if total_energy <= 0.0:
        return _SpectralProfile(
            state=SPECTRAL_SILENT,
            shares=None,
            sampled_windows=sampled,
        )
    shares = tuple(float(value / total_energy) for value in energies)
    if any(not math.isfinite(value) or value < 0.0 for value in shares):
        return _SpectralProfile(
            state=SPECTRAL_BACKEND_ERROR,
            shares=None,
            sampled_windows=sampled,
        )
    return _SpectralProfile(
        state=SPECTRAL_MEASURED,
        shares=shares,
        sampled_windows=sampled,
    )


def _validate_pair(
    left: EngineeringSnapshot,
    right: EngineeringSnapshot,
) -> None:
    if not isinstance(left, EngineeringSnapshot) or not isinstance(
        right, EngineeringSnapshot
    ):
        raise TypeError("mix relationship requires EngineeringSnapshot inputs")
    if left.binding.song_id != right.binding.song_id:
        raise MixRelationshipError("relationship Assets must belong to the same Song")
    if left.binding.version_id != right.binding.version_id:
        raise MixRelationshipError(
            "relationship Assets must belong to the exact same Version"
        )
    if left.binding.asset_id == right.binding.asset_id:
        raise MixRelationshipError("relationship requires two distinct Assets")


def _delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    value = float(left) - float(right)
    return value if math.isfinite(value) else None


def analyze_mix_relationship(
    left_path: str | Path,
    right_path: str | Path,
    *,
    left_snapshot: EngineeringSnapshot,
    right_snapshot: EngineeringSnapshot,
) -> MixRelationshipEvidence:
    """Compare two exact current-Version PCM Assets without changing either one.

    Whole-render engineering metrics remain distinct from the sampled spectral
    distribution. Spectral overlap is only a bounded screening cue for where an
    engineer may listen more closely; it is not proof of audible masking and is
    never converted into a mix score, target, preference, or automatic EQ move.
    """

    _validate_pair(left_snapshot, right_snapshot)
    left_profile = _sampled_spectral_profile(
        left_path,
        binding=left_snapshot.binding,
    )
    right_profile = _sampled_spectral_profile(
        right_path,
        binding=right_snapshot.binding,
    )

    overlap = None
    shared_bands: tuple[str, ...] = ()
    if (
        left_profile.state == SPECTRAL_MEASURED
        and right_profile.state == SPECTRAL_MEASURED
        and left_profile.shares is not None
        and right_profile.shares is not None
    ):
        overlap = sum(
            min(left_share, right_share)
            for left_share, right_share in zip(
                left_profile.shares,
                right_profile.shares,
                strict=True,
            )
        )
        overlap = max(0.0, min(1.0, float(overlap)))
        shared_bands = tuple(
            label
            for (label, _, _), left_share, right_share in zip(
                SPECTRAL_BANDS,
                left_profile.shares,
                right_profile.shares,
                strict=True,
            )
            if left_share >= SHARED_BAND_MIN_SHARE
            and right_share >= SHARED_BAND_MIN_SHARE
        )

    loudness_delta = None
    if (
        left_snapshot.loudness_state == LOUDNESS_MEASURED
        and right_snapshot.loudness_state == LOUDNESS_MEASURED
    ):
        loudness_delta = _delta(
            left_snapshot.integrated_lufs,
            right_snapshot.integrated_lufs,
        )

    return MixRelationshipEvidence(
        left_binding=left_snapshot.binding,
        right_binding=right_snapshot.binding,
        analyzer_version=RELATIONSHIP_ANALYZER_VERSION,
        rms_delta_db=_delta(left_snapshot.rms_dbfs, right_snapshot.rms_dbfs),
        integrated_loudness_delta_lu=loudness_delta,
        crest_factor_delta_db=_delta(
            left_snapshot.crest_factor_db,
            right_snapshot.crest_factor_db,
        ),
        left_stereo_correlation=left_snapshot.stereo_correlation,
        right_stereo_correlation=right_snapshot.stereo_correlation,
        spectral_overlap_ratio=overlap,
        shared_spectral_bands=shared_bands,
        left_spectral_state=left_profile.state,
        right_spectral_state=right_profile.state,
        left_sampled_windows=left_profile.sampled_windows,
        right_sampled_windows=right_profile.sampled_windows,
    )
