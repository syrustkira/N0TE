from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from html.parser import HTMLParser
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


def get_text(shell: ConsumerShell, path: str) -> tuple[int, str]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def get_bytes(shell: ConsumerShell, path: str) -> tuple[int, bytes]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(req, timeout=2.0) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


@dataclass(frozen=True)
class AudioSource:
    src: str
    label: str


class CompareParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[AudioSource] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "audio":
            self.sources.append(
                AudioSource(str(values.get("src", "")), str(values.get("aria-label", "")))
            )
        elif tag == "a" and values.get("href"):
            self.links.append(str(values["href"]))


def seed(data_root: Path, *, current_payload: bytes | None = None):
    hq = HeadquartersMemory.create(data_root, "Compare Artist")
    song = hq.store.create_song("Compare Song")
    reference_payload = pcm16_mono_wav(6000)
    reference = hq.materials.ingest_stream(
        song.id,
        filename="reference.wav",
        stream=BytesIO(reference_payload),
        declared_size=len(reference_payload),
    )
    current_payload = pcm16_mono_wav(12000) if current_payload is None else current_payload
    current = hq.materials.ingest_stream(
        song.id,
        filename="current.wav",
        stream=BytesIO(current_payload),
        declared_size=len(current_payload),
    )
    return hq, song, reference, current


def seed_loudness_pair(data_root: Path):
    hq = HeadquartersMemory.create(data_root, "Compare Artist")
    song = hq.store.create_song("Compare Song")
    reference_payload = pcm16_mono_loudness_wav(6000)
    reference = hq.materials.ingest_stream(
        song.id,
        filename="reference-loudness.wav",
        stream=BytesIO(reference_payload),
        declared_size=len(reference_payload),
    )
    current_payload = pcm16_mono_loudness_wav(12000)
    current = hq.materials.ingest_stream(
        song.id,
        filename="current-loudness.wav",
        stream=BytesIO(current_payload),
        declared_size=len(current_payload),
    )
    return hq, song, reference, current


def test_song_compare_is_exact_read_only_and_falls_back_to_rms_when_integrated_loudness_is_unavailable(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq, song, reference, current = seed(data_root)
    profile_id = hq.store.profile_id
    versions_before = hq.store.versions_for_song(song.id)
    learning_before = hq.learning.episodes_for_song(song.id)
    activity_before = hq.activity.for_song(song.id)
    song_before = hq.store.get_song(song.id)
    hq.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9701, "version-compare"),
        probe=Probe(),
    )
    shell.start()
    status, song_page = get_text(shell, "/song")
    assert status == 200
    parser = CompareParser()
    parser.feed(song_page)
    assert "/compare" in parser.links
    assert "Open A/B compare" in song_page
    assert "integrated loudness difference" in song_page

    status, page = get_text(shell, "/compare")
    assert status == 200
    assert "Compare Versions" in page
    assert "Reference" in page and "Current" in page
    assert "Integrated programme loudness" in page
    assert "render is too short" in page
    assert "Comparable integrated programme loudness is unavailable for this exact pair" in page
    assert "Use that only as a fallback signal-level cue" in page
    assert "RMS is not LUFS" in page
    assert "N0TE applies no gain, loudness normalization, crossfade, processing or DAW change" in page
    assert "Nothing has been chosen" in page
    assert "KEEP, REVERT, REVISE, or INCONCLUSIVE remains a separate explicit decision step" in page
    assert "Version 1: Imported reference.wav" in page
    assert "Version 2: Imported current.wav" in page
    assert "prf_" not in page and "song_" not in page and "ver_" not in page and "asset_" not in page
    assert "n0te-material://" not in page
    assert str(data_root) not in page

    parser = CompareParser()
    parser.feed(page)
    assert len(parser.sources) == 2
    labels = {source.label for source in parser.sources}
    assert labels == {
        "Audition Version 1: Imported reference.wav",
        "Audition Version 2: Imported current.wav",
    }
    for source in parser.sources:
        assert source.src.startswith("/media/song-version/")
        media_status, body = get_bytes(shell, source.src)
        assert media_status == 200
        assert body in {reference.material.path.read_bytes(), current.material.path.read_bytes()}

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert check.store.get_song(song.id) == song_before
        assert check.store.versions_for_song(song.id) == versions_before
        assert check.learning.episodes_for_song(song.id) == learning_before
        assert check.activity.for_song(song.id) == activity_before
    finally:
        check.close()
    shell.stop()


def test_song_compare_surfaces_integrated_loudness_as_primary_level_bias_cue_when_measured(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq, song, _, _ = seed_loudness_pair(data_root)
    profile_id = hq.store.profile_id
    song_before = hq.store.get_song(song.id)
    versions_before = hq.store.versions_for_song(song.id)
    learning_before = hq.learning.episodes_for_song(song.id)
    activity_before = hq.activity.for_song(song.id)
    hq.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9703, "version-compare-loudness"),
        probe=Probe(),
    )
    shell.start()
    status, page = get_text(shell, "/compare")
    assert status == 200
    assert "Integrated programme loudness" in page
    assert "LUFS" in page
    assert "ITU-R BS.1770-4" in page
    assert "Current Version integrated loudness is" in page
    assert "LU higher than the Reference Version" in page
    assert "Secondary RMS context" in page
    assert "RMS is not LUFS" in page
    assert "N0TE has applied no gain or normalization" in page
    assert "loudness target" not in page.lower()

    check = HeadquartersMemory.open(data_root, profile_id)
    try:
        assert check.store.get_song(song.id) == song_before
        assert check.store.versions_for_song(song.id) == versions_before
        assert check.learning.episodes_for_song(song.id) == learning_before
        assert check.activity.for_song(song.id) == activity_before
    finally:
        check.close()
    shell.stop()


def test_compare_does_not_invent_loudness_match_when_pcm_measurement_is_unavailable(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq, _, _, _ = seed(data_root, current_payload=float_wav())
    hq.close()

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9702, "version-compare-unmeasured"),
        probe=Probe(),
    )
    shell.start()
    status, page = get_text(shell, "/compare")
    assert status == 200
    assert "Verified local audio · engineering evidence unavailable" in page
    assert "No trustworthy pair-level difference is available for this exact pair" in page
    assert "N0TE will not invent integrated loudness, RMS, or a loudness match" in page
    parser = CompareParser()
    parser.feed(page)
    assert len(parser.sources) == 2
    shell.stop()
