"""One-shot verification entrypoint for the ``verify`` compose service.

Sequence (the process exits non-zero on the first failed stage):

1. Wait for the API health endpoint to report healthy.
2. Run the code test suite (``pytest``).
3. Build/inspect the ASGI application (imports + route table).
4. API smoke tests against the running container:
   * a valid single-frame request and a valid multi-frame / multi-fragment
     request, asserting verbatim Base64 payloads, fragment counts, byte
     counts and SHA-256, plus byte-for-byte stability across repeated calls;
   * a valid file using an empty Basic Offset Table with the Extended Offset
     Table / Extended Offset Table Lengths pair, asserting verbatim payloads
     and request ordering;
   * a file with a corrupt Basic Offset Table, asserting the stable error
     type and the absence of any partial frame response;
   * a file whose Extended Offset Table Lengths contradict the fragment
     layout, asserting the stable error type and no partial frame response.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import time
import uuid

# Test builders are part of the image (./tests is copied in).
sys.path.insert(0, os.getcwd())

import httpx  # noqa: E402

from tests.dicom_builder import build_dicom, jpeg_stream, split_frame  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080").rstrip("/")
HEALTH_URL = f"{API_BASE_URL}/health"
FRAMES_URL = f"{API_BASE_URL}/api/dicom/frames"


def _step(name: str) -> None:
    print(f"\n=== verify: {name} ===", flush=True)


def fail(message: str) -> "None":
    print(f"VERIFY FAILED: {message}", flush=True)
    sys.exit(1)


def wait_for_health(timeout: float = 60.0) -> None:
    _step(f"waiting for API health at {HEALTH_URL}")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(HEALTH_URL, timeout=3)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                print("health OK", flush=True)
                return
            last_error = RuntimeError(f"status={resp.status_code} body={resp.text!r}")
        except Exception as exc:  # connection refused while the API boots
            last_error = exc
        time.sleep(1)
    fail(f"API did not become healthy within {timeout:.0f}s (last error: {last_error!r})")


def run_code_tests() -> None:
    _step("running code test suite (pytest)")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=os.getcwd(),
    )
    if proc.returncode != 0:
        fail(f"pytest exited with {proc.returncode}")
    print("pytest OK", flush=True)


def build_application() -> None:
    _step("building/inspecting the ASGI application")
    from app.main import app

    paths = {route.path for route in app.routes}
    for required in ("/health", "/api/dicom/frames"):
        if required not in paths:
            fail(f"application is missing route {required} (found {sorted(paths)})")

    import uvicorn  # noqa: F401

    print(f"application build OK; routes={sorted(paths)}; uvicorn={uvicorn.__version__}", flush=True)


def _multipart(indices, file_bytes, filename="smoke.dcm"):
    boundary = "----verify" + uuid.uuid4().hex
    lines = bytearray()
    lines += b"--" + boundary.encode() + b"\r\n"
    lines += 'Content-Disposition: form-data; name="frames"\r\n\r\n'.encode()
    lines += ",".join(str(i) for i in indices).encode() + b"\r\n"
    lines += b"--" + boundary.encode() + b"\r\n"
    lines += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
    ).encode()
    lines += b"Content-Type: application/dicom\r\n\r\n"
    lines += file_bytes + b"\r\n"
    lines += b"--" + boundary.encode() + b"--\r\n"
    body = bytes(lines)
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    return body, headers


def smoke_valid_single_frame() -> bytes:
    _step("API smoke: valid single-frame file")
    frame0 = jpeg_stream(seed=11)
    blob = build_dicom([frame0])
    body, headers = _multipart([0], blob)
    resp = httpx.post(FRAMES_URL, content=body, headers=headers, timeout=10)
    if resp.status_code != 200:
        fail(f"valid single-frame request returned {resp.status_code}: {resp.text}")
    data = resp.json()
    if data["number_of_frames"] != 1 or [f["index"] for f in data["frames"]] != [0]:
        fail(f"unexpected response envelope: {data}")
    frame = data["frames"][0]
    if base64.b64decode(frame["data"]) != frame0:
        fail("returned frame payload is not the verbatim JPEG bytes")
    if frame["fragment_count"] != 1 or frame["byte_count"] != len(frame0):
        fail("fragment_count/byte_count mismatch for single-frame file")
    if frame["sha256"] != hashlib.sha256(frame0).hexdigest():
        fail("sha256 mismatch for single-frame file")
    print("single-frame OK", flush=True)
    return frame["data"]


def smoke_valid_multifragment_and_stable() -> None:
    _step("API smoke: valid multi-fragment frames, request ordering and stability")
    f0 = jpeg_stream(seed=21)
    f1_full = jpeg_stream(seed=22, with_restart=True)
    f1_parts = split_frame(f1_full, (8, 24))  # 3 fragments, cuts are even
    f2 = jpeg_stream(seed=23)
    blob = build_dicom([f0, f1_parts, f2])

    def call():
        body, headers = _multipart([2, 0, 1], blob)
        resp = httpx.post(FRAMES_URL, content=body, headers=headers, timeout=10)
        if resp.status_code != 200:
            fail(f"valid multi-frame request returned {resp.status_code}: {resp.text}")
        return resp.json()

    first = call()
    second = call()
    if first != second:
        fail("repeated requests did not return byte-identical responses")

    if [f["index"] for f in first["frames"]] != [2, 0, 1]:
        fail("response did not preserve request frame order")

    expected = {0: (f0, 1), 1: (f1_full, 3), 2: (f2, 1)}
    by_index = {f["index"]: f for f in first["frames"]}
    for index, (raw, frag_count) in expected.items():
        frame = by_index[index]
        if base64.b64decode(frame["data"]) != raw:
            fail(f"frame {index} payload is not the concatenated raw fragments")
        if frame["fragment_count"] != frag_count:
            fail(f"frame {index} fragment_count {frame['fragment_count']} != {frag_count}")
        if frame["byte_count"] != len(raw):
            fail(f"frame {index} byte_count {frame['byte_count']} != {len(raw)}")
        if frame["sha256"] != hashlib.sha256(raw).hexdigest():
            fail(f"frame {index} sha256 mismatch")
    print("multi-frame / multi-fragment OK and byte-stable", flush=True)


def smoke_bad_offset_table() -> None:
    _step("API smoke: corrupt Basic Offset Table must fail with a stable error type")
    f0 = jpeg_stream(seed=31)
    f1 = jpeg_stream(seed=32)
    blob = build_dicom([f0, f1], bot_entries=[0, 999999])
    body, headers = _multipart([0, 1], blob)
    resp = httpx.post(FRAMES_URL, content=body, headers=headers, timeout=10)
    if resp.status_code != 422:
        fail(f"bad BOT expected HTTP 422, got {resp.status_code}: {resp.text}")
    payload = resp.json()
    if payload.get("error", {}).get("type") != "INVALID_BASIC_OFFSET_TABLE":
        fail(f"bad BOT returned unexpected error payload: {payload}")
    if "frames" in payload:
        fail("bad BOT response must not contain any partial frame data")
    print("bad offset table OK (HTTP 422 INVALID_BASIC_OFFSET_TABLE, no frames)", flush=True)


def smoke_valid_extended_offset_tables() -> None:
    _step("API smoke: valid empty-BOT file with Extended Offset Table pair")
    f0 = jpeg_stream(seed=41)
    f1_full = jpeg_stream(seed=42, with_restart=True)
    f1_parts = split_frame(f1_full, (8, 24))  # 3 fragments, cuts are even
    f2 = jpeg_stream(seed=43)
    blob = build_dicom([f0, f1_parts, f2], extended=True, bot_entries=[])

    def call():
        body, headers = _multipart([2, 0, 1], blob)
        resp = httpx.post(FRAMES_URL, content=body, headers=headers, timeout=10)
        if resp.status_code != 200:
            fail(f"valid extended-table request returned {resp.status_code}: {resp.text}")
        return resp.json()

    first = call()
    second = call()
    if first != second:
        fail("repeated extended-table requests did not return byte-identical responses")

    if first["number_of_frames"] != 3 or [f["index"] for f in first["frames"]] != [2, 0, 1]:
        fail(f"unexpected extended-table response envelope: {first}")

    expected = {0: (f0, 1), 1: (f1_full, 3), 2: (f2, 1)}
    by_index = {f["index"]: f for f in first["frames"]}
    for index, (raw, frag_count) in expected.items():
        frame = by_index[index]
        if base64.b64decode(frame["data"]) != raw:
            fail(f"extended-table frame {index} payload is not the concatenated raw fragments")
        if frame["fragment_count"] != frag_count:
            fail(f"extended-table frame {index} fragment_count {frame['fragment_count']} != {frag_count}")
        if frame["byte_count"] != len(raw):
            fail(f"extended-table frame {index} byte_count {frame['byte_count']} != {len(raw)}")
        if frame["sha256"] != hashlib.sha256(raw).hexdigest():
            fail(f"extended-table frame {index} sha256 mismatch")
    print("extended offset tables OK and byte-stable", flush=True)


def smoke_extended_length_mismatch() -> None:
    _step("API smoke: Extended Offset Table Lengths mismatch must fail with a stable error type")
    f0 = jpeg_stream(seed=51)
    f1 = jpeg_stream(seed=52)
    blob = build_dicom(
        [f0, f1],
        extended=True,
        bot_entries=[],
        eotl_entries=[len(f0), len(f1) + 2],  # contradicts the fragment extent of frame 1
    )
    body, headers = _multipart([0, 1], blob)
    resp = httpx.post(FRAMES_URL, content=body, headers=headers, timeout=10)
    if resp.status_code != 422:
        fail(f"extended length mismatch expected HTTP 422, got {resp.status_code}: {resp.text}")
    payload = resp.json()
    if payload.get("error", {}).get("type") != "INVALID_EXTENDED_OFFSET_TABLE":
        fail(f"extended length mismatch returned unexpected error payload: {payload}")
    if "frames" in payload:
        fail("extended length mismatch response must not contain any partial frame data")
    print("extended length mismatch OK (HTTP 422 INVALID_EXTENDED_OFFSET_TABLE, no frames)", flush=True)


def main() -> None:
    print(f"verify starting against {API_BASE_URL}", flush=True)
    wait_for_health()
    run_code_tests()
    build_application()
    smoke_valid_single_frame()
    smoke_valid_multifragment_and_stable()
    smoke_valid_extended_offset_tables()
    smoke_bad_offset_table()
    smoke_extended_length_mismatch()
    print("\nVERIFY PASSED", flush=True)


if __name__ == "__main__":
    main()
