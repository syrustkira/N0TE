from __future__ import annotations

import math
import struct
from io import BytesIO
from pathlib import Path

from n0te.audio_engineering import LOUDNESS_MEASURED, LOUDNESS_STANDARD, LOUDNESS_TOO_SHORT
from n0te.memory import HeadquartersMemory
from n0te.version_compare import VersionCompareService


def pcm16_mono_wav(amplitude: int) -> bytes:
    samples = (-amplitude, -(amplitude // 2), 0, amplitude // 2, amplitude)
    data = b"".join(struct.pack("<h", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000 * 2, 2, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def pcm16_mono_loudness_wav(amplitude: int, *, sample_rate: int = 48000) -> bytes:
    samples = (
        int(round(amplitude * math.sin(2.0 * math.pi * 440.0 * frame / sample_rate)))
        for frame in range(sample_rate)
    )
    data = b"".join(struct.pack("<h", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def float_wav() -> bytes:
    data = struct.pack("<ffff", -0.25, 0.0, 0.25, 0.0)
    fmt = struct.pack("<HHIIHH", 3, 1, 8000, 8000 * 4, 4, 32)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def ingest(hq: HeadquartersMemory, song_id: str, name: str, payload: bytes):
    return hq.materials.ingest_stream(
        song_id,
        filename=name,
        stream=BytesIO(payload),
        declared_size=len(payload),
    )


def service(hq: HeadquartersMemory) -> VersionCompareService:
    return VersionCompareService(hq.store, hq.materials)


def test_compare_uses_exact_current_parent_and_keeps_rms_as_fallback_signal_context(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Compare Artist")
    try:
        song = hq.store.create_song("Compare Song")
        first = ingest(hq, song.id, "reference.wav", pcm16_mono_wav(6000))
        second = ingest(hq, song.id, "current.wav", pcm16_mono_wav(12000))
        before_versions = hq.store.versions_for_song(song.id)
        before_learning = hq.learning.episodes_for_song(song.id)
        before_activity = hq.activity.for_song(song.id)
        before_song = hq.store.get_song(song.id)

        comparison = service(hq).prepare()

        assert comparison.status == "READY"
        assert comparison.both_auditionable is True
        assert comparison.current is not None and comparison.reference is not None
        assert comparison.current.version_id == second.version.id
        assert comparison.reference.version_id == first.version.id
        assert comparison.current.status == "AUDITIONABLE_MEASURED"
        assert comparison.reference.status == "AUDITIONABLE_MEASURED"
        assert comparison.current.rms_dbfs is not None
        assert comparison.reference.rms_dbfs is not None
        assert comparison.rms_delta_db is not None
        assert 5.5 < comparison.rms_delta_db < 6.5
        assert comparison.current.integrated_lufs is None
        assert comparison.reference.integrated_lufs is None
        assert comparison.current.loudness_state == LOUDNESS_TOO_SHORT
        assert comparison.reference.loudness_state == LOUDNESS_TOO_SHORT
        assert comparison.current.loudness_standard == LOUDNESS_STANDARD
        assert comparison.integrated_loudness_delta_lu is None
        assert any("RMS is not perceptual loudness" in item for item in comparison.limitations)
        assert any("does not invent a loudness match" in item for item in comparison.limitations)
        assert any("applies no gain" in item for item in comparison.limitations)

        after_song = hq.store.get_song(song.id)
        assert after_song == before_song
        assert hq.store.versions_for_song(song.id) == before_versions
        assert hq.learning.episodes_for_song(song.id) == before_learning
        assert hq.activity.for_song(song.id) == before_activity
    finally:
        hq.close()


def test_compare_prefers_integrated_loudness_difference_when_both_exact_sides_are_measured(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Compare Artist")
    try:
        song = hq.store.create_song("Compare Song")
        reference = ingest(hq, song.id, "reference.wav", pcm16_mono_loudness_wav(6000))
        current = ingest(hq, song.id, "current.wav", pcm16_mono_loudness_wav(12000))
        before_song = hq.store.get_song(song.id)
        before_versions = hq.store.versions_for_song(song.id)
        before_learning = hq.learning.episodes_for_song(song.id)
        before_activity = hq.activity.for_song(song.id)

        comparison = service(hq).prepare()

        assert comparison.current is not None and comparison.reference is not None
        assert comparison.current.version_id == current.version.id
        assert comparison.reference.version_id == reference.version.id
        assert comparison.current.loudness_measured is True
        assert comparison.reference.loudness_measured is True
        assert comparison.current.loudness_state == LOUDNESS_MEASURED
        assert comparison.reference.loudness_state == LOUDNESS_MEASURED
        assert comparison.current.loudness_standard == LOUDNESS_STANDARD
        assert comparison.reference.loudness_standard == LOUDNESS_STANDARD
        assert comparison.current.integrated_lufs is not None
        assert comparison.reference.integrated_lufs is not None
        assert comparison.integrated_loudness_delta_lu is not None
        assert 5.8 < comparison.integrated_loudness_delta_lu < 6.3
        assert comparison.rms_delta_db is not None
        assert 5.8 < comparison.rms_delta_db < 6.3
        assert any("standards-based level evidence only" in item for item in comparison.limitations)

        assert hq.store.get_song(song.id) == before_song
        assert hq.store.versions_for_song(song.id) == before_versions
        assert hq.learning.episodes_for_song(song.id) == before_learning
        assert hq.activity.for_song(song.id) == before_activity
    finally:
        hq.close()


def test_resumed_earliest_version_compares_to_nearest_later_peer(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Compare Artist")
    try:
        song = hq.store.create_song("Compare Song")
        first = ingest(hq, song.id, "first.wav", pcm16_mono_wav(5000))
        second = ingest(hq, song.id, "second.wav", pcm16_mono_wav(7000))
        ingest(hq, song.id, "third.wav", pcm16_mono_wav(9000))
        hq.store.set_current_version(song.id, first.version.id)

        comparison = service(hq).prepare()

        assert comparison.status == "READY"
        assert comparison.current is not None and comparison.reference is not None
        assert comparison.current.version_id == first.version.id
        assert comparison.reference.version_id == second.version.id
        assert comparison.reference.ordinal == 2
    finally:
        hq.close()


def test_auditionable_unmeasured_audio_never_becomes_fake_loudness_evidence(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Compare Artist")
    try:
        song = hq.store.create_song("Compare Song")
        ingest(hq, song.id, "reference.wav", pcm16_mono_wav(6000))
        ingest(hq, song.id, "float-current.wav", float_wav())

        comparison = service(hq).prepare()

        assert comparison.status == "READY"
        assert comparison.current is not None and comparison.reference is not None
        assert comparison.current.auditionable is True
        assert comparison.current.status == "AUDITIONABLE_UNMEASURED"
        assert comparison.current.rms_dbfs is None
        assert comparison.current.integrated_lufs is None
        assert comparison.current.loudness_state is None
        assert comparison.reference.status == "AUDITIONABLE_MEASURED"
        assert comparison.rms_delta_db is None
        assert comparison.integrated_loudness_delta_lu is None
        assert any("does not invent a loudness match" in item for item in comparison.limitations)
    finally:
        hq.close()


def test_corrupt_reference_fails_closed_without_hiding_current_audio(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Compare Artist")
    try:
        song = hq.store.create_song("Compare Song")
        reference = ingest(hq, song.id, "reference.wav", pcm16_mono_wav(6000))
        ingest(hq, song.id, "current.wav", pcm16_mono_wav(9000))
        reference.material.path.write_bytes(reference.material.path.read_bytes() + b"tampered")

        comparison = service(hq).prepare()

        assert comparison.status == "PARTIAL"
        assert comparison.current is not None and comparison.current.auditionable is True
        assert comparison.reference is not None
        assert comparison.reference.status == "INTEGRITY_BLOCKED"
        assert comparison.reference.auditionable is False
        assert comparison.rms_delta_db is None
        assert comparison.integrated_loudness_delta_lu is None
    finally:
        hq.close()


def test_first_version_does_not_create_a_fake_reference(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Compare Artist")
    try:
        song = hq.store.create_song("Compare Song")
        first = ingest(hq, song.id, "only.wav", pcm16_mono_wav(6000))

        comparison = service(hq).prepare()

        assert comparison.status == "NO_REFERENCE_VERSION"
        assert comparison.current is not None
        assert comparison.current.version_id == first.version.id
        assert comparison.reference is None
        assert comparison.reference_version_id is None
        assert comparison.integrated_loudness_delta_lu is None
    finally:
        hq.close()
