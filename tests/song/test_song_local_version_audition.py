from __future__ import annotations

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


@dataclass(frozen=True)
class AudioSource:
    src: str
    label: str


class AudioParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[AudioSource] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag != "audio":
            return
        values = dict(attrs)
        self.sources.append(
            AudioSource(str(values.get("src", "")), str(values.get("aria-label", "")))
        )


def get_text(shell: ConsumerShell, path: str) -> tuple[int, str, dict[str, str]]:
    req = Request(shell.address.origin + path, method="GET")
    try:
        with urlopen(req, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers.items())
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers.items())


def get_bytes(
    shell: ConsumerShell,
    path: str,
    *,
    method: str = "GET",
    range_header: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    headers = {} if range_header is None else {"Range": range_header}
    req = Request(shell.address.origin + path, method=method, headers=headers)
    try:
        with urlopen(req, timeout=2.0) as response:
            return response.status, response.read(), dict(response.headers.items())
    except HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def wav_bytes(payload: bytes = b"\x80\x90\x70\x80\x88\x78\x80\x82") -> bytes:
    fmt = (
        (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (8000).to_bytes(4, "little")
        + (8000).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (8).to_bytes(2, "little")
    )
    body = b"WAVEfmt " + (16).to_bytes(4, "little") + fmt
    body += b"data" + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def seed_audition_history(data_root: Path) -> tuple[str, str, str, bytes]:
    headquarters = HeadquartersMemory.create(data_root, "Audition Artist")
    try:
        song = headquarters.store.create_song("Audition Song")
        external = headquarters.store.attach_asset(
            song.id,
            name="outside.wav",
            sha256="e" * 64,
            source_uri="file:///outside.wav",
        )
        headquarters.store.create_version(
            song.id,
            label="External reference",
            asset_ids=(external.id,),
        )
        audio = wav_bytes()
        wav_import = headquarters.materials.ingest_stream(
            song.id,
            filename="listen-to-me.bin",
            stream=BytesIO(audio),
            declared_size=len(audio),
        )
        midi = b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0"
        headquarters.materials.ingest_stream(
            song.id,
            filename="idea.mid",
            stream=BytesIO(midi),
            declared_size=len(midi),
        )
        return headquarters.store.profile_id, song.id, wav_import.asset.id, audio
    finally:
        headquarters.close()


def test_song_history_exposes_only_verified_supported_local_audio(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, _, _ = seed_audition_history(data_root)

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9401, "audition-surface"),
        probe=Probe(),
    )
    shell.start()
    status, page, headers = get_text(shell, "/song")
    assert status == 200
    assert "media-src 'self'" in headers["Content-Security-Policy"]
    assert "listen-to-me.bin" in page
    assert "idea.mid" in page
    assert "outside.wav" in page
    assert "Local audition is not available for this material format." in page
    assert "External reference" in page
    assert "No loudness matching or A/B processing is applied." in page

    parser = AudioParser()
    parser.feed(page)
    assert len(parser.sources) == 1
    source = parser.sources[0]
    assert source.label == "Audition listen-to-me.bin"
    assert source.src.startswith("/media/song-version/")
    assert "song_" not in source.src
    assert "ver_" not in source.src
    assert "asset_" not in source.src
    assert "prf_" not in page
    assert "n0te-material://" not in page
    assert "file:///outside.wav" not in page
    assert str(data_root) not in page

    headquarters = HeadquartersMemory.open(data_root, profile_id)
    try:
        active = headquarters.store.get_song(song_id)
        assert active is not None
        assert len(headquarters.store.versions_for_song(song_id)) == 3
    finally:
        headquarters.close()
    shell.stop()


def test_media_route_streams_ranges_and_fails_closed_when_binding_changes(tmp_path: Path) -> None:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    profile_id, song_id, wav_asset_id, audio = seed_audition_history(data_root)

    shell = ConsumerShell(
        data_root=data_root,
        state_root=state_root,
        process=process(9402, "audition-range"),
        probe=Probe(),
    )
    shell.start()
    status, page, _ = get_text(shell, "/song")
    assert status == 200
    parser = AudioParser()
    parser.feed(page)
    assert len(parser.sources) == 1
    media_path = parser.sources[0].src

    status, body, headers = get_bytes(shell, media_path)
    assert status == 200
    assert body == audio
    assert headers["Content-Type"] == "audio/wav"
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "media-src 'self'" in headers["Content-Security-Policy"]

    status, body, headers = get_bytes(shell, media_path, method="HEAD")
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) == len(audio)

    status, body, headers = get_bytes(shell, media_path, range_header="bytes=4-11")
    assert status == 206
    assert body == audio[4:12]
    assert headers["Content-Range"] == f"bytes 4-11/{len(audio)}"

    status, body, headers = get_bytes(shell, media_path, range_header="bytes=-5")
    assert status == 206
    assert body == audio[-5:]
    assert headers["Content-Range"] == f"bytes {len(audio)-5}-{len(audio)-1}/{len(audio)}"

    status, _, headers = get_bytes(shell, media_path, range_header=f"bytes={len(audio)}-")
    assert status == 416
    assert headers["Content-Range"] == f"bytes */{len(audio)}"

    status, _, _ = get_text(shell, "/now")
    assert status == 200
    expired, _, _ = get_bytes(shell, media_path)
    assert expired == 409

    status, page, _ = get_text(shell, "/song")
    assert status == 200
    parser = AudioParser()
    parser.feed(page)
    rebound_path = parser.sources[0].src
    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        other = changer.store.create_song("Other Song")
        assert other.id != song_id
    finally:
        changer.close()
    stale_song, _, _ = get_bytes(shell, rebound_path)
    assert stale_song == 409

    changer = HeadquartersMemory.open(data_root, profile_id)
    try:
        changer.store.select_song(song_id)
        asset = changer.store.get_asset(wav_asset_id)
        assert asset is not None
        managed = changer.materials.resolve_asset(asset)
    finally:
        changer.close()
    status, page, _ = get_text(shell, "/song")
    assert status == 200
    parser = AudioParser()
    parser.feed(page)
    tamper_path = parser.sources[0].src
    managed.path.write_bytes(b"tampered")
    tampered, _, _ = get_bytes(shell, tamper_path)
    assert tampered == 409

    shell.stop()
