"""Unit tests for Extended Offset Table (7FE0,0001)/(7FE0,0002) support.

A WSI-sized file may carry an empty Basic Offset Table and mark frame
boundaries with the paired Extended Offset Table elements instead. These
tests cover the happy path, byte-for-byte payloads and every structural
violation.
"""

from __future__ import annotations

import struct

from app.errors import (
    INVALID_BASIC_OFFSET_TABLE,
    INVALID_EXTENDED_OFFSET_TABLE,
    PIXEL_DATA_STRUCTURE,
    TRUNCATED_DATA,
    UNEXPECTED_TRAILING_DATA,
)
from app.parser import parse_dicom
from tests.helpers import raises_dicom_error
from tests.dicom_builder import (
    build_dicom,
    evr,
    jpeg_stream,
    split_frame,
)


def _frames(n, multi=()):
    out = []
    for i in range(n):
        payload = jpeg_stream(seed=i + 1, with_restart=(i in multi))
        out.append(split_frame(payload, (10, 30)) if i in multi else payload)
    return out


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #

def test_extended_single_fragment_frames():
    frames = _frames(3)
    parsed = parse_dicom(build_dicom(frames, extended=True))
    assert parsed.number_of_frames == 3
    for i, raw in enumerate(frames):
        assert parsed.frames[i].data == raw
        assert parsed.frames[i].fragment_count == 1


def test_extended_multi_fragment_frame_payload_and_count():
    frames = _frames(4, multi=(1, 3))
    parsed = parse_dicom(build_dicom(frames, extended=True))
    assert parsed.number_of_frames == 4
    for i, expected in enumerate(frames):
        frags = [expected] if isinstance(expected, (bytes, bytearray)) else expected
        joined = b"".join(frags)
        assert parsed.frames[i].data == joined
        assert parsed.frames[i].fragment_count == len(frags)
        assert parsed.frames[i].byte_count == len(joined)


def test_extended_byte_stable_across_parses():
    blob = build_dicom(_frames(3, multi=(2,)), extended=True)
    a = parse_dicom(blob)
    b = parse_dicom(blob)
    for fa, fb in zip(a.frames, b.frames):
        assert fa.data == fb.data
        assert fa.fragment_count == fb.fragment_count


def test_extended_odd_length_frame_nul_pad_returned_verbatim():
    odd = jpeg_stream(seed=2, pad_to_even=False, force_odd=True)
    assert len(odd) % 2 == 1
    padded = odd + b"\x00"
    parsed = parse_dicom(build_dicom([padded], extended=True))
    assert parsed.frames[0].data == padded
    assert parsed.frames[0].byte_count == len(padded)


# --------------------------------------------------------------------------- #
# Empty Basic Offset Table + extension relationship
# --------------------------------------------------------------------------- #

def test_empty_bot_without_extension_still_rejected():
    # An empty BOT with no Extended Offset Tables remains ambiguous -> rejected.
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), bot_entries=[]))


def test_extension_requires_empty_bot_mixed_nonempty_bot_rejected():
    # Extended elements must never be combined with a non-empty BOT.
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, bot_entries=[0, 200]))


def test_extension_pair_must_appear_together_missing_lengths():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_include_lengths=False))


def test_extension_pair_must_appear_together_missing_offsets():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_include_offsets=False))


def test_extension_tables_must_precede_pixel_data():
    # A raw (7FE0,0001) appended after the Pixel Data stream is trailing data.
    blob = build_dicom(_frames(1), trailing=evr(0x7FE0, 0x0001, "OV", struct.pack("<Q", 0)))
    with raises_dicom_error(UNEXPECTED_TRAILING_DATA):
        parse_dicom(blob)


# --------------------------------------------------------------------------- #
# VR / encoding
# --------------------------------------------------------------------------- #

def test_extended_offsets_wrong_vr_rejected():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_offsets_vr="OB"))


def test_extended_lengths_wrong_vr_rejected():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_lengths_vr="OB"))


def test_extended_value_length_not_multiple_of_eight():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_offsets_raw=b"\x00" * 12))


def test_extended_value_empty_rejected():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_offsets_raw=b""))


# --------------------------------------------------------------------------- #
# Entry counts
# --------------------------------------------------------------------------- #

def test_extended_entry_count_must_equal_declared_frames():
    # Two real frames/entries but Number of Frames declares 3.
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, declared_frames=3))


def test_extended_pair_entry_counts_must_match():
    # Three offsets but two lengths.
    bad_offsets = struct.pack("<3Q", 0, 200, 400)
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_offsets_raw=bad_offsets))


# --------------------------------------------------------------------------- #
# Offset semantics
# --------------------------------------------------------------------------- #

def test_extended_first_offset_must_be_zero():
    frames = _frames(2)
    size = len(frames[0]) + (len(frames[0]) % 2)
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(
            build_dicom(frames, extended=True, ext_offsets_raw=struct.pack("<2Q", 8, 8 + size + 8))
        )


def test_extended_offsets_must_be_strictly_increasing():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_offsets_raw=struct.pack("<2Q", 0, 0)))


def test_extended_offset_out_of_bounds():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_offsets_raw=struct.pack("<2Q", 0, 999999)))


def test_extended_offset_not_on_fragment_boundary():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(3), extended=True, ext_offsets_raw=struct.pack("<3Q", 0, 2, 400)))


# --------------------------------------------------------------------------- #
# Length semantics
# --------------------------------------------------------------------------- #

def test_extended_length_mismatch_rejected():
    # First declared length one byte short of the actual frame payload.
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True, ext_lengths_raw=struct.pack("<2Q", 1, 140)))


def test_extended_length_must_cover_full_fragment_range_multifragment():
    # Multi-fragment middle frame; corrupt its (middle) length entry.
    frames = _frames(3, multi=(1,))
    good = b"".join(frames[1])
    bad_lengths = struct.pack("<3Q", len(frames[0]), len(good) - 2, len(frames[2]))
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(frames, extended=True, ext_lengths_raw=bad_lengths))


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #

def test_extended_truncated_value_rejected():
    blob = build_dicom(_frames(1), extended=True)
    with raises_dicom_error(TRUNCATED_DATA):
        parse_dicom(blob[: -(8 + 2)])  # drop sequence delimiter + tail


def test_extended_no_fragments_rejected():
    # Empty fragment stream with an otherwise valid extension pair.
    # Build a stream that is only BOT(empty) + sequence delimiter.
    from tests.dicom_builder import encapsulated_pixel_data
    stream = encapsulated_pixel_data([])
    with raises_dicom_error(PIXEL_DATA_STRUCTURE):
        parse_dicom(build_dicom(_frames(1), extended=True, pixel_data_stream=stream))
