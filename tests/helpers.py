"""Small test helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.errors import DicomError


@contextmanager
def raises_dicom_error(error_type: str) -> Iterator[None]:
    with pytest.raises(DicomError) as exc_info:
        yield
    assert exc_info.value.error_type == error_type, (
        f"expected {error_type}, got {exc_info.value.error_type}: {exc_info.value.message}"
    )
