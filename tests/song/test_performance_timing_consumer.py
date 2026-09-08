from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.platforms import PlatformEnvironment


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process(pid: int, token: str) -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=pid,
        start_token=token,
    )


def get(shell: ConsumerShell, path: str) -> tuple[int, str]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def pcm16_pulse_wav(
    *,
    starts: tuple[float, ...] = (0.20, 0.50, 0.80, 1.10, 1.40, 1.70),
    duration_seconds: float = 2.0,
    rate: int = 8000,
    pulse_seconds: float = 0.020,
    amplitude: int = 20000,
) -> bytes:
    samples = [0] * int(round(duration_seconds * rate))
    width = int(round(pulse_seconds * rate))
    for start_seconds in starts:
        start = int(round(start_seconds * rate))
        for index in range(start, min(len(samples), start + width)):
            samples[index] = amplitude
    data = b"".join(struct.pack("<h", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def pcm16_silence_wav(*, duration_seconds: float = 1.0, rate: int = 8000) -> bytes:
    samples = [0] * int(round(duration_seconds * rate))
    data = b"".join(struct.pack("<h", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def seed(data_root: Path, *, payload: bytes, filename: str):
    hq = HeadquartersMemory.create(data_root, "Performance Artist")
    try:
        song = hq.store.create_song("Performance Song")
        imported = hq.materials.ingest_stream(
            song.id,
            filename=filename,
            stream=BytesIO(payload),
            declared_size=len(payload),
        )
        return (
            hq.store.profile_id,
            song.id,
            imported.version.id,
            imported.asset.id,
            imported.asset.sha256,
            imported.material.path,
        )
    finally:
        hq.close()


def shell_for(data_root: Path, state_root: Path, *, pid: int, token: str) -> ConsumerShell:
    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(pid, token),
        probe=Probe(),
    )
    shell.start()
    return shell


def test_song_exposes_descriptive_timing_evidence_without_grid_claims_or_identity_leaks(tmp_path: Path) -> None:
    from n0te.audio_engineering_shell import install_song_audio_engineering

    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, version_id, asset_id, digest, _ = seed(
        data_root,
        payload=pcm16_pulse_wav(),
        filename="pocket-source.wav",
    )
    install_song_audio_engineering()
    install_song_audio_engineering()

    shell = shell_for(data_root, state_root, pid=9981, token="performance-timing")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>Performance Timing Evidence</h2>") == 1
        assert "pocket-source.wav" in page
        assert "Descriptive timing evidence" in page
        assert "<strong>Energy-change candidates</strong>" in page
        assert ">6</li>" in page
        assert "<strong>Candidate density</strong>" in page
        assert "3.00 candidates/s" in page
        assert "<strong>Median candidate spacing</strong>" in page
        assert "300.0 ms" in page
        assert "<strong>Spacing variability</strong>" in page
        assert "0.0 ms median absolute deviation" in page
        assert "does not grade the performance" in page
        assert "Spacing variability is descriptive, not a quality score" in page
        assert "does not infer BPM, meter, beat phase, early/late, ahead/behind" in page
        assert "swing, tight/sloppy, humanization quality, or needed quantization" in page
        assert "does not authorize timing correction or make an artistic decision" in page
        assert "energy-change candidate is not a beat, note, drum hit" in page
        for forbidden in (
            profile_id,
            song_id,
            version_id,
            asset_id,
            digest,
            "n0te-material://",
            str(data_root),
            "file://",
        ):
            assert forbidden not in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, profile_id)
    try:
        song = reopened.store.get_song(song_id)
        assert song is not None
        assert song.current_version_id == version_id
        assert song.approved_version_id is None
        assert len(reopened.store.versions_for_song(song_id)) == 1
        assert reopened.store.version_asset_ids(version_id) == (asset_id,)
    finally:
        reopened.close()


def test_song_names_silence_without_inventing_spacing_or_tempo(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed(data_root, payload=pcm16_silence_wav(), filename="silent-take.wav")
    shell = shell_for(data_root, state_root, pid=9982, token="performance-silence")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert "<h2>Performance Timing Evidence</h2>" in page
        assert "silent-take.wav" in page
        assert "Digital silence contains no energy-change candidates to describe." in page
        assert "<strong>Median candidate spacing</strong>" not in page
        assert "candidates/s" not in page
        assert "BPM" in page
        assert "does not infer" in page
    finally:
        shell.stop()


def test_tampered_current_material_never_surfaces_stale_timing_statistics(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    _, _, _, _, _, material_path = seed(
        data_root,
        payload=pcm16_pulse_wav(),
        filename="tampered-pocket.wav",
    )
    changed = bytearray(material_path.read_bytes())
    changed[-2:] = struct.pack("<h", 1234)
    material_path.write_bytes(changed)

    shell = shell_for(data_root, state_root, pid=9983, token="performance-tamper")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert "Protected integrity problem" in page
        assert "Performance timing evidence unavailable" in page
        assert "no stale timing statistics" in page
        assert "300.0 ms" not in page
        assert "3.00 candidates/s" not in page
    finally:
        shell.stop()
