"""Unit tests for the strict DICOM parser."""

from __future__ import annotations

import base64

from app.errors import (
    INVALID_BASIC_OFFSET_TABLE,
    INVALID_EXTENDED_OFFSET_TABLE,
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
from app.parser import parse_dicom
from tests.helpers import raises_dicom_error
from tests.dicom_builder import (
    build_dicom,
    encapsulated_pixel_data,
    evr,
    is_value,
    jpeg_stream,
    split_frame,
    struct,
)


def _frames(n, multi=()):
    frames = []
    for i in range(n):
        payload = jpeg_stream(seed=i + 1, with_restart=(i in multi))
        frames.append(split_frame(payload, (10, 30)) if i in multi else payload)
    return frames


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #

def test_single_frame_minimal():
    f = jpeg_stream(seed=9)
    parsed = parse_dicom(build_dicom([f]))
    assert parsed.number_of_frames == 1
    assert len(parsed.frames) == 1
    assert parsed.frames[0].data == f
    assert parsed.frames[0].fragment_count == 1


def test_multiple_frames_with_multifragment_frame_and_restart_markers():
    frames = _frames(4, multi=(1, 3))
    parsed = parse_dicom(build_dicom(frames))
    assert parsed.number_of_frames == 4
    for i, expected_fragments in enumerate(frames):
        frags = [expected_fragments] if isinstance(expected_fragments, (bytes, bytearray)) else expected_fragments
        joined = b"".join(frags)
        assert parsed.frames[i].data == joined
        assert parsed.frames[i].fragment_count == len(frags)
        assert parsed.frames[i].byte_count == len(joined)


def test_extraction_is_order_and_byte_stable():
    frames = _frames(3, multi=(2,))
    blob = build_dicom(frames)
    a = parse_dicom(blob)
    b = parse_dicom(blob)
    for fa, fb in zip(a.frames, b.frames):
        assert fa.data == fb.data
        assert fa.fragment_count == fb.fragment_count


# --------------------------------------------------------------------------- #
# Preamble / meta
# --------------------------------------------------------------------------- #

def test_bad_magic():
    blob = build_dicom(_frames(1), magic=b"XXXX")
    with raises_dicom_error(INVALID_PREAMBLE):
        parse_dicom(blob)


def test_truncated_preamble():
    blob = build_dicom(_frames(1))[50:]
    with raises_dicom_error(INVALID_PREAMBLE):
        parse_dicom(blob)


def test_meta_first_element_wrong():
    # rebuild with a wrong first meta element
    meta_body = evr(0x0002, 0x0010, "UI", b"1.2.840.10008.1.2.4.50")
    meta = evr(0x0002, 0x0002, "UI", b"1.2\x00")
    blob = b"\x00" * 128 + b"DICM" + meta + meta_body
    with raises_dicom_error(INVALID_METADATA):
        parse_dicom(blob)


def test_meta_length_not_on_element_boundary():
    with raises_dicom_error(INVALID_METADATA):
        parse_dicom(build_dicom(_frames(1), meta_length_delta=1))


def test_meta_length_crosses_file_end():
    with raises_dicom_error(TRUNCATED_DATA):
        parse_dicom(build_dicom(_frames(1), meta_length_delta=10000))


def test_unsupported_transfer_syntax():
    with raises_dicom_error(UNSUPPORTED_TRANSFER_SYNTAX):
        parse_dicom(build_dicom(_frames(1), transfer_syntax="1.2.840.10008.1.2.1"))


def test_missing_transfer_syntax():
    # Build meta containing only Implementation Class UID.
    meta_body = evr(0x0002, 0x0012, "UI", b"1.2.3\x00")
    meta = evr(0x0002, 0x0000, "UL", struct.pack("<I", len(meta_body)))
    stream = encapsulated_pixel_data(_frames(1))
    pd = struct.pack("<HH", 0x7FE0, 0x0010) + b"OB\x00\x00" + struct.pack("<I", 0xFFFFFFFF) + stream
    ds = (
        evr(0x0028, 0x0008, "IS", is_value(1))
        + pd
    )
    with raises_dicom_error(INVALID_METADATA):
        parse_dicom(b"\x00" * 128 + b"DICM" + meta + meta_body + ds)


# --------------------------------------------------------------------------- #
# Dataset structure
# --------------------------------------------------------------------------- #

def test_missing_number_of_frames():
    with raises_dicom_error(INVALID_FRAME_DECLARATION):
        parse_dicom(build_dicom(_frames(1), include_number_of_frames=False))


def test_number_of_frames_zero():
    with raises_dicom_error(INVALID_FRAME_DECLARATION):
        parse_dicom(build_dicom([], declared_frames=1, number_of_frames_raw=b"0 "))


def test_number_of_frames_too_large():
    with raises_dicom_error(INVALID_FRAME_DECLARATION):
        parse_dicom(build_dicom(_frames(1), number_of_frames_raw=b"257 "))


def test_declared_count_mismatch():
    # Number of Frames says 2 but BOT contains 1 entry.
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(1), declared_frames=2))


def test_undefined_length_non_pixel_element_rejected():
    with raises_dicom_error(MALFORMED_ELEMENT):
        parse_dicom(build_dicom(_frames(1), undefined_prefix_element=True))


def test_defined_length_pixel_data_rejected():
    with raises_dicom_error(PIXEL_DATA_STRUCTURE):
        parse_dicom(build_dicom(_frames(1), pixel_data_length=100))


def test_trailing_bytes_rejected():
    with raises_dicom_error(UNEXPECTED_TRAILING_DATA):
        parse_dicom(build_dicom(_frames(1), trailing=b"\x00\x00"))


def test_truncated_element_value():
    blob = build_dicom(_frames(1))
    with raises_dicom_error(TRUNCATED_DATA):
        parse_dicom(blob[:-20])


def test_implicit_vr_rejected():
    # Replace the Pixel Data explicit VR header bytes with an implicit-LE tag
    # (no VR, 32-bit length directly) -> the "VR" bytes will be length bytes
    # and fail VR validation.
    frames = _frames(1)
    blob = bytearray(build_dicom(frames))
    idx = blob.find(struct.pack("<HH", 0x7FE0, 0x0010))
    blob[idx + 4: idx + 8] = b"\x00\x00\x00\x00"
    with raises_dicom_error(MALFORMED_ELEMENT):
        parse_dicom(bytes(blob))


def test_dataset_elements_out_of_order_rejected():
    # Insert a tag (0008,0008) AFTER (0028,...) elements via the dedicated hook.
    blob = build_dicom(_frames(1), extra_before_pixel=evr(0x0008, 0x0008, "CS", b"SM"))
    with raises_dicom_error(MALFORMED_ELEMENT):
        parse_dicom(blob)


def test_group_0002_in_main_dataset_rejected():
    # A stray group-0002 element placed (in order-ish, after 0028 tags it is
    # still ascending by integer) must be rejected regardless of order.
    blob = build_dicom(_frames(1), extra_before_pixel=evr(0x0002, 0x0016, "UI", b"1.2\x00"))
    with raises_dicom_error(MALFORMED_ELEMENT):
        parse_dicom(blob)


def test_odd_length_generic_element_rejected():
    # (0010,0010) Patient Name PN with an odd value length.
    blob = build_dicom(_frames(1), extra_before_pixel=evr(0x0010, 0x0010, "PN", b"A"))
    with raises_dicom_error(MALFORMED_ELEMENT):
        parse_dicom(blob)

# --------------------------------------------------------------------------- #
# Encapsulated pixel data / BOT
# --------------------------------------------------------------------------- #

def test_empty_bot_rejected():
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), bot_entries=[]))


def test_bot_first_entry_not_zero():
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), bot_entries=[4, 40]))


def test_bot_not_strictly_increasing():
    frames = _frames(2)
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(frames, bot_entries=[0, 0]))


def test_bot_offset_off_boundary():
    frames = _frames(3)
    # offset 2 lands inside the first fragment's header/payload, not an item
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(frames, bot_entries=[0, 2, 60]))


def test_bot_entry_count_mismatch():
    frames = _frames(2)
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        # 3 offsets for 2 actual frame starts
        parse_dicom(build_dicom(frames, bot_entries=[0, 40, 80]))


def test_bot_length_not_multiple_of_four():
    frames = _frames(1)
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(frames, bot_value=b"\x00\x00\x00", bot_item_length=3))


def test_missing_sequence_delimiter():
    frames = _frames(1)
    stream = encapsulated_pixel_data(frames)
    # strip the trailing sequence delimiter (8 bytes)
    blob = build_dicom(frames, pixel_data_stream=stream[:-8])
    with raises_dicom_error(TRUNCATED_DATA):
        parse_dicom(blob)


def test_sequence_delimiter_nonzero_length():
    with raises_dicom_error(PIXEL_DATA_STRUCTURE):
        parse_dicom(build_dicom(_frames(1), sequence_delim_length=4))


def test_fragment_with_undefined_length_rejected():
    with raises_dicom_error(PIXEL_DATA_STRUCTURE):
        parse_dicom(build_dicom(_frames(2), undefined_fragment_items=True))


def test_bad_item_tag_in_stream():
    frames = _frames(1)
    stream = bytearray(encapsulated_pixel_data(frames))
    # Corrupt the fragment item tag (right after BOT which is 4 bytes long here)
    bot_len = 4
    stream[8 + bot_len: 8 + bot_len + 4] = struct.pack("<HH", 0xFFFE, 0xE00D)
    blob = build_dicom(frames, pixel_data_stream=bytes(stream))
    with raises_dicom_error(PIXEL_DATA_STRUCTURE):
        parse_dicom(blob)


def test_bot_points_past_actual_item():
    frames = _frames(2)
    # second offset declares a boundary that doesn't exist
    with raises_dicom_error(INVALID_BASIC_OFFSET_TABLE):
        parse_dicom(build_dicom(frames, bot_entries=[0, 999999]))


# --------------------------------------------------------------------------- #
# Extended Offset Table (7FE0,0001) / Lengths (7FE0,0002) with an empty BOT
# --------------------------------------------------------------------------- #

def _extended(frames, **kwargs):
    """Build a file whose frame boundaries come from the extended tables."""
    kwargs.setdefault("extended", True)
    kwargs.setdefault("bot_entries", [])  # empty Basic Offset Table
    return build_dicom(frames, **kwargs)


def test_extended_single_frame():
    f = jpeg_stream(seed=41)
    parsed = parse_dicom(_extended([f]))
    assert parsed.number_of_frames == 1
    assert parsed.frames[0].data == f
    assert parsed.frames[0].fragment_count == 1


def test_extended_multiframe_with_multifragment_frame():
    frames = _frames(4, multi=(1, 3))
    parsed = parse_dicom(_extended(frames))
    assert parsed.number_of_frames == 4
    for i, expected_fragments in enumerate(frames):
        frags = [expected_fragments] if isinstance(expected_fragments, (bytes, bytearray)) else expected_fragments
        joined = b"".join(frags)
        assert parsed.frames[i].data == joined
        assert parsed.frames[i].fragment_count == len(frags)
        assert parsed.frames[i].byte_count == len(joined)


def test_extended_extraction_is_byte_stable():
    frames = _frames(3, multi=(2,))
    blob = _extended(frames)
    a = parse_dicom(blob)
    b = parse_dicom(blob)
    for fa, fb in zip(a.frames, b.frames):
        assert fa.data == fb.data
        assert fa.fragment_count == fb.fragment_count


def test_extended_odd_length_frame_padding_verbatim():
    odd = jpeg_stream(seed=42, pad_to_even=False, force_odd=True)
    assert len(odd) % 2 == 1
    f = odd + b"\x00"
    parsed = parse_dicom(_extended([f]))
    assert parsed.frames[0].data == f


def test_extended_missing_lengths_table_rejected():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), include_eotl=False))


def test_extended_missing_offsets_table_rejected():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), include_eot=False))


def test_extended_mixed_with_nonempty_bot_rejected():
    # Non-empty Basic Offset Table (natural entries) plus the extended pair.
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(build_dicom(_frames(2), extended=True))


def test_extended_offset_count_mismatch_rejected():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), eot_entries=[0]))


def test_extended_lengths_count_mismatch_rejected():
    frames = _frames(2)
    natural = [len(f) for f in frames]
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(frames, eotl_entries=natural + [8]))


def test_extended_first_offset_not_zero():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), eot_entries=[8, 160]))


def test_extended_offsets_not_strictly_increasing():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), eot_entries=[0, 0]))


def test_extended_offset_off_fragment_boundary():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), eot_entries=[0, 2]))


def test_extended_offset_out_of_range():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), eot_entries=[0, 999999]))


def test_extended_offset_beyond_32_bits_accepted_semantics():
    # 64-bit entries are parsed as such; a huge offset simply misses every
    # fragment boundary and is rejected as out of range (not truncated).
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), eot_entries=[0, 2**33]))


def test_extended_length_contradicts_fragment_extent():
    frames = _frames(2)
    natural = [len(f) for f in frames]
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(frames, eotl_entries=[natural[0], natural[1] + 2]))


def test_extended_length_ignores_item_headers():
    # Lengths cover fragment payloads only; adding the 8-byte item header of
    # the frame's single fragment must be rejected as a contradiction.
    frames = _frames(2)
    natural = [len(f) for f in frames]
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(frames, eotl_entries=[natural[0] + 8, natural[1]]))


def test_extended_offsets_table_wrong_vr_rejected():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), eot_vr="OW"))


def test_extended_value_length_not_multiple_of_8():
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(1), eot_value=b"\x00" * 4))


def test_extended_declared_frame_count_mismatch():
    # Tables carry 2 entries but Number of Frames declares 3.
    with raises_dicom_error(INVALID_EXTENDED_OFFSET_TABLE):
        parse_dicom(_extended(_frames(2), declared_frames=3))


# --------------------------------------------------------------------------- #
# JPEG structure
# --------------------------------------------------------------------------- #

def test_missing_soi():
    bad = jpeg_stream(seed=1, soi=False)
    with raises_dicom_error(INVALID_JPEG_STREAM):
        parse_dicom(build_dicom([bad]))


def test_missing_eoi():
    bad = jpeg_stream(seed=1, eoi=False)
    with raises_dicom_error(INVALID_JPEG_STREAM):
        parse_dicom(build_dicom([bad]))


def test_non_baseline_sof_rejected():
    bad = jpeg_stream(seed=1, sof_marker=0xC2)  # progressive
    with raises_dicom_error(INVALID_JPEG_STREAM):
        parse_dicom(build_dicom([bad]))


def test_arithmetic_coding_sof_rejected():
    bad = jpeg_stream(seed=1, sof_marker=0xC9)
    with raises_dicom_error(INVALID_JPEG_STREAM):
        parse_dicom(build_dicom([bad]))


def test_truncated_jpeg_segment():
    f = jpeg_stream(seed=1)
    # cut inside the APP0 segment (it claims 16 bytes) while keeping even size
    bad = f[:12] + f[-2:]
    with raises_dicom_error(INVALID_JPEG_STREAM):
        parse_dicom(build_dicom([bad]))


def test_multifragment_jpeg_validates_after_concatenation():
    f = jpeg_stream(seed=4, with_restart=True)
    parts = split_frame(f, (12, 40))
    parsed = parse_dicom(build_dicom([parts]))
    assert parsed.frames[0].data == f
    assert parsed.frames[0].fragment_count == 3


# --------------------------------------------------------------------------- #
# Base64 sanity (payloads are returned verbatim by the API layer)
# --------------------------------------------------------------------------- #

def test_frame_bytes_roundtrip_base64():
    f = jpeg_stream(seed=6)
    parsed = parse_dicom(build_dicom([f]))
    assert base64.b64encode(parsed.frames[0].data).decode() == base64.b64encode(f).decode()


def test_single_nul_pad_after_eoi_accepted_verbatim():
    # Odd-length encoded frame: the encoder appends one NUL after EOI so the
    # final fragment item has even length.
    odd = jpeg_stream(seed=2, pad_to_even=False, force_odd=True)
    assert len(odd) % 2 == 1
    f = odd + b"\x00"
    parsed = parse_dicom(build_dicom([f]))
    assert parsed.frames[0].data == f  # pad byte is returned verbatim


def test_two_nul_pad_bytes_after_eoi_rejected():
    f = bytearray(jpeg_stream(seed=3))
    f += b"\x00\x00"
    with raises_dicom_error(INVALID_JPEG_STREAM):
        parse_dicom(build_dicom([bytes(f)]))
