from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from n0te.consumer_shell import ConsumerShell
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.mix_relationship_shell import install_song_mix_relationships
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
    request = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(request, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def pcm16_stereo_sine(
    *,
    frequency_hz: float = 1000.0,
    amplitude: int = 12000,
    rate: int = 48000,
    duration_seconds: float = 1.0,
) -> bytes:
    import struct

    frame_count = int(rate * duration_seconds)
    data = bytearray()
    for frame in range(frame_count):
        sample = int(
            round(
                amplitude
                * math.sin(2.0 * math.pi * frequency_hz * frame / float(rate))
            )
        )
        data.extend(struct.pack("<hh", sample, sample))
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 4, 4, 16)
    payload = (
        b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + bytes(data)
    )
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WAVE" + payload


def seed_pair(data_root: Path):
    hq = HeadquartersMemory.create(data_root, "Relationship Artist")
    try:
        song = hq.store.create_song("Relationship Song")
        lead_payload = pcm16_stereo_sine(amplitude=12000)
        lead = hq.materials.ingest_stream(
            song.id,
            filename="Lead stem.wav",
            stream=BytesIO(lead_payload),
            declared_size=len(lead_payload),
        )
        harmony_payload = pcm16_stereo_sine(amplitude=6000)
        harmony = hq.materials.ingest_stream(
            song.id,
            filename="Harmony stem.wav",
            stream=BytesIO(harmony_payload),
            declared_size=len(harmony_payload),
        )
        pair_version = hq.store.create_version(
            song.id,
            label="Stem relationship view",
            parent_version_id=harmony.version.id,
            asset_ids=(lead.asset.id, harmony.asset.id),
            make_current=True,
        )
        return {
            "profile_id": hq.store.profile_id,
            "song_id": song.id,
            "pair_version_id": pair_version.id,
            "lead_asset_id": lead.asset.id,
            "harmony_asset_id": harmony.asset.id,
            "lead_digest": lead.asset.sha256,
            "harmony_digest": harmony.asset.sha256,
        }
    finally:
        hq.close()


def seed_single(data_root: Path):
    hq = HeadquartersMemory.create(data_root, "Single Asset Artist")
    try:
        song = hq.store.create_song("Single Asset Song")
        payload = pcm16_stereo_sine()
        imported = hq.materials.ingest_stream(
            song.id,
            filename="Only mix.wav",
            stream=BytesIO(payload),
            declared_size=len(payload),
        )
        return hq.store.profile_id, song.id, imported.version.id
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


def test_normal_song_surface_exposes_exact_pair_relationships_without_private_identity_leaks(
    tmp_path: Path,
) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seeded = seed_pair(data_root)

    shell = shell_for(
        data_root,
        state_root,
        pid=12001,
        token="mix-relationship-pair",
    )
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>Engineering Snapshot</h2>") == 1
        assert page.count("<h2>Mix Relationships</h2>") == 1
        assert "Lead stem.wav" in page
        assert "Harmony stem.wav" in page
        assert "Whole-render RMS difference" in page
        assert "6.02 dB RMS" in page
        assert "Lead stem.wav higher" in page
        assert "Integrated loudness difference" in page
        assert "LU integrated loudness" in page
        assert "Dynamics contrast" in page
        assert "Stereo correlation" in page
        assert "Spectral overlap" in page
        assert "sampled broad-band energy-distribution overlap" in page
        assert "Bands carrying at least 10% of each sampled distribution: Mid" in page
        assert "not proof of audible masking" in page
        assert "Stereo correlation is not a width-quality score" in page
        assert (
            "Level and crest differences are not gain, compression, pan, EQ, "
            "mastering, or artistic recommendations"
        ) in page
        assert "not a mixer" in page

        for forbidden in (
            seeded["profile_id"],
            seeded["song_id"],
            seeded["pair_version_id"],
            seeded["lead_asset_id"],
            seeded["harmony_asset_id"],
            seeded["lead_digest"],
            seeded["harmony_digest"],
            "n0te-material://",
            str(data_root),
            "file://",
        ):
            assert forbidden not in page
    finally:
        shell.stop()

    reopened = HeadquartersMemory.open(data_root, seeded["profile_id"])
    try:
        song = reopened.store.get_song(seeded["song_id"])
        assert song is not None
        assert song.current_version_id == seeded["pair_version_id"]
        assert song.approved_version_id is None
        assert len(reopened.store.versions_for_song(seeded["song_id"])) == 3
        assert set(reopened.store.version_asset_ids(seeded["pair_version_id"])) == {
            seeded["lead_asset_id"],
            seeded["harmony_asset_id"],
        }
    finally:
        reopened.close()


def test_single_supported_asset_does_not_invent_relationship_matrix(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed_single(data_root)

    shell = shell_for(
        data_root,
        state_root,
        pid=12002,
        token="mix-relationship-single",
    )
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert "<h2>Engineering Snapshot</h2>" in page
        assert "Only mix.wav" in page
        assert "<h2>Mix Relationships</h2>" not in page
        assert "Spectral overlap" not in page
    finally:
        shell.stop()


def test_mix_relationship_extension_install_is_idempotent(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    seed_pair(data_root)

    install_song_mix_relationships()
    install_song_mix_relationships()
    shell = shell_for(
        data_root,
        state_root,
        pid=12003,
        token="mix-relationship-idempotent",
    )
    try:
        status, page = get(shell, "/song")
        assert status == 200
        assert page.count("<h2>Mix Relationships</h2>") == 1
    finally:
        shell.stop()
