"""Extract and compare vector bars from TUMexchange chance PDFs.

The public API deliberately returns ordinary dictionaries.  This keeps the
export format stable for the CLI, SQLite, and the small HTML report while
allowing later versions to add fields without making callers depend on a
particular dataclass implementation.
"""

from .extract import (
    EXTRACTION_VERSION,
    CalibrationError,
    ExtractionError,
    UnsupportedLayoutError,
    extract_pdf,
)

__all__ = [
    "EXTRACTION_VERSION",
    "CalibrationError",
    "ExtractionError",
    "UnsupportedLayoutError",
    "extract_pdf",
]

