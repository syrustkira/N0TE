from pathlib import Path

import pytest

from n0te.audition import (
    InvalidByteRange,
    UnsupportedAuditionMedia,
    UnsatisfiableByteRange,
    inspect_audition_media,
    parse_byte_range,
)


def valid_wav(payload: bytes = b"\x80" * 8) -> bytes:
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


def test_audition_media_requires_coherent_audio_structure_not_extension(tmp_path: Path) -> None:
    wav = tmp_path / "not-audio.txt"
    wav.write_bytes(valid_wav())
    media = inspect_audition_media(wav)
    assert media.content_type == "audio/wav"
    assert media.size_bytes == wav.stat().st_size

    mp3 = tmp_path / "also-not-audio.bin"
    mp3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x64" + b"\x00" * 32)
    media = inspect_audition_media(mp3)
    assert media.content_type == "audio/mpeg"

    bare_id3 = tmp_path / "bare-id3.mp3"
    bare_id3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"not audio")
    with pytest.raises(UnsupportedAuditionMedia):
        inspect_audition_media(bare_id3)

    fake_riff = tmp_path / "fake.wav"
    fake_riff.write_bytes(b"RIFF" + (32).to_bytes(4, "little") + b"WAVE" + b"not real wave audio")
    with pytest.raises(UnsupportedAuditionMedia):
        inspect_audition_media(fake_riff)


def test_single_byte_ranges_are_bounded_and_deterministic() -> None:
    assert parse_byte_range(None, size_bytes=100) is None
    assert parse_byte_range("bytes=0-9", size_bytes=100).content_range == "bytes 0-9/100"
    assert parse_byte_range("bytes=90-", size_bytes=100).content_range == "bytes 90-99/100"
    assert parse_byte_range("bytes=-10", size_bytes=100).content_range == "bytes 90-99/100"
    assert parse_byte_range("bytes=90-500", size_bytes=100).content_range == "bytes 90-99/100"

    with pytest.raises(InvalidByteRange):
        parse_byte_range("items=0-9", size_bytes=100)
    with pytest.raises(InvalidByteRange):
        parse_byte_range("bytes=0-9,20-29", size_bytes=100)
    with pytest.raises(InvalidByteRange):
        parse_byte_range("bytes=9-2", size_bytes=100)
    with pytest.raises(InvalidByteRange):
        parse_byte_range("bytes=-0", size_bytes=100)
    with pytest.raises(UnsatisfiableByteRange):
        parse_byte_range("bytes=100-", size_bytes=100)
