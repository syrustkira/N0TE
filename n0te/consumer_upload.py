from __future__ import annotations

import tempfile
from dataclasses import dataclass
from email.message import Message
from typing import BinaryIO

from .material import MAX_MATERIAL_BYTES

MAX_UPLOAD_OVERHEAD_BYTES = 128 * 1024
MAX_UPLOAD_BODY_BYTES = MAX_MATERIAL_BYTES + MAX_UPLOAD_OVERHEAD_BYTES
_MAX_HEADER_LINE = 8192
_MAX_HEADER_BLOCK = 16384
_MAX_TOKEN_BYTES = 4096


class MaterialUploadParseError(RuntimeError):
    """Malformed or unsafe bounded Song-material multipart request."""


class _SliceReader:
    def __init__(self, stream: BinaryIO, start: int, length: int):
        self._stream = stream
        self._start = int(start)
        self._length = int(length)
        self._offset = 0
        self._stream.seek(self._start)

    def read(self, size: int = -1) -> bytes:
        remaining = self._length - self._offset
        if remaining <= 0:
            return b""
        if size is None or int(size) < 0:
            wanted = remaining
        else:
            wanted = min(int(size), remaining)
        data = self._stream.read(wanted)
        if not isinstance(data, bytes):
            raise MaterialUploadParseError("spooled upload returned non-byte data")
        self._offset += len(data)
        if len(data) != wanted:
            raise MaterialUploadParseError("spooled upload ended before the file slice")
        return data


@dataclass
class ParsedMaterialUpload:
    csrf: str
    action: str
    filename: str
    size_bytes: int
    _spool: BinaryIO
    _file_start: int
    _closed: bool = False

    def file_stream(self) -> BinaryIO:
        if self._closed:
            raise MaterialUploadParseError("parsed upload is already closed")
        return _SliceReader(self._spool, self._file_start, self.size_bytes)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._spool.close()

    def __enter__(self) -> "ParsedMaterialUpload":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _content_type_boundary(content_type: str) -> bytes:
    message = Message()
    message["content-type"] = str(content_type)
    if message.get_content_type().lower() != "multipart/form-data":
        raise MaterialUploadParseError("material upload requires multipart/form-data")
    boundary = message.get_param("boundary", header="content-type")
    if not isinstance(boundary, str) or not boundary:
        raise MaterialUploadParseError("multipart boundary is missing")
    if len(boundary) > 70 or "\r" in boundary or "\n" in boundary:
        raise MaterialUploadParseError("multipart boundary is invalid")
    try:
        encoded = boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MaterialUploadParseError("multipart boundary must be ASCII") from exc
    if not encoded or any(byte < 33 or byte > 126 for byte in encoded):
        raise MaterialUploadParseError("multipart boundary contains unsafe bytes")
    return encoded


def _read_headers(stream: BinaryIO) -> dict[str, str]:
    headers: dict[str, str] = {}
    consumed = 0
    while True:
        line = stream.readline(_MAX_HEADER_LINE + 1)
        if not line:
            raise MaterialUploadParseError("multipart headers ended unexpectedly")
        consumed += len(line)
        if len(line) > _MAX_HEADER_LINE or consumed > _MAX_HEADER_BLOCK:
            raise MaterialUploadParseError("multipart headers are too large")
        if line == b"\r\n":
            return headers
        if not line.endswith(b"\r\n") or b":" not in line:
            raise MaterialUploadParseError("multipart header line is malformed")
        raw_name, raw_value = line[:-2].split(b":", 1)
        try:
            name = raw_name.decode("ascii").strip().lower()
            value = raw_value.decode("latin-1").strip()
        except UnicodeDecodeError as exc:
            raise MaterialUploadParseError("multipart header cannot be decoded") from exc
        if not name or name in headers:
            raise MaterialUploadParseError(
                "multipart header name is missing or duplicated"
            )
        headers[name] = value


def _disposition(headers: dict[str, str]) -> tuple[str, str | None]:
    value = headers.get("content-disposition")
    if value is None:
        raise MaterialUploadParseError(
            "multipart part is missing Content-Disposition"
        )
    message = Message()
    message["content-disposition"] = value
    if message.get_content_disposition() != "form-data":
        raise MaterialUploadParseError("multipart part is not form-data")
    name = message.get_param("name", header="content-disposition")
    filename = message.get_filename()
    if not isinstance(name, str) or not name:
        raise MaterialUploadParseError("multipart form field name is missing")
    if filename is not None and not isinstance(filename, str):
        raise MaterialUploadParseError("multipart filename is invalid")
    return name, filename


def _read_token_part(
    stream: BinaryIO,
    *,
    expected_name: str,
    boundary_line: bytes,
) -> str:
    headers = _read_headers(stream)
    if set(headers) != {"content-disposition"}:
        raise MaterialUploadParseError(
            "hidden multipart field has unexpected headers"
        )
    name, filename = _disposition(headers)
    if name != expected_name or filename is not None:
        raise MaterialUploadParseError(
            f"expected hidden multipart field {expected_name}"
        )
    line = stream.readline(_MAX_TOKEN_BYTES + 3)
    if not line.endswith(b"\r\n") or len(line) > _MAX_TOKEN_BYTES + 2:
        raise MaterialUploadParseError("multipart authority token is malformed")
    try:
        value = line[:-2].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaterialUploadParseError(
            "multipart authority token is not UTF-8"
        ) from exc
    if not value:
        raise MaterialUploadParseError("multipart authority token is empty")
    if stream.readline(len(boundary_line) + 2) != boundary_line:
        raise MaterialUploadParseError(
            "multipart field order/boundary is invalid"
        )
    return value


def parse_material_upload(
    stream: BinaryIO,
    *,
    content_type: str,
    content_length: int,
) -> ParsedMaterialUpload:
    if not callable(getattr(stream, "read", None)):
        raise TypeError("upload stream must provide read(size)")
    if isinstance(content_length, bool):
        raise MaterialUploadParseError("upload Content-Length is invalid")
    try:
        body_size = int(content_length)
    except (TypeError, ValueError) as exc:
        raise MaterialUploadParseError("upload Content-Length is invalid") from exc
    if body_size <= 0 or body_size > MAX_UPLOAD_BODY_BYTES:
        raise MaterialUploadParseError(
            "upload request size is outside the allowed bound"
        )

    boundary = _content_type_boundary(content_type)
    normal_boundary = b"--" + boundary + b"\r\n"
    final_trailer = b"\r\n--" + boundary + b"--\r\n"
    final_trailer_no_crlf = b"\r\n--" + boundary + b"--"
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    try:
        remaining = body_size
        while remaining:
            requested = min(64 * 1024, remaining)
            chunk = stream.read(requested)
            if not isinstance(chunk, bytes) or not chunk:
                raise MaterialUploadParseError(
                    "upload body ended before Content-Length"
                )
            if len(chunk) > requested:
                raise MaterialUploadParseError(
                    "upload stream returned more bytes than requested"
                )
            spool.write(chunk)
            remaining -= len(chunk)
        spool.flush()
        spool.seek(0)
        if spool.readline(len(normal_boundary) + 2) != normal_boundary:
            raise MaterialUploadParseError(
                "multipart opening boundary is invalid"
            )

        csrf = _read_token_part(
            spool,
            expected_name="csrf",
            boundary_line=normal_boundary,
        )
        action = _read_token_part(
            spool,
            expected_name="action",
            boundary_line=normal_boundary,
        )

        file_headers = _read_headers(spool)
        if not set(file_headers).issubset(
            {"content-disposition", "content-type"}
        ):
            raise MaterialUploadParseError(
                "material part has unexpected headers"
            )
        name, filename = _disposition(file_headers)
        if name != "material" or filename is None or not filename:
            raise MaterialUploadParseError("multipart material file is missing")
        file_start = spool.tell()

        trailer_length = None
        for trailer in (final_trailer, final_trailer_no_crlf):
            if body_size < len(trailer):
                continue
            spool.seek(body_size - len(trailer))
            if spool.read(len(trailer)) == trailer:
                trailer_length = len(trailer)
                break
        if trailer_length is None:
            raise MaterialUploadParseError(
                "multipart final boundary is invalid"
            )
        file_end = body_size - trailer_length
        size = file_end - file_start
        if size <= 0:
            raise MaterialUploadParseError("material file is empty")
        if size > MAX_MATERIAL_BYTES:
            raise MaterialUploadParseError(
                "material file exceeds the local ingest limit"
            )

        return ParsedMaterialUpload(
            csrf=csrf,
            action=action,
            filename=filename,
            size_bytes=size,
            _spool=spool,
            _file_start=file_start,
        )
    except Exception:
        spool.close()
        raise
