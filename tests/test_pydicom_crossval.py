"""Cross-validation against a real DICOM implementation (pydicom).

These tests guarantee byte-for-byte agreement with a widely used,
standards-compliant encoder/decoder for Basic Offset Table semantics,
fragment grouping and odd-length frame padding.
"""

from __future__ import annotations

import io

from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate, get_frame
from pydicom.uid import UID

from app.parser import parse_dicom
from tests.dicom_builder import jpeg_stream

TS = UID("1.2.840.10008.1.2.4.50")


def _pydicom_file(frames, fragments_per_frame=1):
    stream = encapsulate(frames, fragments_per_frame=fragments_per_frame, has_bot=True)

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.6"
    meta.MediaStorageSOPInstanceUID = "1.2.3.4"
    meta.TransferSyntaxUID = TS

    ds = Dataset()
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Modality = "SM"
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "YBR_FULL_422"
    ds.Rows = 1
    ds.Columns = 1
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = len(frames)

    fds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    for key, value in ds.items():
        fds[key] = value
    fds["PixelData"] = DataElement(0x7FE00010, "OB", stream)

    buf = io.BytesIO()
    fds.save_as(buf, enforce_file_format=True, little_endian=True, implicit_vr=False)
    return buf.getvalue(), stream


def test_matches_pydicom_single_fragment_frames():
    frames = [jpeg_stream(seed=i + 1, pad_to_even=False) for i in range(3)]
    blob, stream = _pydicom_file(frames, fragments_per_frame=1)

    parsed = parse_dicom(blob)
    assert parsed.number_of_frames == 3
    for i, raw in enumerate(frames):
        assert parsed.frames[i].data == get_frame(stream, i)
        assert parsed.frames[i].fragment_count == 1


def test_matches_pydicom_multi_fragment_frames():
    frames = [jpeg_stream(seed=i + 10, pad_to_even=False) for i in range(3)]
    blob, stream = _pydicom_file(frames, fragments_per_frame=2)

    parsed = parse_dicom(blob)
    for i in range(3):
        assert parsed.frames[i].fragment_count == 2
        assert parsed.frames[i].data == get_frame(stream, i)


def test_matches_pydicom_odd_length_frame_padding():
    frames = [
        jpeg_stream(seed=1, pad_to_even=False, force_odd=False),
        jpeg_stream(seed=2, pad_to_even=False, force_odd=True),
        jpeg_stream(seed=3, pad_to_even=False, force_odd=False),
    ]
    assert [len(f) % 2 for f in frames] == [0, 1, 0]
    blob, stream = _pydicom_file(frames, fragments_per_frame=1)

    parsed = parse_dicom(blob)
    for i, raw in enumerate(frames):
        expected = get_frame(stream, i)
        assert parsed.frames[i].data == expected
        assert expected == (raw if len(raw) % 2 == 0 else raw + b"\x00")
