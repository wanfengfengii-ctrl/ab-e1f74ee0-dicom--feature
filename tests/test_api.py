"""End-to-end tests for POST /api/dicom/frames and GET /health."""

from __future__ import annotations

import base64
import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.errors import (
    DUPLICATE_FRAME_INDEX,
    FILE_MISSING,
    FILE_TOO_LARGE,
    FRAME_INDEX_OUT_OF_RANGE,
    INVALID_BASIC_OFFSET_TABLE,
    INVALID_FRAME_INDEX,
    INVALID_PREAMBLE,
    NO_FRAME_INDICES,
    TOO_MANY_FRAME_INDICES,
    UNSUPPORTED_TRANSFER_SYNTAX,
)
from app.main import MAX_FILE_BYTES, app
from tests.dicom_builder import build_dicom, jpeg_stream, split_frame

BOUNDARY = "----dicomtestboundary42"


def encode_multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes]],
                      *, boundary: str = BOUNDARY, raw_tail: bytes | None = None,
                      omit_closing: bool = False) -> bytes:
    lines = bytearray()
    for name, value in fields:
        lines += b"--" + boundary.encode() + b"\r\n"
        lines += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        lines += value.encode() + b"\r\n"
    for name, filename, data in files:
        lines += b"--" + boundary.encode() + b"\r\n"
        lines += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        lines += b"Content-Type: application/dicom\r\n\r\n"
        lines += data + b"\r\n"
    if not omit_closing:
        lines += b"--" + boundary.encode() + b"--\r\n"
    if raw_tail is not None:
        lines += raw_tail
    return bytes(lines)


def _frames(n, multi=()):
    out = []
    for i in range(n):
        payload = jpeg_stream(seed=i + 1, with_restart=(i in multi))
        out.append(split_frame(payload, (8, 24)) if i in multi else payload)
    return out


@pytest.fixture
def valid_blob():
    return build_dicom(_frames(4, multi=(2,)))


async def _post(body: bytes, content_type: str = f"multipart/form-data; boundary={BOUNDARY}"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/dicom/frames",
            content=body,
            headers={"content-type": content_type},
        )


def _frames_fields(indices: list[int]) -> list[tuple[str, str]]:
    # send one field per index to exercise multi-part collection + ordering
    return [("frames", str(i)) for i in indices]


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

async def test_extract_returns_verbatim_base64_fragments_bytes_sha(valid_blob):
    body = encode_multipart(_frames_fields([0, 2]), [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["number_of_frames"] == 4
    assert [f["index"] for f in payload["frames"]] == [0, 2]

    expected_frames = _frames(4, multi=(2,))
    for returned, expected_frags, idx in zip(payload["frames"], [expected_frames[0], expected_frames[2]], [0, 2]):
        joined = expected_frags if isinstance(expected_frags, bytes) else b"".join(expected_frags)
        assert returned["byte_count"] == len(joined)
        assert returned["fragment_count"] == (1 if isinstance(expected_frags, bytes) else len(expected_frags))
        assert base64.b64decode(returned["data"]) == joined
        assert returned["sha256"] == hashlib.sha256(joined).hexdigest()


async def test_response_order_follows_request_order(valid_blob):
    body = encode_multipart(_frames_fields([3, 0, 1]), [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 200
    assert [f["index"] for f in resp.json()["frames"]] == [3, 0, 1]


async def test_comma_separated_indices_accepted(valid_blob):
    body = encode_multipart([("frames", "0, 2 ,1")], [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 200
    assert [f["index"] for f in resp.json()["frames"]] == [0, 2, 1]


async def test_results_byte_stable_across_requests(valid_blob):
    bodies = [
        encode_multipart(_frames_fields([2, 0]), [("file", "a.dcm", valid_blob)])
        for _ in range(3)
    ]
    digests = []
    for b in bodies:
        resp = await _post(b)
        assert resp.status_code == 200
        digests.append([(f["sha256"], f["data"], f["byte_count"], f["fragment_count"])
                        for f in resp.json()["frames"]])
    assert digests[0] == digests[1] == digests[2]


async def test_max_32_indices_accepted():
    blob = build_dicom(_frames(32))
    body = encode_multipart([("frames", ",".join(str(i) for i in range(32)))],
                            [("file", "big.dcm", blob)])
    resp = await _post(body)
    assert resp.status_code == 200
    assert len(resp.json()["frames"]) == 32


async def test_declared_256_frames_accepted_and_index_255_valid():
    blob = build_dicom(_frames(256))
    body = encode_multipart([("frames", "255,0")], [("file", "big.dcm", blob)])
    resp = await _post(body)
    assert resp.status_code == 200
    assert [f["index"] for f in resp.json()["frames"]] == [255, 0]


# --------------------------------------------------------------------------- #
# Request-shape errors
# --------------------------------------------------------------------------- #

async def test_missing_file_part():
    body = encode_multipart(_frames_fields([0]), [])
    resp = await _post(body)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == FILE_MISSING


async def test_no_frame_indices(valid_blob):
    body = encode_multipart([], [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == NO_FRAME_INDICES


async def test_too_many_indices(valid_blob):
    body = encode_multipart([("frames", ",".join(str(i) for i in range(33)))],
                            [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == TOO_MANY_FRAME_INDICES


async def test_duplicate_index(valid_blob):
    body = encode_multipart([("frames", "1,0,1")], [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == DUPLICATE_FRAME_INDEX


async def test_non_numeric_index(valid_blob):
    body = encode_multipart([("frames", "0,x")], [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == INVALID_FRAME_INDEX


async def test_negative_index(valid_blob):
    body = encode_multipart([("frames", "-1")], [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == INVALID_FRAME_INDEX


async def test_index_beyond_max_possible(valid_blob):
    body = encode_multipart([("frames", "256")], [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == INVALID_FRAME_INDEX


async def test_index_out_of_range_for_file(valid_blob):
    body = encode_multipart([("frames", "3")], [("file", "a.dcm", valid_blob)])
    # 4 frames -> indices 0..3 valid; bump to 4 which is out of range.
    body = encode_multipart([("frames", "4")], [("file", "a.dcm", valid_blob)])
    resp = await _post(body)
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == FRAME_INDEX_OUT_OF_RANGE
    assert "frames" not in resp.json()  # no partial frame response


async def test_wrong_content_type(valid_blob):
    body = encode_multipart(_frames_fields([0]), [("file", "a.dcm", valid_blob)])
    resp = await _post(body, content_type="application/octet-stream")
    assert resp.status_code == 400


async def test_malformed_multipart_body():
    resp = await _post(b"not a multipart body at all")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Payload errors (422, stable types, no partial frames)
# --------------------------------------------------------------------------- #

async def test_bad_offset_table_rejected():
    good = _frames(2)
    blob = build_dicom(good, bot_entries=[0, 999999])
    body = encode_multipart([("frames", "0,1")], [("file", "bad.dcm", blob)])
    resp = await _post(body)
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == INVALID_BASIC_OFFSET_TABLE
    assert set(resp.json()) == {"error"}


async def test_bad_preamble_rejected():
    blob = build_dicom(_frames(1), magic=b"XXXX")
    body = encode_multipart([("frames", "0")], [("file", "bad.dcm", blob)])
    resp = await _post(body)
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == INVALID_PREAMBLE


async def test_unsupported_transfer_syntax_rejected():
    blob = build_dicom(_frames(1), transfer_syntax="1.2.840.10008.1.2.1")
    body = encode_multipart([("frames", "0")], [("file", "bad.dcm", blob)])
    resp = await _post(body)
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == UNSUPPORTED_TRANSFER_SYNTAX


async def test_one_bad_frame_fails_whole_request():
    # index 0 is valid; the file itself is invalid (bad BOT), so even a valid
    # requested index must not produce a partial response.
    blob = build_dicom(_frames(2), bot_entries=[0, 0])
    body = encode_multipart([("frames", "0")], [("file", "bad.dcm", blob)])
    resp = await _post(body)
    assert resp.status_code == 422
    assert "frames" not in resp.json()


# --------------------------------------------------------------------------- #
# Size limit
# --------------------------------------------------------------------------- #

async def test_file_over_16mib_rejected():
    blob = build_dicom(_frames(1))
    blob += b"\x00" * (MAX_FILE_BYTES + 1 - len(blob))
    body = encode_multipart([("frames", "0")], [("file", "big.dcm", blob)])
    resp = await _post(body)
    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == FILE_TOO_LARGE
