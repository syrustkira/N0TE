from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

from n0te.creative_diagnosis import CreativeDiagnosisService
from n0te.memory import HeadquartersMemory


def pcm16_stereo_wav() -> bytes:
    frames = [
        (-16384, -16384),
        (-8192, -4096),
        (0, 0),
        (8192, 4096),
        (16384, 16384),
    ]
    data = b"".join(struct.pack("<hh", *frame) for frame in frames)
    fmt = struct.pack("<HHIIHH", 1, 2, 8000, 8000 * 4, 4, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def float_wav() -> bytes:
    data = struct.pack("<f", 0.25)
    fmt = struct.pack("<HHIIHH", 3, 1, 48000, 48000 * 4, 4, 32)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def seeded_headquarters(root: Path, *, audio: bytes | None = None) -> HeadquartersMemory:
    hq = HeadquartersMemory.create(root, "Diagnosis Artist")
    song = hq.store.create_song("Signal Bloom")
    hq.sessions.start_session(
        song_id=song.id,
        objective="Make the chorus hit harder without changing the vocal melody",
    )
    if audio is not None:
        hq.materials.ingest_stream(
            song.id,
            filename="current-mix.wav",
            stream=BytesIO(audio),
            declared_size=len(audio),
        )
    return hq


def service(hq: HeadquartersMemory) -> CreativeDiagnosisService:
    return CreativeDiagnosisService(hq.store, hq.sessions, hq.materials)


def test_diagnosis_separates_artist_statement_observation_and_hypothesis(tmp_path: Path) -> None:
    hq = seeded_headquarters(tmp_path / "data", audio=pcm16_stereo_wav())
    try:
        before_versions = hq.store.versions_for_song(hq.store.active_song().id)
        before_learning = hq.learning.episodes_for_song(hq.store.active_song().id)

        diagnosis = service(hq).diagnose(
            problem="My chorus feels weak. Give me two ways to make it hit harder without changing the vocal melody."
        )

        assert diagnosis.evidence_status == "OBSERVED_PCM"
        assert diagnosis.has_measured_audio is True
        assert "MELODY" in diagnosis.effective_locks
        assert diagnosis.facts[0].truth_kind == "USER_DECLARED"
        observed = [fact for fact in diagnosis.facts if fact.truth_kind == "OBSERVED"]
        assert {fact.label for fact in observed} >= {"Sample peak", "RMS", "Crest factor"}
        assert all("whole render" in fact.scope.lower() or "current-version wav" in fact.scope.lower() for fact in observed)
        assert len(diagnosis.hypotheses) == 2
        assert all("may" in item.statement.lower() or "hypothesis" in item.statement.lower() for item in diagnosis.hypotheses)
        assert len(diagnosis.interventions) == 2
        assert diagnosis.interventions[0].dimension == "ARRANGEMENT"
        assert diagnosis.interventions[1].dimension == "DYNAMICS"
        assert diagnosis.interventions[0].dimension != diagnosis.interventions[1].dimension
        assert all("MELODY" in path.preserves for path in diagnosis.interventions)
        assert any("subjective weakness" in item for item in diagnosis.limitations)
        assert any("Nothing has been changed yet" in item for item in diagnosis.limitations)

        assert hq.store.versions_for_song(hq.store.active_song().id) == before_versions
        assert hq.learning.episodes_for_song(hq.store.active_song().id) == before_learning
    finally:
        hq.close()


def test_unsupported_audio_never_becomes_invented_signal_evidence(tmp_path: Path) -> None:
    hq = seeded_headquarters(tmp_path / "data", audio=float_wav())
    try:
        diagnosis = service(hq).diagnose()
        assert diagnosis.evidence_status == "NO_SUPPORTED_AUDIO"
        assert diagnosis.has_measured_audio is False
        assert not [fact for fact in diagnosis.facts if fact.truth_kind == "OBSERVED"]
        assert diagnosis.measured_asset_id is None
        assert any("did not invent signal evidence" in item for item in diagnosis.limitations)
    finally:
        hq.close()


def test_corrupted_managed_audio_blocks_measurement_and_does_not_surface_stale_metrics(tmp_path: Path) -> None:
    hq = seeded_headquarters(tmp_path / "data", audio=pcm16_stereo_wav())
    try:
        song = hq.store.active_song()
        version = hq.store.get_version(song.current_version_id)
        view = hq.materials.version_materials(version.id)[0]
        material = hq.materials.resolve_asset(view.asset)
        material.path.write_bytes(material.path.read_bytes() + b"tampered")

        diagnosis = service(hq).diagnose()
        assert diagnosis.evidence_status == "INTEGRITY_BLOCKED"
        assert diagnosis.has_measured_audio is False
        assert not [fact for fact in diagnosis.facts if fact.truth_kind == "OBSERVED"]
        assert diagnosis.measured_asset_sha256 is None
    finally:
        hq.close()


def test_diagnosis_binding_expires_when_session_or_version_changes(tmp_path: Path) -> None:
    hq = seeded_headquarters(tmp_path / "data", audio=pcm16_stereo_wav())
    try:
        diagnosis = service(hq).diagnose()
        assert service(hq).is_current(diagnosis) is True

        song = hq.store.active_song()
        open_session = hq.sessions.latest_for_song(song.id)
        assert open_session is not None
        hq.sessions.close_session(
            open_session.id,
            debrief_summary="Close the original work Session before starting another.",
            next_action="Start the newer work Session.",
        )
        hq.sessions.start_session(song_id=song.id, objective="A newer work Session")
        assert service(hq).is_current(diagnosis) is False

        diagnosis2 = service(hq).diagnose(problem="Test the new Session")
        assert service(hq).is_current(diagnosis2) is True
        payload = pcm16_stereo_wav()
        hq.materials.ingest_stream(
            song.id,
            filename="next-mix.wav",
            stream=BytesIO(payload),
            declared_size=len(payload),
        )
        assert service(hq).is_current(diagnosis2) is False
    finally:
        hq.close()


def test_explicit_checkbox_locks_constrain_both_materially_distinct_paths(tmp_path: Path) -> None:
    hq = seeded_headquarters(tmp_path / "data")
    try:
        diagnosis = service(hq).diagnose(
            problem="The groove feels flat; give me two different tests.",
            locked_dimensions=("MELODY", "HARMONY"),
        )
        assert len(diagnosis.interventions) == 2
        assert diagnosis.interventions[0].dimension == "RHYTHM"
        assert diagnosis.interventions[1].dimension == "ARRANGEMENT"
        assert all(set(path.preserves) == {"MELODY", "HARMONY"} for path in diagnosis.interventions)
        assert all(path.dimension not in {"MELODY", "HARMONY"} for path in diagnosis.interventions)
    finally:
        hq.close()
