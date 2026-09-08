from __future__ import annotations

import math
import re
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


def pcm16_stereo_wav() -> bytes:
    frames = [
        (-16384, -16384),
        (-8192, -8192),
        (0, 0),
        (8192, 8192),
        (16384, 16384),
    ]
    data = b"".join(struct.pack("<hh", *frame) for frame in frames)
    fmt = struct.pack("<HHIIHH", 1, 2, 8000, 8000 * 4, 4, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def pcm16_sine_wav(*, amplitude: int = 12000, rate: int = 48000, duration_seconds: float = 1.0) -> bytes:
    frame_count = int(rate * duration_seconds)
    data = bytearray()
    for frame in range(frame_count):
        sample = int(round(amplitude * math.sin(2.0 * math.pi * 1000.0 * frame / rate)))
        data.extend(struct.pack("<hh", sample, sample))
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 4, 4, 16)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + bytes(data)
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def float_wav() -> bytes:
    data = struct.pack("<f", 0.25)
    fmt = struct.pack("<HHIIHH", 3, 1, 48000, 48000 * 4, 4, 32)
    payload = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def seed(data_root: Path, *, payload: bytes, filename: str):
    hq = HeadquartersMemory.create(data_root, "Engineering Artist")
    try:
        song = hq.store.create_song("Engineering Song")
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


def test_current_verified_wav_exposes_read_only_engineering_evidence_without_identity_leaks(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, version_id, asset_id, digest, _ = seed(
        data_root,
        payload=pcm16_stereo_wav(),
        filename="current-mix.wav",
    )

    shell = shell_for(data_root, state_root, pid=9971, token="engineering-current")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>Engineering Snapshot</h2>") == 1
        assert "current-mix.wav" in page
        assert "Exact local signal evidence" in page
        assert "8 kHz" in page
        assert "Stereo" in page
        assert "16-bit integer PCM" in page
        assert "Sample peak" in page
        assert "-6.02 dBFS" in page
        assert "RMS" in page
        assert "-9.03 dBFS" in page
        assert "Integrated loudness" in page
        assert "not measured · shorter than the 400 ms integrated-loudness window" in page
        assert "ITU-R BS.1770-4 programme loudness" in page
        assert "Crest factor" in page
        assert "3.01 dB" in page
        assert "DC offset" in page
        assert "Stereo correlation" in page
        assert "+1.000" in page
        assert "Sample peak is not true peak. RMS is not LUFS." in page
        assert "not a conformance certification, mastering target, mix score, or artistic recommendation" in page
        assert "Measurements can help an engineer inspect the signal; they do not decide whether the music is good or finished." in page
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


def test_normal_song_surface_exposes_finite_bs1770_loudness_for_supported_programme(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed(data_root, payload=pcm16_sine_wav(), filename="loudness-proof.wav")
    shell = shell_for(data_root, state_root, pid=9975, token="engineering-loudness")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert "loudness-proof.wav" in page
        assert "<strong>Integrated loudness</strong>" in page
        match = re.search(r"(-?\d+\.\d{2}) LUFS", page)
        assert match is not None
        measured = float(match.group(1))
        assert math.isfinite(measured)
        assert measured < 0.0
        assert "ITU-R BS.1770-4 programme loudness" in page
        assert "not a conformance certification" in page
        assert "mastering target" in page
        assert "mix score" in page
        assert "artistic recommendation" in page
    finally:
        shell.stop()


def test_unsupported_float_wav_is_truthful_and_does_not_invent_metrics(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed(data_root, payload=float_wav(), filename="float-master.wav")
    shell = shell_for(data_root, state_root, pid=9972, token="engineering-float")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert "<h2>Engineering Snapshot</h2>" in page
        assert "float-master.wav" in page
        assert "This WAV encoding is not yet inside the bounded Engineering Snapshot contract." in page
        assert "N0TE shows no invented substitute measurements." in page
        assert "<strong>Sample peak</strong>" not in page
        assert "<strong>RMS</strong>" not in page
        assert "<strong>Integrated loudness</strong>" not in page
    finally:
        shell.stop()


def test_corrupted_managed_wav_never_surfaces_stale_engineering_numbers(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    _, _, _, _, _, material_path = seed(
        data_root,
        payload=pcm16_stereo_wav(),
        filename="tamper-test.wav",
    )
    material_path.write_bytes(material_path.read_bytes() + b"tampered")

    shell = shell_for(data_root, state_root, pid=9973, token="engineering-corrupt")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert "Protected integrity problem" in page
        assert "<h2>Engineering Snapshot</h2>" not in page
        assert "-6.02 dBFS" not in page
        assert " LUFS" not in page
    finally:
        shell.stop()


def test_engineering_extension_install_is_idempotent(tmp_path: Path) -> None:
    from n0te.audio_engineering_shell import install_song_audio_engineering

    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed(data_root, payload=pcm16_stereo_wav(), filename="idempotent.wav")
    install_song_audio_engineering()
    install_song_audio_engineering()
    shell = shell_for(data_root, state_root, pid=9974, token="engineering-idempotent")
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>Engineering Snapshot</h2>") == 1
    finally:
        shell.stop()
