"""Strict, zero-tolerance parser for the one accepted DICOM subset.

Accepted subset (anything else is rejected with a stable error type):

* Part 10 file: 128-byte preamble + ``DICM`` magic.
* File Meta Information (group 0002), Explicit VR Little Endian, with a
  correct (0002,0000) group length landing on an element boundary.
* Main dataset: Explicit VR Little Endian only.
* Exactly one Pixel Data element (7FE0,0010), encapsulated
  (undefined length), containing a complete Basic Offset Table whose
  offsets are zero-based (first == 0), strictly increasing, one per
  declared frame, each landing on a fragment item boundary.
* Pixel data encoded with JPEG Baseline (1.2.840.10008.1.2.4.50).
* Every element before/after Pixel Data has a finite (explicit) length
  and lands on a clean boundary; the file ends exactly at the last
  element boundary.
* All item lengths and the Sequence Delimitation Item of the
  encapsulated pixel stream are validated.

The parser never "heals" anything: no fallback to implicit VR, no
EOB-based fragment guessing, no tolerance of padding bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import (
    DicomError,
    INVALID_BASIC_OFFSET_TABLE,
    INVALID_FRAME_DECLARATION,
    INVALID_JPEG_STREAM,
    INVALID_METADATA,
    INVALID_PREAMBLE,
    MALFORMED_ELEMENT,
    PIXEL_DATA_STRUCTURE,
    TRUNCATED_DATA,
    UNEXPECTED_TRAILING_DATA,
    UNSUPPORTED_TRANSFER_SYNTAX,
)

PREAMBLE_LEN = 128
DICM = b"DICM"

# Tags
TAG_FILE_META_INFO_LENGTH = 0x00020000
TAG_TRANSFER_SYNTAX_UID = 0x00020010
TAG_NUMBER_OF_FRAMES = 0x00280008
TAG_PIXEL_DATA = 0x7FE00010

ITEM_TAG = 0xFFFEE000
ITEM_DELIMITATION_TAG = 0xFFFEE00D
SEQUENCE_DELIMITATION_TAG = 0xFFFEE0DD

UNDEFINED_LENGTH = 0xFFFFFFFF

JPEG_BASELINE_TS = "1.2.840.10008.1.2.4.50"

MAX_DECLARED_FRAMES = 256

# VRs whose 16-bit length field is replaced by 2 reserved bytes + 32-bit length.
EXTENDED_LENGTH_VRS = frozenset(
    {"OB", "OD", "OF", "OL", "OV", "OW", "SQ", "UC", "UN", "UR", "UT"}
)


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Frame:
    """One extracted frame: raw concatenated fragment payload plus stats."""

    data: bytes
    fragment_count: int

    @property
    def byte_count(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class ParsedDicom:
    number_of_frames: int
    frames: list[Frame]


# --------------------------------------------------------------------------- #
# Bounds-checked cursor
# --------------------------------------------------------------------------- #

class _Reader:
    def __init__(self, blob: bytes):
        self.b = blob
        self.n = len(blob)
        self.pos = 0

    def take(self, size: int, error_type: str, what: str) -> bytes:
        if size < 0:  # pragma: no cover - lengths are unsigned, defensive only
            raise DicomError(error_type, f"Negative length while reading {what}")
        end = self.pos + size
        if end > self.n:
            raise DicomError(TRUNCATED_DATA, f"Truncated data while reading {what}")
        chunk = self.b[self.pos:end]
        self.pos = end
        return chunk

    def eof(self) -> bool:
        return self.pos >= self.n


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def _is_valid_vr(vr: bytes) -> bool:
    return len(vr) == 2 and all(0x41 <= c <= 0x5A for c in vr)


def _read_element_header(
    r: _Reader,
    error_type: str,
    what: str,
    *,
    allow_undefined_tag: int | None = None,
) -> tuple[int, str, int]:
    """Read one Explicit VR Little Endian element header.

    Returns ``(tag, vr, value_length)``. Undefined lengths are rejected
    everywhere except the tag named by ``allow_undefined_tag`` (Pixel Data).
    """
    head = r.take(8, error_type, f"{what} element header")
    group = _u16(head, 0)
    element = _u16(head, 2)
    tag = (group << 16) | element
    vr = head[4:6]

    # (FFFE,xxxx) item/delimiter tags never belong where an element is expected.
    if group == 0xFFFE:
        raise DicomError(error_type, f"Unexpected item/delimiter tag (FFFE,{element:04X}) in {what}")

    if not _is_valid_vr(vr):
        raise DicomError(
            error_type,
            f"Invalid/implicit VR {vr!r} in {what} element ({group:04X},{element:04X})",
        )
    vr_s = vr.decode("ascii")

    if vr_s in EXTENDED_LENGTH_VRS:
        if head[6] != 0 or head[7] != 0:
            raise DicomError(error_type, f"Non-zero reserved bytes in {what} element ({group:04X},{element:04X})")
        length = _u32(r.take(4, error_type, f"{what} 32-bit length"), 0)
    else:
        length = _u16(head, 6)

    if length == UNDEFINED_LENGTH and tag != allow_undefined_tag:
        raise DicomError(error_type, f"Undefined-length element ({group:04X},{element:04X}) in {what} is not accepted")

    # PS3.5: every data element has an even value length (odd ones are pad
    # extended). An odd length here signals truncation or offset misalignment.
    if length != UNDEFINED_LENGTH and length % 2:
        raise DicomError(error_type, f"Odd value length for element ({group:04X},{element:04X}) in {what}")

    return tag, vr_s, length


def _read_item_tag(r: _Reader, error_type: str, what: str) -> tuple[int, int]:
    head = r.take(8, error_type, what)
    # Item tags are encoded as two little-endian 16-bit halves: group at
    # offset 0 (FFFE), element at offset 2 (E000/E00D/E0DD). Reassemble in
    # the same (group<<16 | element) space as the element-tag constants.
    tag = (_u16(head, 0) << 16) | _u16(head, 2)
    length = _u32(head, 4)
    return tag, length


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def parse_dicom(blob: bytes) -> ParsedDicom:
    r = _Reader(blob)

    _parse_preamble(r)
    _parse_file_meta(r)  # advances r.pos past the meta group and validates TS

    number_of_frames: int | None = None
    frames: list[Frame] | None = None
    pixel_data_seen = False
    previous_tag: int | None = None

    while not r.eof():
        tag, vr, length = _read_element_header(
            r,
            MALFORMED_ELEMENT,
            "dataset",
            allow_undefined_tag=TAG_PIXEL_DATA,
        )
        group = tag >> 16
        if group in (0x0000, 0x0002):
            raise DicomError(
                MALFORMED_ELEMENT,
                f"File meta/command group tag ({group:04X},{tag & 0xFFFF:04X}) must not appear in the main dataset",
            )
        # PS3.7: data elements shall be encoded in strictly increasing tag
        # order. Equal/descending tags reveal duplicates or misalignment.
        if previous_tag is not None and tag <= previous_tag:
            raise DicomError(MALFORMED_ELEMENT, f"Data elements are not in ascending tag order at ({group:04X},{tag & 0xFFFF:04X})")
        previous_tag = tag

        if tag == TAG_NUMBER_OF_FRAMES:
            if pixel_data_seen:
                raise DicomError(INVALID_FRAME_DECLARATION, "Number of Frames must appear before Pixel Data")
            if vr != "IS":
                raise DicomError(INVALID_FRAME_DECLARATION, "Number of Frames (0028,0008) must use VR IS")
            number_of_frames = _parse_frame_count(r.take(length, TRUNCATED_DATA, "Number of Frames value"))
            continue

        if tag == TAG_PIXEL_DATA:
            if vr != "OB":
                raise DicomError(
                    PIXEL_DATA_STRUCTURE,
                    f"Encapsulated Pixel Data must use VR OB (found {vr})",
                )
            if length != UNDEFINED_LENGTH:
                raise DicomError(PIXEL_DATA_STRUCTURE, "Encapsulated Pixel Data must have undefined length")
            frames = _parse_encapsulated_pixel_data(r)
            pixel_data_seen = True
            # Pixel Data must be the final element: the finite-length element
            # requirement is scoped to the elements preceding it, and the
            # encapsulated stream ends with its own sequence delimiter.
            if not r.eof():
                raise DicomError(UNEXPECTED_TRAILING_DATA, "Trailing bytes/elements after encapsulated Pixel Data")
            continue

        # Ordinary finite-length element: consume exactly its declared bytes,
        # which validates the element boundary against the file end.
        r.take(length, TRUNCATED_DATA, f"element ({tag >> 16:04X},{tag & 0xFFFF:04X}) value")

    if not pixel_data_seen:
        raise DicomError(PIXEL_DATA_STRUCTURE, "Dataset contains no Pixel Data (7FE0,0010) element")
    if number_of_frames is None:
        raise DicomError(INVALID_FRAME_DECLARATION, "Number of Frames (0028,0008) is required")
    if not (1 <= number_of_frames <= MAX_DECLARED_FRAMES):
        raise DicomError(
            INVALID_FRAME_DECLARATION,
            f"Declared frame count {number_of_frames} outside 1..{MAX_DECLARED_FRAMES}",
        )

    assert frames is not None
    if len(frames) != number_of_frames:
        raise DicomError(
            INVALID_BASIC_OFFSET_TABLE,
            f"Basic Offset Table yields {len(frames)} frames but Number of Frames is {number_of_frames}",
        )

    for idx, frame in enumerate(frames):
        _validate_jpeg_baseline(frame, idx)

    return ParsedDicom(number_of_frames=number_of_frames, frames=frames)


# --------------------------------------------------------------------------- #
# Preamble + File Meta Information
# --------------------------------------------------------------------------- #

def _parse_preamble(r: _Reader) -> None:
    r.take(PREAMBLE_LEN, INVALID_PREAMBLE, "128-byte preamble")
    magic = r.take(4, INVALID_PREAMBLE, "DICM magic")
    if magic != DICM:
        raise DicomError(INVALID_PREAMBLE, "Missing or invalid 'DICM' preamble magic")


def _parse_file_meta(r: _Reader) -> None:
    tag, vr, length = _read_element_header(r, INVALID_METADATA, "file meta header")
    if tag != TAG_FILE_META_INFO_LENGTH:
        raise DicomError(
            INVALID_METADATA,
            "First meta element must be (0002,0000) File Meta Information Group Length",
        )
    if vr != "UL" or length != 4:
        raise DicomError(INVALID_METADATA, "(0002,0000) must be UL with value length 4")

    meta_length = _u32(r.take(4, INVALID_METADATA, "meta group length value"), 0)
    meta_end = r.pos + meta_length
    if meta_end > r.n:
        raise DicomError(TRUNCATED_DATA, "File Meta Information length exceeds file size")

    transfer_syntax: str | None = None
    previous_meta_tag: int | None = TAG_FILE_META_INFO_LENGTH
    while r.pos < meta_end:
        m_tag, m_vr, m_len = _read_element_header(r, INVALID_METADATA, "file meta")
        if (m_tag >> 16) != 0x0002:
            raise DicomError(INVALID_METADATA, f"Non-group-0002 tag ({m_tag >> 16:04X},{m_tag & 0xFFFF:04X}) inside file meta information")
        if previous_meta_tag is not None and m_tag <= previous_meta_tag:
            raise DicomError(INVALID_METADATA, "File meta elements are not in ascending tag order")
        previous_meta_tag = m_tag
        value = r.take(m_len, INVALID_METADATA, "file meta element value")
        if r.pos > meta_end:
            raise DicomError(INVALID_METADATA, "Meta element value crosses the declared meta group boundary")
        if m_tag == TAG_TRANSFER_SYNTAX_UID:
            if m_vr != "UI":
                raise DicomError(INVALID_METADATA, "Transfer Syntax UID (0002,0010) must use VR UI")
            transfer_syntax = _decode_uid(value)

    if r.pos != meta_end:
        raise DicomError(INVALID_METADATA, "File Meta Information length does not land on an element boundary")
    if transfer_syntax is None:
        raise DicomError(INVALID_METADATA, "Missing Transfer Syntax UID (0002,0010)")
    if transfer_syntax != JPEG_BASELINE_TS:
        raise DicomError(
            UNSUPPORTED_TRANSFER_SYNTAX,
            f"Unsupported transfer syntax {transfer_syntax!r}; only JPEG Baseline is accepted",
        )


def _decode_uid(value: bytes) -> str:
    try:
        text = value.rstrip(b"\x00 ").decode("ascii")
    except UnicodeDecodeError:
        raise DicomError(INVALID_METADATA, "UID contains non-ASCII bytes")
    if not text:
        raise DicomError(INVALID_METADATA, "Empty UID value")
    for ch in text:
        if not (ch.isdigit() or ch == "."):
            raise DicomError(INVALID_METADATA, f"Illegal character in UID {text!r}")
    return text


def _parse_frame_count(raw: bytes) -> int:
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise DicomError(INVALID_FRAME_DECLARATION, "Number of Frames contains non-ASCII bytes")
    if not text.isdigit():
        raise DicomError(INVALID_FRAME_DECLARATION, f"Number of Frames is not a decimal integer: {raw!r}")
    value = int(text)
    if value == 0:
        raise DicomError(INVALID_FRAME_DECLARATION, "Number of Frames must be >= 1")
    return value


# --------------------------------------------------------------------------- #
# Encapsulated Pixel Data (PS3.5 A.4)
# --------------------------------------------------------------------------- #

def _parse_encapsulated_pixel_data(r: _Reader) -> list[Frame]:
    """Parse the item stream following an undefined-length Pixel Data header."""

    # The very first item must be the Basic Offset Table item (FFFE,E000).
    tag, bot_length = _read_item_tag(r, PIXEL_DATA_STRUCTURE, "Basic Offset Table item header")
    if tag != ITEM_TAG:
        raise DicomError(PIXEL_DATA_STRUCTURE, f"Expected Basic Offset Table item (FFFE,E000), found {tag:08X}")
    if bot_length == UNDEFINED_LENGTH:
        raise DicomError(INVALID_BASIC_OFFSET_TABLE, "Basic Offset Table item must have a defined length")
    if bot_length % 4 != 0:
        raise DicomError(INVALID_BASIC_OFFSET_TABLE, "Basic Offset Table length must be a multiple of 4")

    bot_bytes = r.take(bot_length, TRUNCATED_DATA, "Basic Offset Table entries")
    # BOT offsets are measured from the first byte after the BOT item, which
    # must be the tag of the first fragment item (no padding allowed).
    origin = r.pos

    count = bot_length // 4
    offsets = list(struct.unpack(f"<{count}I", bot_bytes)) if count else []

    fragments: list[tuple[int, bytes]] = []  # (absolute file offset of item tag, payload)
    while True:
        item_start = r.pos
        tag, length = _read_item_tag(r, PIXEL_DATA_STRUCTURE, "pixel data item header")

        if tag == SEQUENCE_DELIMITATION_TAG:
            if length != 0:
                raise DicomError(PIXEL_DATA_STRUCTURE, "Sequence Delimitation Item (FFFE,E0DD) must have length 0")
            break
        if tag == ITEM_DELIMITATION_TAG:
            raise DicomError(PIXEL_DATA_STRUCTURE, "Unexpected Item Delimitation Item (FFFE,E00D) in pixel data")
        if tag != ITEM_TAG:
            raise DicomError(PIXEL_DATA_STRUCTURE, f"Unexpected tag {tag:08X} in encapsulated pixel data")

        # PS3.5 A.4: every Fragment Item carries an explicit, even? length;
        # undefined-length items and Item Delimitation Items never occur in a
        # conformant encapsulated pixel stream.
        if length == UNDEFINED_LENGTH:
            raise DicomError(PIXEL_DATA_STRUCTURE, "Encapsulated pixel fragment items must have a defined length")
        if length < 2 or length % 2:
            raise DicomError(PIXEL_DATA_STRUCTURE, "Encapsulated pixel fragment item length must be an even number >= 2")
        payload = r.take(length, TRUNCATED_DATA, "pixel data fragment payload")
        fragments.append((item_start, payload))

    if not fragments:
        raise DicomError(PIXEL_DATA_STRUCTURE, "Encapsulated pixel data contains no fragment items")
    if fragments[0][0] != origin:
        raise DicomError(INVALID_BASIC_OFFSET_TABLE, "Bytes between Basic Offset Table and first fragment item")
    if not offsets:
        raise DicomError(INVALID_BASIC_OFFSET_TABLE, "Empty Basic Offset Table is not accepted (frame boundaries would be ambiguous)")

    return _group_fragments_into_frames(fragments, origin, offsets)


def _group_fragments_into_frames(
    fragments: list[tuple[int, bytes]],
    origin: int,
    offsets: list[int],
) -> list[Frame]:
    # Validate offset semantics first, so every failure is reported as a
    # bad offset table rather than guessed from fragment layout.
    if offsets[0] != 0:
        raise DicomError(INVALID_BASIC_OFFSET_TABLE, "First Basic Offset Table entry must be 0")
    for i in range(1, len(offsets)):
        if offsets[i] <= offsets[i - 1]:
            raise DicomError(INVALID_BASIC_OFFSET_TABLE, "Basic Offset Table entries must be strictly increasing")

    relative_boundaries = {item_off - origin for item_off, _ in fragments}
    for off in offsets:
        if off not in relative_boundaries:
            raise DicomError(INVALID_BASIC_OFFSET_TABLE, f"Offset {off} does not land on a fragment item boundary")

    # Map each declared offset to the ordinal of the fragment starting there.
    starts_abs = [origin + off for off in offsets]
    frames: list[Frame] = []
    fi = 0
    for i, start_abs in enumerate(starts_abs):
        if fi >= len(fragments) or fragments[fi][0] != start_abs:
            raise DicomError(INVALID_BASIC_OFFSET_TABLE, f"Frame {i} offset does not coincide with a fragment item start")
        next_start = starts_abs[i + 1] if i + 1 < len(starts_abs) else None

        gathered: list[bytes] = []
        while fi < len(fragments):
            item_off, payload = fragments[fi]
            if next_start is not None and item_off >= next_start:
                break
            gathered.append(payload)
            fi += 1
        if not gathered:
            raise DicomError(INVALID_BASIC_OFFSET_TABLE, f"Frame {i} contains no fragments")
        frames.append(Frame(data=b"".join(gathered), fragment_count=len(gathered)))

    if fi != len(fragments):
        raise DicomError(INVALID_BASIC_OFFSET_TABLE, "Fragments exist that are not covered by any frame offset")

    return frames


# --------------------------------------------------------------------------- #
# JPEG Baseline marker-level validation (no pixel decoding performed)
# --------------------------------------------------------------------------- #

def _validate_jpeg_baseline(frame: Frame, idx: int) -> None:
    data = frame.data
    # PS3.5 6.2/A.4: the last fragment of a frame may carry one trailing NUL
    # pad byte after EOI so the fragment item has even length. Exactly one is
    # legal; it is kept in the verbatim payload but excluded from marker walks.
    if len(data) >= 3 and data.endswith(b"\xff\xd9\x00"):
        data = data[:-1]

    if len(data) < 4:
        raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: JPEG stream too short")
    if data[0:2] != b"\xff\xd8":
        raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: missing SOI marker")
    if data[-2:] != b"\xff\xd9":
        raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: missing EOI marker")

    pos = 2
    saw_sof = False
    saw_sos = False
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: expected marker prefix at byte {pos}")
        while pos < n and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= n:
            raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: dangling fill bytes before marker")
        marker = data[pos]
        pos += 1

        if marker == 0xD9:  # EOI
            if pos != n:
                raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: trailing bytes after EOI")
            break
        if marker == 0xD8:
            raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: unexpected SOI inside stream")
        if 0xD0 <= marker <= 0xD7:  # RSTn between marker segments: standalone
            continue
        if marker == 0x00:
            raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: stuffed 0x00 where a marker code was expected")

        if pos + 2 > n:
            raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: truncated segment at marker FF{marker:02X}")
        seg_len = (data[pos] << 8) | data[pos + 1]
        if seg_len < 2 or pos + seg_len > n:
            raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: bad segment length for marker FF{marker:02X}")

        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if marker != 0xC0:
                raise DicomError(
                    INVALID_JPEG_STREAM,
                    f"Frame {idx}: not JPEG Baseline (found SOF{marker - 0xC0:X}; only SOF0 allowed)",
                )
            saw_sof = True

        if marker == 0xDA:  # SOS: length-prefixed header followed by entropy data
            pos += seg_len
            pos = _skip_entropy(data, pos, idx)
            saw_sos = True
            continue

        pos += seg_len

    if not saw_sof:
        raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: no SOF0 (Baseline DCT) marker")
    if not saw_sos:
        raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: no SOS scan present")


def _skip_entropy(data: bytes, pos: int, idx: int) -> int:
    """Return the position of the 0xFF prefix of the marker ending the scan."""
    n = len(data)
    while pos < n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        j = pos
        while j < n and data[j] == 0xFF:
            j += 1
        if j >= n:
            raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: entropy scan runs past EOI")
        m = data[j]
        if m == 0x00:  # byte stuffing inside entropy data
            pos = j + 1
            continue
        if 0xD0 <= m <= 0xD7:  # RSTn restart inside scan
            pos = j + 1
            continue
        if m == 0xD9:  # EOI ends the scan
            return j - 1
        raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: unexpected marker FF{m:02X} inside entropy scan")
    raise DicomError(INVALID_JPEG_STREAM, f"Frame {idx}: entropy scan never terminates")
