import io
import unittest

from n0te.consumer_upload import (
    MAX_UPLOAD_BODY_BYTES,
    MaterialUploadParseError,
    parse_material_upload,
)


BOUNDARY = "N0TEBoundary7f32e951"


def multipart_body(
    payload=b"song-bytes",
    *,
    csrf="csrf-token",
    action="action-token",
    filename="demo.wav",
    order=("csrf", "action", "material"),
    material_extra_header=None,
):
    chunks = []
    for field in order:
        chunks.append(f"--{BOUNDARY}\r\n".encode())
        if field == "csrf":
            chunks.append(b'Content-Disposition: form-data; name="csrf"\r\n\r\n')
            chunks.append(csrf.encode("utf-8") + b"\r\n")
        elif field == "action":
            chunks.append(b'Content-Disposition: form-data; name="action"\r\n\r\n')
            chunks.append(action.encode("utf-8") + b"\r\n")
        elif field == "material":
            chunks.append(
                f'Content-Disposition: form-data; name="material"; filename="{filename}"\r\n'.encode(
                    "latin-1"
                )
            )
            chunks.append(b"Content-Type: application/octet-stream\r\n")
            if material_extra_header is not None:
                chunks.append(material_extra_header + b"\r\n")
            chunks.append(b"\r\n")
            chunks.append(payload)
            chunks.append(b"\r\n")
        else:
            raise AssertionError(field)
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


class _OverreadStream:
    def __init__(self, payload):
        self.payload = payload
        self.offset = 0

    def read(self, size=-1):
        if self.offset >= len(self.payload):
            return b""
        wanted = len(self.payload) - self.offset if size < 0 else size + 1
        data = self.payload[self.offset : self.offset + wanted]
        self.offset += len(data)
        return data


class ConsumerMaterialUploadParserTests(unittest.TestCase):
    def parse(self, body):
        return parse_material_upload(
            io.BytesIO(body),
            content_type=f"multipart/form-data; boundary={BOUNDARY}",
            content_length=len(body),
        )

    def test_exact_form_extracts_authority_and_only_file_slice(self):
        payload = b"prefix\r\n--N0TEBoundary7f32e951--not-final\x00suffix"
        body = multipart_body(payload)
        with self.parse(body) as parsed:
            self.assertEqual(parsed.csrf, "csrf-token")
            self.assertEqual(parsed.action, "action-token")
            self.assertEqual(parsed.filename, "demo.wav")
            self.assertEqual(parsed.size_bytes, len(payload))
            stream = parsed.file_stream()
            self.assertEqual(stream.read(5), payload[:5])
            self.assertEqual(stream.read(), payload[5:])
            self.assertEqual(stream.read(), b"")

    def test_hidden_field_order_is_part_of_the_narrow_contract(self):
        body = multipart_body(order=("action", "csrf", "material"))
        with self.assertRaises(MaterialUploadParseError):
            self.parse(body)

    def test_unexpected_material_header_is_rejected(self):
        body = multipart_body(material_extra_header=b"Content-Transfer-Encoding: base64")
        with self.assertRaises(MaterialUploadParseError):
            self.parse(body)

    def test_empty_file_is_rejected(self):
        with self.assertRaises(MaterialUploadParseError):
            self.parse(multipart_body(b""))

    def test_truncated_body_is_rejected_against_content_length(self):
        body = multipart_body()
        with self.assertRaises(MaterialUploadParseError):
            parse_material_upload(
                io.BytesIO(body[:-7]),
                content_type=f"multipart/form-data; boundary={BOUNDARY}",
                content_length=len(body),
            )

    def test_request_bound_is_checked_before_body_read(self):
        with self.assertRaises(MaterialUploadParseError):
            parse_material_upload(
                io.BytesIO(b""),
                content_type=f"multipart/form-data; boundary={BOUNDARY}",
                content_length=MAX_UPLOAD_BODY_BYTES + 1,
            )

    def test_stream_that_overreads_requested_bound_is_rejected(self):
        body = multipart_body()
        # Content-Length authorizes exactly ``body``. The extra sentinel makes an
        # adversarial stream's size+1 return physically observable, proving the
        # parser rejects bytes beyond the declared request envelope.
        with self.assertRaises(MaterialUploadParseError):
            parse_material_upload(
                _OverreadStream(body + b"X"),
                content_type=f"multipart/form-data; boundary={BOUNDARY}",
                content_length=len(body),
            )

    def test_closed_parsed_upload_cannot_reopen_file_slice(self):
        parsed = self.parse(multipart_body())
        parsed.close()
        with self.assertRaises(MaterialUploadParseError):
            parsed.file_stream()

    def test_wrong_content_type_or_unsafe_boundary_is_rejected(self):
        body = multipart_body()
        with self.assertRaises(MaterialUploadParseError):
            parse_material_upload(
                io.BytesIO(body),
                content_type="application/x-www-form-urlencoded",
                content_length=len(body),
            )
        with self.assertRaises(MaterialUploadParseError):
            parse_material_upload(
                io.BytesIO(body),
                content_type='multipart/form-data; boundary="bad boundary"',
                content_length=len(body),
            )


if __name__ == "__main__":
    unittest.main()
