from __future__ import annotations

import hashlib
import math
import tempfile
import wave
from pathlib import Path

import pytest

from n0te.audio_engineering import (
    CorruptEngineeringMedia,
    EngineeringEvidenceBinding,
    analyze_pcm_wave,
)
from n0te.mix_relationship import (
    RELATIONSHIP_ANALYZER_VERSION,
    SPECTRAL_MEASURED,
    SPECTRAL_SILENT,
    MixRelationshipError,
    analyze_mix_relationship,
)


def _binding(
    path: Path,
    *,
    song: str = "song_one",
    version: str = "ver_one",
    asset: str,
) -> EngineeringEvidenceBinding:
    payload = path.read_bytes()
    return EngineeringEvidenceBinding(
        song_id=song,
        version_id=version,
        asset_id=asset,
        sha256=hashlib.sha256(payload).hexdigest(),
        source_size_bytes=len(payload),
    )


def _write_stereo_sine(
    path: Path,
    *,
    frequency_hz: float,
    amplitude: int = 12000,
    rate: int = 48000,
    duration_seconds: float = 1.0,
    anti_phase: bool = False,
) -> None:
    frames = bytearray()
    frame_count = int(rate * duration_seconds)
    for frame in range(frame_count):
        sample = int(
            round(
                amplitude
                * math.sin(2.0 * math.pi * frequency_hz * frame / float(rate))
            )
        )
        right = -sample if anti_phase else sample
        frames.extend(int(sample).to_bytes(2, "little", signed=True))
        frames.extend(int(right).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(bytes(frames))


def _write_silence(
    path: Path,
    *,
    rate: int = 48000,
    duration_seconds: float = 1.0,
) -> None:
    frame_count = int(rate * duration_seconds)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\x00\x00\x00\x00" * frame_count)


def _snapshot(
    path: Path,
    *,
    asset: str,
    song: str = "song_one",
    version: str = "ver_one",
):
    binding = _binding(
        path,
        song=song,
        version=version,
        asset=asset,
    )
    return analyze_pcm_wave(path, binding=binding)


def test_same_spectrum_level_delta_stays_separate_from_overlap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        louder_path = root / "louder.wav"
        quieter_path = root / "quieter.wav"
        _write_stereo_sine(louder_path, frequency_hz=1000.0, amplitude=12000)
        _write_stereo_sine(quieter_path, frequency_hz=1000.0, amplitude=6000)
        louder = _snapshot(louder_path, asset="asset_louder")
        quieter = _snapshot(quieter_path, asset="asset_quieter")

        result = analyze_mix_relationship(
            louder_path,
            quieter_path,
            left_snapshot=louder,
            right_snapshot=quieter,
        )

        assert result.analyzer_version == RELATIONSHIP_ANALYZER_VERSION
        assert result.evidence_only is True
        assert result.action_authority_granted is False
        assert result.mutation_authorized is False
        assert result.artistic_decision_recorded is False
        assert result.left_spectral_state == SPECTRAL_MEASURED
        assert result.right_spectral_state == SPECTRAL_MEASURED
        assert result.spectral_overlap_ratio is not None
        assert result.spectral_overlap_ratio > 0.99
        assert "Mid" in result.shared_spectral_bands
        assert result.rms_delta_db is not None
        assert result.rms_delta_db == pytest.approx(20.0 * math.log10(2.0), abs=0.05)
        assert result.integrated_loudness_delta_lu is not None
        assert result.integrated_loudness_delta_lu == pytest.approx(
            20.0 * math.log10(2.0),
            abs=0.05,
        )


def test_separated_frequency_regions_report_low_sampled_overlap_without_masking_verdict() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bass_path = root / "bass.wav"
        presence_path = root / "presence.wav"
        _write_stereo_sine(bass_path, frequency_hz=100.0)
        _write_stereo_sine(presence_path, frequency_hz=5000.0)
        bass = _snapshot(bass_path, asset="asset_bass")
        presence = _snapshot(presence_path, asset="asset_presence")

        result = analyze_mix_relationship(
            bass_path,
            presence_path,
            left_snapshot=bass,
            right_snapshot=presence,
        )

        assert result.spectral_overlap_ratio is not None
        assert result.spectral_overlap_ratio < 0.05
        assert result.shared_spectral_bands == ()


def test_anti_phase_stereo_does_not_disappear_from_spectral_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        correlated_path = root / "correlated.wav"
        anti_path = root / "anti.wav"
        _write_stereo_sine(correlated_path, frequency_hz=1000.0)
        _write_stereo_sine(anti_path, frequency_hz=1000.0, anti_phase=True)
        correlated = _snapshot(correlated_path, asset="asset_correlated")
        anti = _snapshot(anti_path, asset="asset_anti")

        result = analyze_mix_relationship(
            correlated_path,
            anti_path,
            left_snapshot=correlated,
            right_snapshot=anti,
        )

        assert result.left_spectral_state == SPECTRAL_MEASURED
        assert result.right_spectral_state == SPECTRAL_MEASURED
        assert result.spectral_overlap_ratio is not None
        assert result.spectral_overlap_ratio > 0.99
        assert result.left_stereo_correlation == pytest.approx(1.0, abs=1e-6)
        assert result.right_stereo_correlation == pytest.approx(-1.0, abs=1e-6)


def test_silent_asset_never_becomes_fabricated_overlap_number() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        audible_path = root / "audible.wav"
        silent_path = root / "silent.wav"
        _write_stereo_sine(audible_path, frequency_hz=1000.0)
        _write_silence(silent_path)
        audible = _snapshot(audible_path, asset="asset_audible")
        silent = _snapshot(silent_path, asset="asset_silent")

        result = analyze_mix_relationship(
            audible_path,
            silent_path,
            left_snapshot=audible,
            right_snapshot=silent,
        )

        assert result.left_spectral_state == SPECTRAL_MEASURED
        assert result.right_spectral_state == SPECTRAL_SILENT
        assert result.spectral_overlap_ratio is None
        assert result.shared_spectral_bands == ()
        assert result.integrated_loudness_delta_lu is None


def test_pair_must_be_two_distinct_assets_on_exact_same_version() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        left_path = root / "left.wav"
        right_path = root / "right.wav"
        _write_stereo_sine(left_path, frequency_hz=500.0)
        _write_stereo_sine(right_path, frequency_hz=750.0)
        left = _snapshot(left_path, asset="asset_left", version="ver_one")
        right_other_version = _snapshot(
            right_path,
            asset="asset_right",
            version="ver_two",
        )

        with pytest.raises(MixRelationshipError, match="exact same Version"):
            analyze_mix_relationship(
                left_path,
                right_path,
                left_snapshot=left,
                right_snapshot=right_other_version,
            )

        same_asset = _snapshot(right_path, asset="asset_left", version="ver_one")
        with pytest.raises(MixRelationshipError, match="distinct Assets"):
            analyze_mix_relationship(
                left_path,
                right_path,
                left_snapshot=left,
                right_snapshot=same_asset,
            )


def test_same_size_tampering_after_snapshot_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        left_path = root / "left.wav"
        right_path = root / "right.wav"
        _write_stereo_sine(left_path, frequency_hz=500.0)
        _write_stereo_sine(right_path, frequency_hz=750.0)
        left = _snapshot(left_path, asset="asset_left")
        right = _snapshot(right_path, asset="asset_right")
        changed = bytearray(right_path.read_bytes())
        changed[-2] ^= 0x01
        right_path.write_bytes(changed)

        with pytest.raises(CorruptEngineeringMedia, match="fingerprint changed"):
            analyze_mix_relationship(
                left_path,
                right_path,
                left_snapshot=left,
                right_snapshot=right,
            )
