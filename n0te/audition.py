from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AuditionError(RuntimeError):
    """Local media cannot be auditioned under the bounded consumer contract."""


class UnsupportedAuditionMedia(AuditionError):
    """The verified material is not browser-auditionable under this bounded contract."""


class InvalidByteRange(AuditionError):
    """A Range header is malformed or unsupported."""


class UnsatisfiableByteRange(AuditionError):
    """A syntactically valid Range cannot be satisfied for this representation."""


@dataclass(frozen=True)
class AuditionMedia:
    path: Path
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"


def _little_u32(value: bytes) -> int:
    if len(value) != 4:
        raise ValueError("expected four bytes")
    return int.from_bytes(value, "little", signed=False)


def _valid_wave(path: Path, *, size_bytes: int) -> bool:
    if size_bytes < 44:
        return False
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return False
        riff_payload_size = _little_u32(header[4:8])
        if riff_payload_size + 8 > size_bytes:
            return False
        have_fmt = False
        have_data = False
        while handle.tell() + 8 <= size_bytes:
            chunk_header = handle.read(8)
            if len(chunk_header) != 8:
                return False
            kind = chunk_header[:4]
            chunk_size = _little_u32(chunk_header[4:])
            chunk_start = handle.tell()
            chunk_end = chunk_start + chunk_size
            padded_end = chunk_end + (chunk_size & 1)
            if chunk_end > size_bytes or padded_end > size_bytes:
                return False
            if kind == b"fmt ":
                if chunk_size < 16:
                    return False
                fmt = handle.read(16)
                if len(fmt) != 16:
                    return False
                audio_format = int.from_bytes(fmt[0:2], "little")
                channels = int.from_bytes(fmt[2:4], "little")
                sample_rate = _little_u32(fmt[4:8])
                byte_rate = _little_u32(fmt[8:12])
                block_align = int.from_bytes(fmt[12:14], "little")
                bits_per_sample = int.from_bytes(fmt[14:16], "little")
                if (
                    audio_format not in {1, 3}
                    or channels <= 0
                    or sample_rate <= 0
                    or byte_rate <= 0
                    or block_align <= 0
                    or bits_per_sample <= 0
                ):
                    return False
                have_fmt = True
            elif kind == b"data":
                if chunk_size <= 0:
                    return False
                have_data = True
            handle.seek(padded_end)
        return have_fmt and have_data


def _synchsafe_u32(value: bytes) -> int | None:
    if len(value) != 4 or any(byte & 0x80 for byte in value):
        return None
    result = 0
    for byte in value:
        result = (result << 7) | byte
    return result


def _valid_mpeg_audio_header(header: bytes) -> bool:
    if len(header) < 4 or header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
        return False
    version_bits = (header[1] >> 3) & 0x03
    layer_bits = (header[1] >> 1) & 0x03
    bitrate_index = (header[2] >> 4) & 0x0F
    sample_rate_index = (header[2] >> 2) & 0x03
    return (
        version_bits != 0x01
        and layer_bits != 0x00
        and bitrate_index not in {0x00, 0x0F}
        and sample_rate_index != 0x03
    )


def _valid_mp3(path: Path, *, size_bytes: int) -> bool:
    if size_bytes < 4:
        return False
    with path.open("rb") as handle:
        prefix = handle.read(10)
        offset = 0
        if prefix.startswith(b"ID3"):
            if len(prefix) != 10 or prefix[3] == 0xFF or prefix[4] == 0xFF:
                return False
            tag_size = _synchsafe_u32(prefix[6:10])
            if tag_size is None:
                return False
            offset = 10 + tag_size
            if prefix[5] & 0x10:
                offset += 10
            if offset + 4 > size_bytes:
                return False
        handle.seek(offset)
        scan = handle.read(min(4096, size_bytes - offset))
    for index in range(max(0, len(scan) - 3)):
        if _valid_mpeg_audio_header(scan[index : index + 4]):
            return True
    return False


def inspect_audition_media(path: str | Path) -> AuditionMedia:
    media_path = Path(path)
    if not media_path.is_file():
        raise UnsupportedAuditionMedia("audition material is not a regular local file")
    size = media_path.stat().st_size
    if size <= 0:
        raise UnsupportedAuditionMedia("empty material is not auditionable")
    if _valid_wave(media_path, size_bytes=size):
        content_type = "audio/wav"
    elif _valid_mp3(media_path, size_bytes=size):
        content_type = "audio/mpeg"
    else:
        raise UnsupportedAuditionMedia(
            "only structurally verified WAV and MP3 material is auditionable here"
        )
    return AuditionMedia(path=media_path, content_type=content_type, size_bytes=size)


def parse_byte_range(value: str | None, *, size_bytes: int) -> ByteRange | None:
    if size_bytes <= 0:
        raise ValueError("size_bytes must be positive")
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not text.startswith("bytes="):
        raise InvalidByteRange("only byte ranges are supported")
    spec = text[6:].strip()
    if not spec or "," in spec:
        raise InvalidByteRange("exactly one byte range is supported")
    if "-" not in spec:
        raise InvalidByteRange("byte range is missing a dash")
    first, last = (part.strip() for part in spec.split("-", 1))
    if not first:
        if not last.isdigit():
            raise InvalidByteRange("suffix byte range is malformed")
        suffix = int(last)
        if suffix <= 0:
            raise InvalidByteRange("suffix byte range must be positive")
        length = min(suffix, size_bytes)
        return ByteRange(size_bytes - length, size_bytes - 1, size_bytes)
    if not first.isdigit():
        raise InvalidByteRange("byte range start is malformed")
    start = int(first)
    if start >= size_bytes:
        raise UnsatisfiableByteRange("byte range starts beyond the representation")
    if not last:
        return ByteRange(start, size_bytes - 1, size_bytes)
    if not last.isdigit():
        raise InvalidByteRange("byte range end is malformed")
    end = int(last)
    if end < start:
        raise InvalidByteRange("byte range end precedes start")
    return ByteRange(start, min(end, size_bytes - 1), size_bytes)
