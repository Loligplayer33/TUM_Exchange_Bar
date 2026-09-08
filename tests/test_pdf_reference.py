"""Independent source-PDF checks used by extractor integration tests."""

from __future__ import annotations

import collections
import math
import unittest
from collections import defaultdict

import pdfplumber

try:  # unittest discover -s tests imports this file as a top-level module.
    from .reference_data import (
        BRAZIL_2025_FULL_WIDTH_PT,
        EXPECTED_NAMED_WIDTHS_PT,
        EXPECTED_PAGE_ROW_COUNTS,
        EXPECTED_SLOT_COUNT,
        EXPECTED_STATUS_COUNTS,
        EXPECTED_STATUS_ROWS,
        SOURCE_PDF,
    )
except ImportError:  # pragma: no cover - exercised by unittest discovery.
    from reference_data import (
    BRAZIL_2025_FULL_WIDTH_PT,
    EXPECTED_NAMED_WIDTHS_PT,
    EXPECTED_PAGE_ROW_COUNTS,
    EXPECTED_SLOT_COUNT,
    EXPECTED_STATUS_COUNTS,
    EXPECTED_STATUS_ROWS,
    SOURCE_PDF,
    )


def _is_cyan(rect: dict) -> bool:
    return tuple(rect.get("non_stroking_color") or ()) == (0.0, 0.439216, 0.752941)


def _is_dark_bar_or_status(rect: dict) -> bool:
    return (
        tuple(rect.get("non_stroking_color") or ()) == (0.188235, 0.439216, 0.701961)
        and rect["x0"] >= 330
        and rect["height"] > 5
    )


def _source_status_rows(pdf: pdfplumber.pdf.PDF) -> set[tuple[str, int, str]]:
    """Read status cells from words and column geometry, independently."""

    statuses = ("no applications", "no places", "no cooperation")
    columns = ((320, 395, 2024), (395, 465, 2025), (465, 535, 2026))
    found: set[tuple[str, int, str]] = set()
    for page_number, page in enumerate(pdf.pages, start=1):
        y_min = 300 if page_number == 1 else 35
        rows: defaultdict[float, list[dict]] = defaultdict(list)
        for word in page.extract_words(keep_blank_chars=False):
            if word["top"] > y_min:
                rows[round(word["top"], 2)].append(word)
        for words in rows.values():
            label = " ".join(
                word["text"]
                for word in sorted((word for word in words if word["x0"] < 330), key=lambda word: word["x0"])
            )
            for x0, x1, year in columns:
                column_text = " ".join(
                    word["text"]
                    for word in sorted(
                        (word for word in words if x0 <= word["x0"] < x1),
                        key=lambda word: word["x0"],
                    )
                )
                for status in statuses:
                    if status in column_text:
                        found.add((label, year, status))
    return found


def _rects_for_row(page: pdfplumber.page.Page, top: float, x0: float) -> list[dict]:
    return [
        rect
        for rect in page.rects
        if _is_dark_bar_or_status(rect)
        and math.isclose(rect["top"], top, abs_tol=0.02)
        and math.isclose(rect["x0"], x0, abs_tol=0.02)
        and rect["height"] < 9
    ]


class SourcePdfReferenceTests(unittest.TestCase):
    """Assert facts computed directly from PDF text and vector geometry."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pdf = pdfplumber.open(SOURCE_PDF)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.pdf.close()

    def test_table_has_four_pages_and_expected_rows(self) -> None:
        self.assertEqual(len(self.pdf.pages), 4)
        actual = {}
        for page_number, page in enumerate(self.pdf.pages, start=1):
            # The table borders are cyan horizontal rectangles.  Status cells
            # interrupt a border into segments, so count distinct y positions.
            ys = {
                round(rect["top"], 2)
                for rect in page.rects
                if _is_cyan(rect) and rect["height"] < 1 and rect["width"] >= 18
            }
            actual[page_number] = len(ys)
        self.assertEqual(actual, EXPECTED_PAGE_ROW_COUNTS)

    def test_status_text_occurs_in_expected_counts(self) -> None:
        actual = collections.Counter()
        for _, _, status in _source_status_rows(self.pdf):
            actual[status] += 1
        self.assertEqual(actual, collections.Counter(EXPECTED_STATUS_COUNTS))
        self.assertEqual(sum(actual.values()), 48)

    def test_source_status_years_are_known_and_complete(self) -> None:
        # This independent inventory guards against assigning a status to the
        # wrong year when table rows have missing bars.
        actual = _source_status_rows(self.pdf)
        self.assertEqual(actual, set(EXPECTED_STATUS_ROWS))
        self.assertEqual(len(actual), 48)

    def test_full_reference_bars_share_one_pdf_width(self) -> None:
        page = self.pdf.pages[0]
        bars = []
        for top in (515.52, 527.76, 540.00):
            bars.extend(_rects_for_row(page, top, 405.12))
        self.assertEqual(len(bars), 3)
        self.assertTrue(all(math.isclose(rect["width"], BRAZIL_2025_FULL_WIDTH_PT, abs_tol=1e-4) for rect in bars))

    def test_named_source_widths_and_status_box_are_distinct(self) -> None:
        # The expected width map is deliberately small and focuses on examples
        # that expose normalization mistakes.
        page = self.pdf.pages[0]
        itba = _rects_for_row(page, 319.68, 335.40)
        sydney = _rects_for_row(page, 460.44, 335.40)
        self.assertEqual(len(itba), 1)
        self.assertEqual(len(sydney), 1)
        self.assertAlmostEqual(itba[0]["width"], EXPECTED_NAMED_WIDTHS_PT[("Instituto Tecnológico de Buenos Aires (ITBA)", 2024)], places=2)
        self.assertAlmostEqual(sydney[0]["width"], EXPECTED_NAMED_WIDTHS_PT[("University of Sydney", 2024)], places=2)
        self.assertEqual(EXPECTED_SLOT_COUNT, 375)

        # White-on-dark `no applications` cells are 51.12 pt wide in this PDF,
        # so their background must not be mistaken for a numeric 100% bar.
        boxes = [
            rect
            for page in self.pdf.pages
            for rect in page.rects
            if _is_dark_bar_or_status(rect) and math.isclose(rect["width"], 51.12, abs_tol=1e-4)
        ]
        self.assertEqual(len(boxes), 17)
        self.assertNotAlmostEqual(boxes[0]["width"], BRAZIL_2025_FULL_WIDTH_PT, places=2)


if __name__ == "__main__":
    unittest.main()
