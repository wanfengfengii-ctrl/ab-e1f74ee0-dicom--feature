"""FastAPI application exposing the strict DICOM frame extraction service."""

from __future__ import annotations

import base64
import hashlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import (
    DicomError,
    DUPLICATE_FRAME_INDEX,
    FILE_MISSING,
    FILE_TOO_LARGE,
    INVALID_FRAME_INDEX,
    FRAME_INDEX_OUT_OF_RANGE,
    MALFORMED_REQUEST,
    NO_FRAME_INDICES,
    TOO_MANY_FRAME_INDICES,
)
from .multipart_parse import Part, parse_multipart
from .parser import MAX_DECLARED_FRAMES, parse_dicom

MAX_FILE_BYTES = 16 * 1024 * 1024  # the Part 10 file must not exceed 16 MiB
# Multipart framing + the frame-index fields are allowed a small slack on top.
MAX_BODY_BYTES = MAX_FILE_BYTES + 64 * 1024
MAX_FRAME_INDICES = 32

FILE_FIELD = "file"
FRAMES_FIELD = "frames"

# DicomError error types that are problems with the request shape itself
# rather than the DICOM payload, mapped to HTTP 400.
_BAD_REQUEST_TYPES = frozenset(
    {
        MALFORMED_REQUEST,
        FILE_MISSING,
        NO_FRAME_INDICES,
        TOO_MANY_FRAME_INDICES,
        DUPLICATE_FRAME_INDEX,
        INVALID_FRAME_INDEX,
    }
)

app = FastAPI(title="Strict DICOM Frame Extractor", docs_url=None, redoc_url=None, openapi_url=None)


def _error_body(error_type: str, message: str) -> dict:
    return {"error": {"type": error_type, "message": message}}


@app.exception_handler(DicomError)
async def dicom_error_handler(request: Request, exc: DicomError) -> JSONResponse:
    if exc.error_type == FILE_TOO_LARGE:
        status = 413
    elif exc.error_type in _BAD_REQUEST_TYPES:
        status = 400
    else:
        # Every parser-side structural/boundary/index-range violation.
        status = 422
    return JSONResponse(status_code=status, content=_error_body(exc.error_type, exc.message))


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/dicom/frames")
async def extract_frames(request: Request) -> JSONResponse:
    body = await _read_capped_body(request)

    content_type = request.headers.get("content-type", "")
    parts = parse_multipart(body, content_type)

    file_part = _select_file_part(parts)
    indices = _parse_frame_indices(parts)

    if len(file_part.data) > MAX_FILE_BYTES:
        raise DicomError(FILE_TOO_LARGE, f"Part 10 file exceeds {MAX_FILE_BYTES} bytes")

    # Parse and validate the entire file first; a single bad frame or boundary
    # must fail the whole request without returning any frame payload.
    parsed = parse_dicom(file_part.data)

    for index in indices:
        if not 0 <= index < parsed.number_of_frames:
            raise DicomError(
                FRAME_INDEX_OUT_OF_RANGE,
                f"Frame index {index} is out of range for {parsed.number_of_frames} frame(s)",
            )

    response_frames = []
    for index in indices:
        frame = parsed.frames[index]
        digest = hashlib.sha256(frame.data).hexdigest()
        response_frames.append(
            {
                "index": index,
                "fragment_count": frame.fragment_count,
                "byte_count": frame.byte_count,
                "sha256": digest,
                "data": base64.b64encode(frame.data).decode("ascii"),
            }
        )

    return JSONResponse(
        status_code=200,
        content={"number_of_frames": parsed.number_of_frames, "frames": response_frames},
    )


async def _read_capped_body(request: Request) -> bytes:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError:
            raise DicomError(MALFORMED_REQUEST, "Content-Length is not an integer")
        if declared < 0:
            raise DicomError(MALFORMED_REQUEST, "Negative Content-Length")
        if declared > MAX_BODY_BYTES:
            raise DicomError(FILE_TOO_LARGE, f"Request body exceeds the {MAX_FILE_BYTES}-byte file limit")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise DicomError(FILE_TOO_LARGE, f"Request body exceeds the {MAX_FILE_BYTES}-byte file limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _select_file_part(parts: list[Part]) -> Part:
    files = [p for p in parts if p.name == FILE_FIELD]
    if not files:
        raise DicomError(FILE_MISSING, "Request is missing the 'file' form field")
    if len(files) > 1:
        raise DicomError(MALFORMED_REQUEST, "Exactly one 'file' part is accepted")
    return files[0]


def _parse_frame_indices(parts: list[Part]) -> list[int]:
    """Collect indices from every ``frames`` part.

    Each part holds either one index or a comma-separated list; request
    ordering is preserved for the response order.
    """
    raw_parts = [p for p in parts if p.name == FRAMES_FIELD]
    tokens: list[str] = []
    for part in raw_parts:
        try:
            text = part.data.decode("ascii")
        except UnicodeDecodeError:
            raise DicomError(INVALID_FRAME_INDEX, "Frame indices must be ASCII text")
        for token in text.split(","):
            token = token.strip()
            if token:
                tokens.append(token)

    if not tokens:
        raise DicomError(NO_FRAME_INDICES, "At least one zero-based frame index is required")
    if len(tokens) > MAX_FRAME_INDICES:
        raise DicomError(TOO_MANY_FRAME_INDICES, f"At most {MAX_FRAME_INDICES} frame indices are accepted")

    indices: list[int] = []
    for token in tokens:
        indices.append(_parse_index(token))

    seen: set[int] = set()
    for index in indices:
        if index in seen:
            raise DicomError(DUPLICATE_FRAME_INDEX, f"Duplicate frame index {index}")
        seen.add(index)
    return indices


def _parse_index(token: str) -> int:
    # Canonical decimal integer only: optional single sign rejected.
    if not token.isdigit():
        raise DicomError(INVALID_FRAME_INDEX, f"Frame index {token!r} is not a non-negative decimal integer")
    value = int(token)
    if value >= MAX_DECLARED_FRAMES:
        # File-declared frame counts are capped at 256, so indices 0..255 can
        # ever be valid; reject syntactically huge values with the same type.
        raise DicomError(INVALID_FRAME_INDEX, f"Frame index {value} exceeds the maximum possible index 255")
    return value
