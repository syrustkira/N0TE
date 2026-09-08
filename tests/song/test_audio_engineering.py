from __future__ import annotations

import hashlib
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from n0te.audio_engineering import (
    ANALYZER_VERSION,
    LOUDNESS_BACKEND,
    LOUDNESS_MEASURED,
    LOUDNESS_SILENT,
    LOUDNESS_STANDARD,
    LOUDNESS_TOO_SHORT,
    CorruptEngineeringMedia,
    EngineeringEvidenceBinding,
    UnsupportedEngineeringMedia,
    analyze_pcm_wave,
)


def _binding(path: Path, *, song: str = "song_one", version: str = "ver_one", asset: str = "asset_one") -> EngineeringEvidenceBinding:
    payload = path.read_bytes()
    return EngineeringEvidenceBinding(
        song_id=song,
        version_id=version,
        asset_id=asset,
        sha256=hashlib.sha256(payload).hexdigest(),
        source_size_bytes=len(payload),
    )


def _write_pcm16(path: Path, frames: list[tuple[int, ...]], *, rate: int = 8000) -> None:
    channels = len(frames[0])
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(
            b"".join(struct.pack("<" + "h" * channels, *frame) for frame in frames)
        )


def _write_sine_pcm16(
    path: Path,
    *,
    amplitude: int,
    rate: int = 48000,
    duration_seconds: float = 1.0,
    channels: int = 2,
    frequency_hz: float = 1000.0,
) -> None:
    frame_count = int(rate * duration_seconds)
    frames: list[tuple[int, ...]] = []
    for frame in range(frame_count):
        sample = int(round(amplitude * math.sin(2.0 * math.pi * frequency_hz * frame / rate)))
        frames.append(tuple(sample for _ in range(channels)))
    _write_pcm16(path, frames, rate=rate)


def _encode_signed(value: int, bits: int) -> bytes:
    width = bits // 8
    if value < 0:
        value += 1 << bits
    return int(value).to_bytes(width, "little", signed=False)


def _write_pcm24(path: Path, samples: list[int], *, rate: int = 48000) -> None:
    data = b"".join(_encode_signed(value, 24) for value in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 3, 3, 24)
    data_chunk = b"data" + struct.pack("<I", len(data)) + data
    if len(data) & 1:
        data_chunk += b"\x00"
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + data_chunk
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload)


def _write_float_wav_header(path: Path) -> None:
    data = struct.pack("<f", 0.25)
    fmt = struct.pack("<HHIIHH", 3, 1, 48000, 48000 * 4, 4, 32)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload)


class AudioEngineeringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stereo_snapshot_has_exact_metrics_and_evidence_binding(self) -> None:
        path = self.root / "correlated.wav"
        frames = [
            (-16384, -16384),
            (-8192, -8192),
            (0, 0),
            (8192, 8192),
            (16384, 16384),
        ]
        _write_pcm16(path, frames)
        binding = _binding(path)
        before = path.read_bytes()

        result = analyze_pcm_wave(path, binding=binding)

        self.assertEqual(result.binding, binding)
        self.assertEqual(result.analyzer_version, ANALYZER_VERSION)
        self.assertTrue(result.evidence_only)
        self.assertEqual(result.sample_rate_hz, 8000)
        self.assertEqual(result.channels, 2)
        self.assertEqual(result.bits_per_sample, 16)
        self.assertEqual(result.frame_count, 5)
        self.assertAlmostEqual(result.duration_seconds, 5 / 8000)
        self.assertAlmostEqual(result.sample_peak_dbfs or 0.0, -6.020599913, places=6)
        self.assertAlmostEqual(result.rms_dbfs or 0.0, -9.03089987, places=6)
        self.assertAlmostEqual(result.crest_factor_db or 0.0, 3.010299957, places=6)
        self.assertAlmostEqual(result.dc_offset_percent, 0.0, places=12)
        self.assertAlmostEqual(result.stereo_correlation or 0.0, 1.0, places=12)
        self.assertIsNone(result.integrated_lufs)
        self.assertEqual(result.loudness_state, LOUDNESS_TOO_SHORT)
        self.assertEqual(result.loudness_standard, LOUDNESS_STANDARD)
        self.assertEqual(result.loudness_backend, LOUDNESS_BACKEND)
        self.assertEqual(path.read_bytes(), before)

    def test_integrated_loudness_tracks_known_level_delta_for_same_signal(self) -> None:
        louder = self.root / "louder.wav"
        quieter = self.root / "quieter.wav"
        _write_sine_pcm16(louder, amplitude=12000)
        _write_sine_pcm16(quieter, amplitude=6000)

        loud = analyze_pcm_wave(louder, binding=_binding(louder, asset="louder"))
        quiet = analyze_pcm_wave(quieter, binding=_binding(quieter, asset="quieter"))

        self.assertEqual(loud.loudness_state, LOUDNESS_MEASURED)
        self.assertEqual(quiet.loudness_state, LOUDNESS_MEASURED)
        self.assertIsNotNone(loud.integrated_lufs)
        self.assertIsNotNone(quiet.integrated_lufs)
        assert loud.integrated_lufs is not None
        assert quiet.integrated_lufs is not None
        self.assertTrue(math.isfinite(loud.integrated_lufs))
        self.assertTrue(math.isfinite(quiet.integrated_lufs))
        self.assertGreater(loud.integrated_lufs, quiet.integrated_lufs)
        self.assertAlmostEqual(
            loud.integrated_lufs - quiet.integrated_lufs,
            20.0 * math.log10(2.0),
            delta=0.05,
        )

    def test_long_silence_is_named_without_inventing_finite_lufs(self) -> None:
        path = self.root / "silence.wav"
        _write_pcm16(path, [(0,)] * 48000, rate=48000)
        result = analyze_pcm_wave(path, binding=_binding(path))
        self.assertEqual(result.loudness_state, LOUDNESS_SILENT)
        self.assertIsNone(result.integrated_lufs)
        self.assertIsNone(result.sample_peak_dbfs)
        self.assertIsNone(result.rms_dbfs)

    def test_stereo_anticorrelation_is_measured_without_judgment(self) -> None:
        path = self.root / "anti.wav"
        _write_pcm16(
            path,
            [(-16384, 16384), (-8192, 8192), (8192, -8192), (16384, -16384)],
        )
        result = analyze_pcm_wave(path, binding=_binding(path))
        self.assertAlmostEqual(result.stereo_correlation or 0.0, -1.0, places=12)
        self.assertEqual(result.loudness_state, LOUDNESS_TOO_SHORT)

    def test_24_bit_integer_pcm_is_supported(self) -> None:
        path = self.root / "pcm24.wav"
        _write_pcm24(path, [-(1 << 22), 0, 1 << 22])
        result = analyze_pcm_wave(path, binding=_binding(path))
        self.assertEqual(result.bits_per_sample, 24)
        self.assertEqual(result.channels, 1)
        self.assertAlmostEqual(result.sample_peak_dbfs or 0.0, -6.020599913, places=6)
        self.assertIsNone(result.stereo_correlation)
        self.assertEqual(result.loudness_state, LOUDNESS_TOO_SHORT)

    def test_float_wav_is_rejected_instead_of_misreported_as_pcm(self) -> None:
        path = self.root / "float.wav"
        _write_float_wav_header(path)
        with self.assertRaisesRegex(UnsupportedEngineeringMedia, "integer PCM"):
            analyze_pcm_wave(path, binding=_binding(path))

    def test_size_change_after_binding_fails_closed(self) -> None:
        path = self.root / "changed.wav"
        _write_pcm16(path, [(0,), (1000,)])
        binding = _binding(path)
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaisesRegex(CorruptEngineeringMedia, "size changed"):
            analyze_pcm_wave(path, binding=binding)

    def test_same_size_fingerprint_change_after_binding_fails_closed(self) -> None:
        path = self.root / "same-size-changed.wav"
        _write_pcm16(path, [(0,), (1000,), (-1000,)])
        binding = _binding(path)
        changed = bytearray(path.read_bytes())
        changed[-2:] = struct.pack("<h", 2000)
        self.assertEqual(len(changed), binding.source_size_bytes)
        path.write_bytes(changed)
        with self.assertRaisesRegex(CorruptEngineeringMedia, "fingerprint changed"):
            analyze_pcm_wave(path, binding=binding)

    def test_misaligned_pcm_data_is_rejected(self) -> None:
        path = self.root / "bad.wav"
        fmt = struct.pack("<HHIIHH", 1, 2, 48000, 48000 * 4, 4, 16)
        data = b"\x00\x00"
        payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
        path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload)
        with self.assertRaisesRegex(CorruptEngineeringMedia, "complete PCM frames"):
            analyze_pcm_wave(path, binding=_binding(path))

    def test_binding_rejects_fake_or_empty_identity(self) -> None:
        with self.assertRaises(ValueError):
            EngineeringEvidenceBinding("", "ver", "asset", "0" * 64, 1)
        with self.assertRaises(ValueError):
            EngineeringEvidenceBinding("song", "ver", "asset", "not-a-sha", 1)
        with self.assertRaises(ValueError):
            EngineeringEvidenceBinding("song", "ver", "asset", "0" * 64, 0)


if __name__ == "__main__":
    unittest.main()
