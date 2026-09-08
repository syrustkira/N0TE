from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

import pytest

from n0te.memory import HeadquartersMemory
from n0te.version_compare import VersionCompareService
from n0te.version_compare_decision import (
    StaleVersionCompareDecisionError,
    VersionCompareDecisionBinding,
    VersionCompareDecisionMemory,
)


def pcm16_mono_wav(amplitude: int) -> bytes:
    samples = (-amplitude, -(amplitude // 2), 0, amplitude // 2, amplitude)
    data = b"".join(struct.pack("<h", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000 * 2, 2, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def ingest(hq: HeadquartersMemory, song_id: str, name: str, amplitude: int):
    payload = pcm16_mono_wav(amplitude)
    return hq.materials.ingest_stream(
        song_id,
        filename=name,
        stream=BytesIO(payload),
        declared_size=len(payload),
    )


def binding_for(hq: HeadquartersMemory) -> VersionCompareDecisionBinding:
    comparison = VersionCompareService(hq.store, hq.materials).prepare()
    assert comparison.reference is not None and comparison.current is not None
    return VersionCompareDecisionBinding(
        comparison.song_id,
        comparison.reference.version_id,
        comparison.current.version_id,
    )


def test_read_only_inspection_does_not_create_decision_schema(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Decision Artist")
    try:
        song = hq.store.create_song("Decision Song")
        ingest(hq, song.id, "reference.wav", 6000)
        ingest(hq, song.id, "current.wav", 9000)

        before_tables = tuple(
            str(row["name"])
            for row in hq.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        before_metadata = tuple(
            (str(row["key"]), str(row["value"]))
            for row in hq.store._conn.execute("SELECT key,value FROM metadata ORDER BY key")
        )

        memory = VersionCompareDecisionMemory(hq.store, create=False)

        assert memory.initialized is False
        assert memory.latest_for_pair(song.id, "missing-a", "missing-b") is None
        assert tuple(
            str(row["name"])
            for row in hq.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ) == before_tables
        assert tuple(
            (str(row["key"]), str(row["value"]))
            for row in hq.store._conn.execute("SELECT key,value FROM metadata ORDER BY key")
        ) == before_metadata
    finally:
        hq.close()


def test_exact_pair_decision_is_append_only_judgment_not_execution(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Decision Artist")
    try:
        song = hq.store.create_song("Decision Song")
        reference = ingest(hq, song.id, "reference.wav", 6000)
        current = ingest(hq, song.id, "current.wav", 9000)
        before_song = hq.store.get_song(song.id)
        before_versions = hq.store.versions_for_song(song.id)
        before_learning = hq.learning.episodes_for_song(song.id)
        before_activity = hq.activity.checkpoint()

        memory = VersionCompareDecisionMemory(hq.store, create=True)
        result = memory.record(
            binding_for(hq),
            decision="REVERT",
            rationale="The reference leaves more room for the vocal.",
        )

        assert result.decision == "REVERT"
        assert result.reference_version_id == reference.version.id
        assert result.current_version_id == current.version.id
        assert result.rationale == "The reference leaves more room for the vocal."
        assert hq.store.get_song(song.id) == before_song
        assert hq.store.versions_for_song(song.id) == before_versions
        assert hq.learning.episodes_for_song(song.id) == before_learning
        assert hq.store.get_song(song.id).current_version_id == current.version.id
        assert hq.store.get_song(song.id).approved_version_id is None

        events = hq.activity.for_song(song.id, after_sequence=before_activity)
        assert len(events) == 1
        assert events[0].event_type == "VERSION_COMPARE_DECISION_RECORDED"
        assert events[0].version_id == current.version.id
        assert events[0].payload == {"decision": "REVERT"}
    finally:
        hq.close()


def test_same_exact_pair_can_preserve_changed_artist_judgment_history(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Decision Artist")
    try:
        song = hq.store.create_song("Decision Song")
        ingest(hq, song.id, "reference.wav", 6000)
        ingest(hq, song.id, "current.wav", 9000)
        binding = binding_for(hq)
        memory = VersionCompareDecisionMemory(hq.store, create=True)

        first = memory.record(binding, decision="INCONCLUSIVE")
        second = memory.record(
            binding,
            decision="KEEP",
            rationale="After another listen, Current supports the hook better.",
        )

        history = memory.decisions_for_song(song.id)
        assert [item.id for item in history] == [first.id, second.id]
        assert [item.decision for item in history] == ["INCONCLUSIVE", "KEEP"]
        assert memory.latest_for_pair(
            binding.song_id,
            binding.reference_version_id,
            binding.current_version_id,
        ) == second
    finally:
        hq.close()


def test_stale_current_version_rejects_old_pair_decision(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Decision Artist")
    try:
        song = hq.store.create_song("Decision Song")
        reference = ingest(hq, song.id, "reference.wav", 6000)
        ingest(hq, song.id, "current.wav", 9000)
        binding = binding_for(hq)
        memory = VersionCompareDecisionMemory(hq.store, create=True)
        hq.store.set_current_version(song.id, reference.version.id)

        with pytest.raises(StaleVersionCompareDecisionError):
            memory.record(binding, decision="KEEP")

        assert memory.decisions_for_song(song.id) == ()
    finally:
        hq.close()


def test_foreign_song_version_cannot_enter_pair_memory(tmp_path: Path) -> None:
    hq = HeadquartersMemory.create(tmp_path / "data", "Decision Artist")
    try:
        song = hq.store.create_song("Decision Song")
        ingest(hq, song.id, "reference.wav", 6000)
        current = ingest(hq, song.id, "current.wav", 9000)
        other = hq.store.create_song("Other Song")
        foreign = ingest(hq, other.id, "foreign.wav", 7000)
        hq.store.select_song(song.id)
        memory = VersionCompareDecisionMemory(hq.store, create=True)

        with pytest.raises(StaleVersionCompareDecisionError):
            memory.record(
                VersionCompareDecisionBinding(song.id, foreign.version.id, current.version.id),
                decision="REVISE",
            )

        assert memory.decisions_for_song(song.id) == ()
    finally:
        hq.close()
