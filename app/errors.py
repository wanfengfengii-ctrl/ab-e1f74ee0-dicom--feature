"""Stable error vocabulary shared by the strict DICOM parser and the HTTP API.

Every rejected request/file yields one of the ``error_type`` strings below.
The strings are part of the service contract and must not be renamed.
"""

# ---- request-level errors (400 / 413 / 422) ----
FILE_MISSING = "FILE_MISSING"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
MALFORMED_REQUEST = "MALFORMED_REQUEST"
NO_FRAME_INDICES = "NO_FRAME_INDICES"
TOO_MANY_FRAME_INDICES = "TOO_MANY_FRAME_INDICES"
DUPLICATE_FRAME_INDEX = "DUPLICATE_FRAME_INDEX"
INVALID_FRAME_INDEX = "INVALID_FRAME_INDEX"
FRAME_INDEX_OUT_OF_RANGE = "FRAME_INDEX_OUT_OF_RANGE"

# ---- file-level errors (422) ----
INVALID_PREAMBLE = "INVALID_PREAMBLE"
INVALID_METADATA = "INVALID_METADATA"
UNSUPPORTED_TRANSFER_SYNTAX = "UNSUPPORTED_TRANSFER_SYNTAX"
MALFORMED_ELEMENT = "MALFORMED_ELEMENT"
TRUNCATED_DATA = "TRUNCATED_DATA"
INVALID_FRAME_DECLARATION = "INVALID_FRAME_DECLARATION"
PIXEL_DATA_STRUCTURE = "PIXEL_DATA_STRUCTURE"
INVALID_BASIC_OFFSET_TABLE = "INVALID_BASIC_OFFSET_TABLE"
INVALID_EXTENDED_OFFSET_TABLE = "INVALID_EXTENDED_OFFSET_TABLE"
INVALID_JPEG_STREAM = "INVALID_JPEG_STREAM"
UNEXPECTED_TRAILING_DATA = "UNEXPECTED_TRAILING_DATA"


class DicomError(Exception):
    """Raised for any violation of the accepted, narrow DICOM subset."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
