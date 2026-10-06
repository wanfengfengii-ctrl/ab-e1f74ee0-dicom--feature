"""Unit tests for the strict multipart/form-data parser."""

from __future__ import annotations

from app.errors import MALFORMED_REQUEST
from app.multipart_parse import parse_multipart
from tests.helpers import raises_dicom_error

B = "----testboundary1234"
CT = f"multipart/form-data; boundary={B}"


def _body(parts: list[bytes], *, closing: bytes | None = None, preamble: bytes = b"") -> bytes:
    out = preamble
    for p in parts:
        out += b"--" + B.encode() + b"\r\n" + p + b"\r\n"
    out += b"--" + B.encode() + (b"--\r\n" if closing is None else closing)
    return out


def _part(name: str, value: bytes, *, filename: str | None = None, content_type: str | None = None,
          extra_headers: bytes = b"") -> bytes:
    disp = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        disp += f'; filename="{filename}"'
    block = disp.encode() + b"\r\n"
    if content_type:
        block += f"Content-Type: {content_type}\r\n".encode()
    block += extra_headers
    block += b"\r\n" + value
    return block


def test_parses_field_and_file_preserving_payload():
    body = _body([
        _part("frames", b"0,1"),
        _part("file", b"\x00\x01\x02DICM", filename="a.dcm", content_type="application/dicom"),
    ])
    parts = parse_multipart(body, CT)
    assert len(parts) == 2
    assert parts[0].name == "frames" and parts[0].data == b"0,1" and parts[0].filename is None
    assert parts[1].name == "file" and parts[1].filename == "a.dcm"
    assert parts[1].content_type == "application/dicom"
    assert parts[1].data == b"\x00\x01\x02DICM"


def test_missing_content_type():
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(b"xyz", "")


def test_wrong_media_type():
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(b"xyz", "application/octet-stream")


def test_missing_boundary_parameter():
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(b"xyz", "multipart/form-data")


def test_body_does_not_start_with_boundary():
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(b"preamble-junk", CT)


def test_missing_closing_boundary():
    body = _body([_part("frames", b"0")], closing=b"\r\njunk")
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(body, CT)


def test_epilogue_data_rejected():
    body = _body([_part("frames", b"0")]) + b"extra-epilogue"
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(body, CT)


def test_part_without_content_disposition():
    block = b"Content-Type: text/plain\r\n\r\nvalue"
    body = _body([block])
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(body, CT)


def test_part_without_name():
    block = b'Content-Disposition: form-data; filename="x"\r\n\r\nvalue'
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(_body([block]), CT)


def test_part_disposition_not_form_data():
    block = b'Content-Disposition: attachment; name="x"\r\n\r\nv'
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(_body([block]), CT)


def test_quoted_boundary_parameter():
    ct = f'multipart/form-data; boundary="{B}"'
    body = _body([_part("frames", b"0")])
    parts = parse_multipart(body, ct)
    assert parts[0].data == b"0"


def test_binary_payload_untouched():
    payload = bytes(range(256))
    body = _body([_part("file", payload, filename="x")])
    parts = parse_multipart(body, CT)
    assert parts[0].data == payload


def test_crlf_inside_payload_is_part_of_value():
    payload = b"line1\r\nline2\r\n--" + B.encode() + b"-not-a-boundary"
    body = _body([_part("file", payload, filename="x")])
    parts = parse_multipart(body, CT)
    assert parts[0].data == payload


def test_no_parts_rejected():
    body = b"--" + B.encode() + b"--\r\n"
    with raises_dicom_error(MALFORMED_REQUEST):
        parse_multipart(body, CT)
