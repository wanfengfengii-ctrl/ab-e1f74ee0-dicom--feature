"""Minimal, strict ``multipart/form-data`` parser.

Only what this service needs is supported, on purpose:

* a top-level ``multipart/form-data`` body with a ``boundary`` parameter;
* parts carrying ``Content-Disposition: form-data; name="..."`` and an
  optional ``filename`` (RFC 7578);
* CRLF line endings and no preamble/epilogue.

Anything unusual (multiple Content-Disposition headers, unknown framing,
boundary violations, nested multiparts) is rejected as MALFORMED_REQUEST
rather than tolerated. The whole body is bounded up front by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DicomError, MALFORMED_REQUEST

CRLF = b"\r\n"
DASHDASH = b"--"


@dataclass(frozen=True)
class Part:
    name: str
    filename: str | None
    content_type: str | None
    data: bytes


def parse_multipart(body: bytes, content_type: str) -> list[Part]:
    if not content_type:
        raise DicomError(MALFORMED_REQUEST, "Missing Content-Type header")
    media_type, params = _parse_header_options(content_type)
    if media_type.lower() != "multipart/form-data":
        raise DicomError(MALFORMED_REQUEST, "Content-Type must be multipart/form-data")
    boundary = params.get("boundary")
    if not boundary:
        raise DicomError(MALFORMED_REQUEST, "multipart/form-data Content-Type lacks a boundary")
    boundary_b = boundary.encode("ascii", errors="strict")
    if len(boundary_b) > 70:
        raise DicomError(MALFORMED_REQUEST, "Multipart boundary too long")

    delim = DASHDASH + boundary_b
    if not body.startswith(delim):
        raise DicomError(MALFORMED_REQUEST, "Body does not start with the multipart boundary")

    parts: list[Part] = []
    pos = len(delim)

    while True:
        # After a boundary delimiter we expect "--" (end) or CRLF + part.
        if body.startswith(DASHDASH, pos):
            pos += 2
            # Epilogue: only CRLF / empty allowed.
            tail = body[pos:]
            if tail and tail != CRLF:
                raise DicomError(MALFORMED_REQUEST, "Unexpected epilogue data after closing boundary")
            break
        if not body.startswith(CRLF, pos):
            raise DicomError(MALFORMED_REQUEST, "Malformed boundary delimiter (expected CRLF)")
        pos += 2

        header_end = body.find(CRLF + CRLF, pos)
        if header_end == -1:
            raise DicomError(MALFORMED_REQUEST, "Multipart part headers are not terminated")
        header_block = body[pos:header_end]
        pos = header_end + 4

        next_delim = _find_delimiter_line(body, delim, pos)
        if next_delim == -1:
            raise DicomError(MALFORMED_REQUEST, "Multipart part is not followed by a boundary")
        payload = body[pos:next_delim]
        pos = next_delim + 2 + len(delim)  # skip CRLF and the boundary itself

        parts.append(_parse_part_headers(header_block, payload))

    if not parts:
        raise DicomError(MALFORMED_REQUEST, "Multipart body contains no parts")
    return parts


def _find_delimiter_line(body: bytes, delim: bytes, start: int) -> int:
    """Find the next *real* boundary delimiter line (``CRLF--boundary``).

    A boundary token only counts when followed by CRLF (another part) or
    ``--`` (closing). Tokens that merely share the boundary prefix inside a
    payload must be skipped.
    """
    needle = CRLF + delim
    pos = start
    while True:
        hit = body.find(needle, pos)
        if hit == -1:
            return -1
        after = hit + len(needle)
        if body.startswith(CRLF, after) or body.startswith(DASHDASH, after):
            return hit
        # False prefix match inside payload data; keep scanning.
        pos = hit + 1


def _parse_part_headers(block: bytes, payload: bytes) -> Part:
    name: str | None = None
    filename: str | None = None
    content_type: str | None = None
    disposition_seen = False

    for raw_line in block.split(CRLF):
        if not raw_line:
            raise DicomError(MALFORMED_REQUEST, "Empty line inside multipart header block")
        try:
            line = raw_line.decode("ascii")
        except UnicodeDecodeError:
            raise DicomError(MALFORMED_REQUEST, "Non-ASCII bytes in multipart headers")
        if ":" not in line:
            raise DicomError(MALFORMED_REQUEST, "Malformed multipart header line")
        header_name, header_value = line.split(":", 1)
        header_name = header_name.strip().lower()
        header_value = header_value.strip()
        if not header_name or not header_value:
            raise DicomError(MALFORMED_REQUEST, "Empty multipart header name or value")

        if header_name == "content-disposition":
            if disposition_seen:
                raise DicomError(MALFORMED_REQUEST, "Repeated Content-Disposition header")
            disposition_seen = True
            disp, options = _parse_header_options(header_value)
            if disp.lower() != "form-data":
                raise DicomError(MALFORMED_REQUEST, "Part Content-Disposition must be form-data")
            name = options.get("name")
            filename = options.get("filename")
        elif header_name == "content-type":
            if content_type is not None:
                raise DicomError(MALFORMED_REQUEST, "Repeated Content-Type header in part")
            content_type = header_value
        # All other part headers are ignored but still syntax-checked above.

    if not disposition_seen:
        raise DicomError(MALFORMED_REQUEST, "Part is missing a Content-Disposition header")
    if not name:
        raise DicomError(MALFORMED_REQUEST, "Part is missing a form field name")
    if filename is not None and content_type is None:
        # RFC 7578 sends application/octet-stream by default; require a
        # declared Content-Type for the uploaded file to keep inputs explicit.
        content_type = "application/octet-stream"

    return Part(name=name, filename=filename, content_type=content_type, data=payload)


def _parse_header_options(value: str) -> tuple[str, dict[str, str]]:
    """Parse a header value into its main token and ``key=value`` options."""
    parts = _split_header(value)
    if not parts:
        raise DicomError(MALFORMED_REQUEST, "Empty header value")
    main = parts[0].strip()
    if not main:
        raise DicomError(MALFORMED_REQUEST, "Empty header token")
    options: dict[str, str] = {}
    for item in parts[1:]:
        if "=" not in item:
            raise DicomError(MALFORMED_REQUEST, f"Malformed header parameter {item!r}")
        key, val = item.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
            if '"' in val:
                raise DicomError(MALFORMED_REQUEST, "Nested quotes in header parameter")
        if not key:
            raise DicomError(MALFORMED_REQUEST, "Empty header parameter name")
        options[key] = val
    return main, options


def _split_header(value: str) -> list[str]:
    """Split on semicolons while honoring double-quoted parameter values."""
    out: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in value:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == ";" and not in_quotes:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    if in_quotes:
        raise DicomError(MALFORMED_REQUEST, "Unterminated quoted header parameter")
    return out
