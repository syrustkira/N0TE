from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

ANALYZER_VERSION = "AUDIO_ENGINEERING_SNAPSHOT_V3"
LOUDNESS_STANDARD = "ITU-R BS.1770-4"
LOUDNESS_BACKEND = "pyloudnorm 0.2.0"
MIN_LOUDNESS_DURATION_SECONDS = 0.4
MAX_LOUDNESS_SAMPLE_VALUES = 30_000_000
LOUDNESS_MEASURED = "MEASURED"
LOUDNESS_TOO_SHORT = "TOO_SHORT"
LOUDNESS_SILENT = "SILENT"
LOUDNESS_BOUNDED_OUT = "BOUNDED_OUT"
LOUDNESS_UNSUPPORTED_CHANNEL_LAYOUT = "UNSUPPORTED_CHANNEL_LAYOUT"
LOUDNESS_BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
LOUDNESS_BACKEND_ERROR = "BACKEND_ERROR"
MAX_ENGINEERING_BYTES = 256 * 1024 * 1024
_MAX_DATA_CHUNKS = 64
_SUPPORTED_BITS = {8, 16, 24, 32}
_LOUDNESS_SUPPORTED_CHANNELS = {1, 2}


class AudioEngineeringError(RuntimeError):
    """Audio material cannot produce a trustworthy bounded engineering snapshot."""


class UnsupportedEngineeringMedia(AudioEngineeringError):
    """The material is audio, but this analyzer does not truthfully support its encoding."""


class CorruptEngineeringMedia(AudioEngineeringError):
    """The material is malformed or inconsistent with its declared PCM structure."""


@dataclass(frozen=True)
class EngineeringEvidenceBinding:
    song_id: str
    version_id: str
    asset_id: str
    sha256: str
    source_size_bytes: int

    def __post_init__(self) -> None:
        for field in ("song_id", "version_id", "asset_id"):
            value = str(getattr(self, field)).strip()
            if not value:
                raise ValueError(f"{field} must not be empty")
        digest = str(self.sha256).strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("sha256 must be a lowercase 64-character hex digest")
        if isinstance(self.source_size_bytes, bool) or int(self.source_size_bytes) <= 0:
            raise ValueError("source_size_bytes must be positive")


@dataclass(frozen=True)
class EngineeringSnapshot:
    binding: EngineeringEvidenceBinding
    analyzer_version: str
    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    frame_count: int
    duration_seconds: float
    sample_peak_dbfs: float | None
    rms_dbfs: float | None
    crest_factor_db: float | None
    dc_offset_percent: float
    stereo_correlation: float | None
    integrated_lufs: float | None
    loudness_state: str
    loudness_standard: str
    loudness_backend: str

    @property
    def evidence_only(self) -> bool:
        return True


@dataclass(frozen=True)
class _WaveLayout:
    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    block_align: int
    data_chunks: tuple[tuple[int, int], ...]
    frame_count: int


def _u16(value: bytes) -> int:
    if len(value) != 2:
        raise CorruptEngineeringMedia("truncated 16-bit WAV field")
    return int.from_bytes(value, "little", signed=False)


def _u32(value: bytes) -> int:
    if len(value) != 4:
        raise CorruptEngineeringMedia("truncated 32-bit WAV field")
    return int.from_bytes(value, "little", signed=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _inspect_pcm_layout(path: Path, *, size_bytes: int) -> _WaveLayout:
    if size_bytes < 44:
        raise CorruptEngineeringMedia("WAV is too small to contain PCM audio")
    if size_bytes > MAX_ENGINEERING_BYTES:
        raise UnsupportedEngineeringMedia("audio exceeds the bounded engineering-analysis size")

    with path.open("rb") as source:
        header = source.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise UnsupportedEngineeringMedia("engineering snapshot currently supports RIFF/WAVE only")
        riff_end = _u32(header[4:8]) + 8
        if riff_end > size_bytes or riff_end < 12:
            raise CorruptEngineeringMedia("WAV RIFF size is inconsistent with the verified file")

        fmt: tuple[int, int, int, int] | None = None
        data_chunks: list[tuple[int, int]] = []
        while source.tell() + 8 <= riff_end:
            chunk_header = source.read(8)
            if len(chunk_header) != 8:
                raise CorruptEngineeringMedia("WAV chunk header is truncated")
            kind = chunk_header[:4]
            chunk_size = _u32(chunk_header[4:8])
            chunk_start = source.tell()
            chunk_end = chunk_start + chunk_size
            padded_end = chunk_end + (chunk_size & 1)
            if chunk_end > riff_end or padded_end > riff_end:
                raise CorruptEngineeringMedia("WAV chunk extends beyond the declared RIFF payload")

            if kind == b"fmt ":
                if fmt is not None:
                    raise CorruptEngineeringMedia("WAV contains multiple fmt chunks")
                if chunk_size < 16:
                    raise CorruptEngineeringMedia("WAV fmt chunk is too small")
                base = source.read(16)
                if len(base) != 16:
                    raise CorruptEngineeringMedia("WAV fmt chunk is truncated")
                audio_format = _u16(base[0:2])
                channels = _u16(base[2:4])
                sample_rate = _u32(base[4:8])
                byte_rate = _u32(base[8:12])
                block_align = _u16(base[12:14])
                bits = _u16(base[14:16])
                if audio_format != 1:
                    raise UnsupportedEngineeringMedia(
                        "engineering snapshot currently supports integer PCM WAV only"
                    )
                if channels <= 0 or channels > 32:
                    raise UnsupportedEngineeringMedia("WAV channel count is outside the bounded PCM contract")
                if sample_rate <= 0:
                    raise CorruptEngineeringMedia("WAV sample rate is invalid")
                if bits not in _SUPPORTED_BITS:
                    raise UnsupportedEngineeringMedia(
                        "engineering snapshot supports 8, 16, 24 and 32-bit integer PCM"
                    )
                bytes_per_sample = bits // 8
                expected_align = channels * bytes_per_sample
                if block_align != expected_align:
                    raise CorruptEngineeringMedia("WAV block alignment does not match PCM format")
                if byte_rate != sample_rate * block_align:
                    raise CorruptEngineeringMedia("WAV byte rate does not match PCM format")
                fmt = (sample_rate, channels, bits, block_align)
            elif kind == b"data":
                if chunk_size <= 0:
                    raise CorruptEngineeringMedia("WAV data chunk is empty")
                if len(data_chunks) >= _MAX_DATA_CHUNKS:
                    raise UnsupportedEngineeringMedia("WAV contains too many data chunks")
                data_chunks.append((chunk_start, chunk_size))
            source.seek(padded_end)

    if fmt is None:
        raise CorruptEngineeringMedia("WAV has no PCM fmt chunk")
    if not data_chunks:
        raise CorruptEngineeringMedia("WAV has no audio data chunk")
    sample_rate, channels, bits, block_align = fmt
    total_data = sum(size for _, size in data_chunks)
    if any(size % block_align for _, size in data_chunks):
        raise CorruptEngineeringMedia("WAV data size is not aligned to complete PCM frames")
    frame_count = total_data // block_align
    if frame_count <= 0:
        raise CorruptEngineeringMedia("WAV has no complete PCM frames")
    return _WaveLayout(
        sample_rate_hz=sample_rate,
        channels=channels,
        bits_per_sample=bits,
        block_align=block_align,
        data_chunks=tuple(data_chunks),
        frame_count=frame_count,
    )


def _decode_sample(raw: bytes, bits: int) -> float:
    if bits == 8:
        return (raw[0] - 128) / 128.0
    if bits == 24:
        value = int.from_bytes(raw, "little", signed=False)
        if value & 0x800000:
            value -= 1 << 24
        return value / float(1 << 23)
    value = int.from_bytes(raw, "little", signed=True)
    return value / float(1 << (bits - 1))


def _to_dbfs(value: float) -> float | None:
    if value <= 0.0:
        return None
    return 20.0 * math.log10(value)


def _prepare_loudness_buffer(layout: _WaveLayout):
    minimum_frames = math.ceil(layout.sample_rate_hz * MIN_LOUDNESS_DURATION_SECONDS)
    if layout.frame_count < minimum_frames:
        return None, LOUDNESS_TOO_SHORT
    if layout.channels not in _LOUDNESS_SUPPORTED_CHANNELS:
        return None, LOUDNESS_UNSUPPORTED_CHANNEL_LAYOUT
    sample_values = layout.frame_count * layout.channels
    if sample_values > MAX_LOUDNESS_SAMPLE_VALUES:
        return None, LOUDNESS_BOUNDED_OUT
    try:
        import numpy as np
    except ImportError:
        return None, LOUDNESS_BACKEND_UNAVAILABLE
    return np.empty(sample_values, dtype=np.float32), "PENDING"


def _measure_integrated_loudness(buffer, *, layout: _WaveLayout) -> tuple[float | None, str]:
    try:
        import pyloudnorm as pyln
    except ImportError:
        return None, LOUDNESS_BACKEND_UNAVAILABLE
    try:
        samples = buffer if layout.channels == 1 else buffer.reshape((layout.frame_count, layout.channels))
        meter = pyln.Meter(layout.sample_rate_hz)
        measured = float(meter.integrated_loudness(samples))
    except (ValueError, RuntimeError, TypeError, FloatingPointError, OverflowError):
        return None, LOUDNESS_BACKEND_ERROR
    if not math.isfinite(measured):
        return None, LOUDNESS_BACKEND_ERROR
    return measured, LOUDNESS_MEASURED


def analyze_pcm_wave(
    path: str | Path,
    *,
    binding: EngineeringEvidenceBinding,
) -> EngineeringSnapshot:
    """Measure one exact verified PCM WAV without mutating or persisting anything.

    The snapshot reports signal evidence only. Integrated programme loudness uses
    ITU-R BS.1770-4 through the pinned pyloudnorm backend when the material is
    inside this increment's bounded mono/stereo contract. It does not measure
    true peak, certify standards conformance, infer mastering quality, choose a
    loudness target, or make an artistic decision.
    """

    media_path = Path(path)
    if media_path.is_symlink() or not media_path.is_file():
        raise CorruptEngineeringMedia("engineering material is not a safe regular file")
    size_bytes = media_path.stat().st_size
    if size_bytes != binding.source_size_bytes:
        raise CorruptEngineeringMedia("engineering material size changed after lineage verification")
    if _sha256_file(media_path) != binding.sha256:
        raise CorruptEngineeringMedia("engineering material fingerprint changed after lineage verification")

    layout = _inspect_pcm_layout(media_path, size_bytes=size_bytes)
    bytes_per_sample = layout.bits_per_sample // 8
    channels = layout.channels
    sample_count = layout.frame_count * channels
    sums = [0.0] * channels
    sums_sq = [0.0] * channels
    peaks = [0.0] * channels
    stereo_product = 0.0
    frames_seen = 0
    sample_values_seen = 0
    loudness_buffer, loudness_state = _prepare_loudness_buffer(layout)

    with media_path.open("rb") as source:
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
                    raise CorruptEngineeringMedia("WAV audio data ended before its declared size")
                remaining -= len(block)
                for frame_start in range(0, len(block), layout.block_align):
                    values: list[float] = []
                    for channel in range(channels):
                        start = frame_start + channel * bytes_per_sample
                        value = _decode_sample(
                            block[start : start + bytes_per_sample],
                            layout.bits_per_sample,
                        )
                        values.append(value)
                        sums[channel] += value
                        sums_sq[channel] += value * value
                        peaks[channel] = max(peaks[channel], abs(value))
                        if loudness_buffer is not None:
                            loudness_buffer[sample_values_seen] = value
                        sample_values_seen += 1
                    if channels == 2:
                        stereo_product += values[0] * values[1]
                    frames_seen += 1

    if frames_seen != layout.frame_count or sample_values_seen != sample_count:
        raise CorruptEngineeringMedia("PCM frame count changed during engineering analysis")
    if media_path.stat().st_size != binding.source_size_bytes:
        raise CorruptEngineeringMedia("engineering material size changed during engineering analysis")
    if _sha256_file(media_path) != binding.sha256:
        raise CorruptEngineeringMedia("engineering material fingerprint changed during engineering analysis")

    total_sq = sum(sums_sq)
    sample_peak = max(peaks)
    rms = math.sqrt(total_sq / sample_count)
    peak_dbfs = _to_dbfs(sample_peak)
    rms_dbfs = _to_dbfs(rms)
    crest = None
    if sample_peak > 0.0 and rms > 0.0:
        crest = 20.0 * math.log10(sample_peak / rms)
    dc_offset = max(abs(total / layout.frame_count) for total in sums) * 100.0

    correlation = None
    if channels == 2:
        n = float(layout.frame_count)
        left_energy = sums_sq[0] - (sums[0] * sums[0] / n)
        right_energy = sums_sq[1] - (sums[1] * sums[1] / n)
        covariance = stereo_product - (sums[0] * sums[1] / n)
        denominator = math.sqrt(max(0.0, left_energy) * max(0.0, right_energy))
        if denominator > 0.0:
            correlation = max(-1.0, min(1.0, covariance / denominator))

    integrated_lufs = None
    if loudness_buffer is not None:
        if sample_peak <= 0.0:
            loudness_state = LOUDNESS_SILENT
        else:
            integrated_lufs, loudness_state = _measure_integrated_loudness(
                loudness_buffer,
                layout=layout,
            )

    if media_path.stat().st_size != binding.source_size_bytes:
        raise CorruptEngineeringMedia("engineering material size changed before evidence return")
    if _sha256_file(media_path) != binding.sha256:
        raise CorruptEngineeringMedia("engineering material fingerprint changed before evidence return")

    return EngineeringSnapshot(
        binding=binding,
        analyzer_version=ANALYZER_VERSION,
        sample_rate_hz=layout.sample_rate_hz,
        channels=layout.channels,
        bits_per_sample=layout.bits_per_sample,
        frame_count=layout.frame_count,
        duration_seconds=layout.frame_count / float(layout.sample_rate_hz),
        sample_peak_dbfs=peak_dbfs,
        rms_dbfs=rms_dbfs,
        crest_factor_db=crest,
        dc_offset_percent=dc_offset,
        stereo_correlation=correlation,
        integrated_lufs=integrated_lufs,
        loudness_state=loudness_state,
        loudness_standard=LOUDNESS_STANDARD,
        loudness_backend=LOUDNESS_BACKEND,
    )
